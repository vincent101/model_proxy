"""Mac installation authority under the explicitly approved pragmatic policy.

Flock prevents duplicate starts but does not identify the holder. A protected
record is not by itself authority: PID creation/image and the kernel-observed
service command and configuration are checked independently. The connected
socket is separately checked by MacControl before any token is sent.
Same-user impersonation and the identity-check/signal race remain possible.
"""
from __future__ import annotations

import contextlib
import errno
import ipaddress
import json
import os
import re
import socket
import stat
import struct
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.discovery import (
    PreflightError,
    _verify_directory,
    delete_instance_record_if_matches,
    inspect_runtime_identities,
    initialize_installation,
    process_executable,
    process_start_identity,
    read_instance_set,
    verify_held_instance_locks,
    verify_local_filesystem_fd,
    verify_no_extended_acl,
    verify_record_directory,
)
from core.cli.control import (ControlError, MacControl, StartupPending, _check_mac_new_listener,
                              _endpoint, _valid_record,
                              INSTALL_INITIALIZED, INSTALL_UNINITIALIZED_OR_RECORD_LOST)
from core import platform_process
from core.listen_config import resolve_listen
from core.platform_lock import LockError, acquire_lock, verify_local_access
from core.runtime_paths import PATH_KEYS, parse_runtime_paths
from core.secure_files import (
    ProtectionEvidence, SecureSnapshotError, capture_target, mac_private_protection,
)
from core.mac_config_path import capture_config, verify_config, same_target

try:
    import fcntl
except ImportError:  # No Windows lock authority is implied by this module.
    fcntl = None


@dataclass(frozen=True)
class LockConflictObservation:
    """A point-in-time F_GETLK result; not a flock owner or authority."""

    blocked: bool
    reported_pid: int | None


@dataclass(frozen=True, repr=False)
class _BusinessSnapshot:
    """Adapter for existing launch/restart consumers; evidence remains intact."""
    evidence: object

    @property
    def exists(self):
        return True

    @property
    def contents(self):
        return self.evidence.data


def _capture_business(path):
    try:
        return _BusinessSnapshot(capture_config(path))
    except (OSError, RuntimeError, ValueError, TypeError):
        raise PreflightError("config_unverified", "Business configuration unavailable") from None


def _verify_business(snapshot):
    try:
        verify_config(snapshot.evidence)
    except (OSError, RuntimeError, ValueError, TypeError):
        raise PreflightError("config_changed", "Business configuration changed") from None


def _same_business(first, second):
    try:
        if not same_target(first.evidence, second.evidence):
            raise ControlError("installation_identity_unknown")
    except (OSError, RuntimeError, ValueError, TypeError):
        raise ControlError("installation_identity_unknown") from None


def _read_retry(operation):
    """Retry only transient read instability; never retry permission failure."""
    for attempt in range(4):
        try:
            return operation()
        except PreflightError as exc:
            from core.darwin_acl import ACLVerificationError
            cause = exc.__context__
            acl_changed = (exc.code == "protection_unavailable"
                           and isinstance(cause, ACLVerificationError)
                           and cause.code == "acl_changed")
            if ((exc.code not in ("protection_changed", "record_changed", "records_changed")
                 and not acl_changed) or attempt == 3):
                raise
            time.sleep(0.01)


def _capture_restart_ledger(snapshot):
    """Use the server's read-only ledger contract, imported only on restart."""
    try:
        from core.server import _capture_migration_ledger
    except ImportError:
        raise PreflightError("ledger_unverified", "Ledger verification unavailable") from None
    return _capture_migration_ledger(snapshot)


def _validate_restart_business_config(snapshot):
    """Run the server's startup validator on protected bytes, without logging.

    ConfigStore's validator is the startup authority. Rebind its existing code
    objects to a private globals mapping with a no-op logger: this avoids
    leaking config values or mutating process-wide logging state. The validator
    works entirely on an in-memory object; it does not read appkey_file.
    """
    try:
        from core.server import ConfigStore
        if not snapshot.exists or not isinstance(snapshot.contents, bytes):
            raise ValueError("missing snapshot")
        config = json.loads(snapshot.contents.decode("utf-8"))
        if not isinstance(config, dict):
            raise ValueError("invalid snapshot")

        class SilentLog:
            def warning(self, *args, **kwargs):
                pass

        methods = {}
        for name in ("_validate_config", "_validate_cooldown_rules", "_validate_body_error_rules"):
            original = getattr(ConfigStore, name)
            if not isinstance(original, types.FunctionType):
                raise TypeError("startup validator unavailable")
            namespace = dict(original.__globals__)
            namespace["log"] = SilentLog()
            clone = types.FunctionType(original.__code__, namespace, original.__name__,
                                       original.__defaults__, original.__closure__)
            clone.__kwdefaults__ = original.__kwdefaults__
            methods[name] = clone
        validator_type = type("_PrivateStartupValidator", (ConfigStore,), methods)
        validator = object.__new__(validator_type)
        validator._config = config
        validator._validate_config()
    except Exception:
        # Validation errors may contain tokens, ids, paths, or header contents.
        raise PreflightError("config_unverified", "Business configuration invalid") from None


def _check_restart_targets(root, config_path, snapshot):
    """Run the server's path-conflict contract on this installation's paths."""
    try:
        from core.server import _check_config_targets
        if not isinstance(_check_config_targets, types.FunctionType):
            raise ValueError("server path check unavailable")
        sidecar = Path(config_path).parent / "session_overrides.json"
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            info = None
        # A stable, local, readable symlink target can be compared by the
        # server's existing conflict check.  Do not accept dangling/remote or
        # ambiguous identities; never mutate the sidecar in this preflight.
        link_observation = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            def marker(st):
                return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                        st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            _verify_directory(sidecar.parent, private=False)
            target = sidecar.resolve(strict=True)
            _verify_directory(target.parent, private=False)
            target_before = target.lstat()
            if (not stat.S_ISREG(target_before.st_mode) or target_before.st_nlink != 1
                    or target_before.st_uid not in (os.geteuid(), 0)
                    or target_before.st_mode & 0o022):
                raise PreflightError("config_unverified", "Sidecar target unavailable")
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                if marker(os.fstat(fd)) != marker(target_before):
                    raise PreflightError("config_unverified", "Sidecar target changed")
                verify_local_filesystem_fd(fd)
                verify_no_extended_acl(fd)
                if marker(os.fstat(fd)) != marker(target.lstat()):
                    raise PreflightError("config_unverified", "Sidecar target changed")
            finally:
                os.close(fd)
            link_observation = (marker(info), target, marker(target_before))
        namespace = dict(_check_config_targets.__globals__)
        namespace["_PACKAGE_DIR"] = Path(root)
        namespace["_RUNTIME_PATHS_FILE"] = Path(root) / "config" / "runtime_paths.json"
        check = types.FunctionType(_check_config_targets.__code__, namespace,
                                   _check_config_targets.__name__, _check_config_targets.__defaults__,
                                   _check_config_targets.__closure__)
        ids = snapshot.as_dict()
        check({key: ids[key].canonical for key in PATH_KEYS}, config_path)
        if link_observation is not None:
            link_marker, target, target_marker = link_observation
            if (marker(sidecar.lstat()) != link_marker or sidecar.resolve(strict=True) != target
                    or marker(target.lstat()) != target_marker):
                raise PreflightError("config_unverified", "Sidecar identity changed")
    except PreflightError:
        raise
    except LockError as exc:
        code = "config_conflict" if exc.code in ("config_path_conflict", "config_state_conflict") else "config_unverified"
        raise PreflightError(code, "Restart path check unavailable") from None
    except (OSError, ValueError, TypeError, AttributeError, KeyError, ImportError, RuntimeError):
        raise PreflightError("config_unverified", "Restart path check unavailable") from None


def _check_restart_log(snapshot):
    """Prove an existing local log can be opened append-only without writing it.

    A missing log is accepted only with a verified local owner-writable parent.
    ACL ambiguity, a different inode or an inaccessible parent is unknown.
    This is a point-in-time preflight, not a promise of later access.
    """
    try:
        identity = snapshot.as_dict()["log"]
        path = Path(identity.canonical)
        if not identity.exists:
            parent_before = _verify_directory(path.parent, private=False)
            if (identity.final_was_link or parent_before.st_uid != os.geteuid()
                    or parent_before.st_mode & 0o300 != 0o300 or parent_before.st_flags
                    or not os.access(path.parent, os.W_OK | os.X_OK)):
                raise PreflightError("log_write_unverified", "Log parent access unavailable")
            try:
                path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise PreflightError("log_write_unverified", "Log identity changed")
            parent_after = _verify_directory(path.parent, private=False)
            if (parent_before.st_dev, parent_before.st_ino, parent_before.st_mode,
                    parent_before.st_uid, parent_before.st_gid, parent_before.st_flags) != (
                    parent_after.st_dev, parent_after.st_ino, parent_after.st_mode,
                    parent_after.st_uid, parent_after.st_gid, parent_after.st_flags):
                raise PreflightError("log_write_unverified", "Log parent changed")
            try:
                path.lstat()
            except FileNotFoundError:
                return
            raise PreflightError("log_write_unverified", "Log identity changed")
        if identity.final_was_link or identity.link_count != 1:
            raise PreflightError("log_write_unverified", "Log write access unavailable")
        parent_before = _verify_directory(path.parent, private=False)
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        before = path.lstat()
        def marker(st):
            return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_nlink, st.st_flags)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in (os.geteuid(), 0)
                or (before.st_dev, before.st_ino) != (identity.device, identity.inode)):
            raise PreflightError("log_write_unverified", "Log identity unavailable")
        read_fd = os.open(path, os.O_RDONLY | flags)
        try:
            opened = os.fstat(read_fd)
            if marker(opened) != marker(before):
                raise PreflightError("log_write_unverified", "Log identity changed")
            verify_local_filesystem_fd(read_fd)
            verify_no_extended_acl(read_fd)
            # O_APPEND without O_CREAT/O_TRUNC and without a write syscall.
            write_fd = os.open(path, os.O_WRONLY | os.O_APPEND | flags)
            try:
                if marker(os.fstat(write_fd)) != marker(opened):
                    raise PreflightError("log_write_unverified", "Log identity changed")
                if marker(os.fstat(read_fd)) != marker(opened) or marker(path.lstat()) != marker(opened):
                    raise PreflightError("log_write_unverified", "Log identity changed")
                verify_no_extended_acl(read_fd)
                after = _verify_directory(path.parent, private=False)
                if (parent_before.st_dev, parent_before.st_ino, parent_before.st_mode, parent_before.st_uid) != (
                        after.st_dev, after.st_ino, after.st_mode, after.st_uid):
                    raise PreflightError("log_write_unverified", "Log parent changed")
            finally:
                os.close(write_fd)
        finally:
            os.close(read_fd)
    except PreflightError as exc:
        if exc.code == "log_write_unverified":
            raise
        raise PreflightError("log_write_unverified", "Log write access unavailable") from None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise PreflightError("log_write_unverified", "Log write access unavailable") from None


def observe_lock_conflict(fd: int, *, candidate_pid: int | None = None) -> LockConflictObservation:
    """Query a regular file without acquiring or releasing a lock.

    Darwin's F_GETLKPID also matches flock locks regardless of the requested
    PID and reports their owner as -1.  Even a positive PID can describe a
    different kind of record lock, so this result must never authorize action.
    The fixed layout is Darwin's struct flock, not a portable ABI.
    """
    if sys.platform != "darwin" or fcntl is None:
        raise PreflightError("protection_unavailable", "Lock observation unavailable")
    if type(fd) is not int or fd < 0:
        raise PreflightError("locks_unverified", "Lock descriptor unavailable")
    if candidate_pid is not None and (type(candidate_pid) is not int or candidate_pid <= 0):
        raise PreflightError("locks_unverified", "Lock candidate unavailable")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PreflightError("locks_unverified", "Lock file identity unavailable")
        # sys/fcntl.h: off_t, off_t, pid_t, short, short. F_GETLKPID=66.
        request = struct.pack("=qqihh", 0, 0, candidate_pid or 0, fcntl.F_WRLCK, os.SEEK_SET)
        result = fcntl.fcntl(fd, fcntl.F_GETLK if candidate_pid is None else 66, request)
        _, _, pid, kind, _ = struct.unpack("=qqihh", result)
        if kind == fcntl.F_UNLCK:
            return LockConflictObservation(False, None)
        if kind not in (fcntl.F_RDLCK, fcntl.F_WRLCK):
            raise PreflightError("locks_unverified", "Lock conflict unavailable")
        return LockConflictObservation(True, pid if pid > 0 else None)
    except PreflightError:
        raise
    except (OSError, ValueError, OverflowError, struct.error):
        raise PreflightError("locks_unverified", "Lock conflict unavailable") from None


class InstallationObservation:
    """Read records while holding the verified fixed control lock.

    Initialization requires the explicit TTY protocol. No method grants
    permission to start, stop or send a token.
    Direct server entries do not hold control.lock, so record reads may still
    observe concurrent server publication and must fail on incomplete sets.
    """

    def __init__(self, root: Path, paths: Mapping[str, str], held, config_path: Path | None):
        self._root = root
        self._paths = dict(paths)
        self._held = held
        self._config_path = config_path
        self._state = root / ".runtime"
        self._control = self._state / "control.lock"
        st = os.fstat(held.fileno())
        self._control_identity = (st.st_dev, st.st_ino)

    def _check(self) -> None:
        try:
            if self._held.closed:
                raise PreflightError("locks_unverified", "Control lock is not held")
            _read_retry(lambda: verify_record_directory(self._state))
            opened = os.fstat(self._held.fileno())
            current = self._control.lstat()
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                    or opened.st_nlink != 1 or current.st_nlink != 1
                    or opened.st_uid != os.geteuid() or current.st_uid != os.geteuid()
                    or opened.st_mode & 0o077 or current.st_mode & 0o077
                    or opened.st_mode & 0o600 != 0o600
                    or current.st_mode & 0o600 != 0o600
                    or (opened.st_dev, opened.st_ino) != self._control_identity
                    or (current.st_dev, current.st_ino) != self._control_identity
                    or not verify_local_access(self._control, "file")):
                raise PreflightError("locks_unverified", "Control lock identity changed")
            # Recheck path conflicts, but do not infer authority from the result.
            inspect_runtime_identities(self._root, self._paths)
        except PreflightError:
            raise
        except (OSError, ValueError, AttributeError):
            raise PreflightError("locks_unverified", "Control lock identity unavailable") from None

    def record_set(self) -> dict | None:
        """Return the protected consistent schema-2 set, or an absent set."""
        self._check()
        result = _read_retry(lambda: read_instance_set(self._state))
        self._check()
        return result

    def records(self) -> tuple[dict | None, dict | None]:
        """Return the active/last-good pair in the MacControl lease shape."""
        result = self.record_set()
        return ((None, None) if result is None
                else (result["active"], result["last_good_paths"]))

    def installation_state(self) -> str:
        """Classify installation metadata; missing records do not mean stopped."""
        records = self.record_set()
        if records is not None:
            return INSTALL_INITIALIZED
        snapshot = inspect_runtime_identities(self._root, self._paths)
        with acquire_lock(snapshot, "anchor.lock", create=True,
                          access_verifier=verify_local_access):
            # A direct server may have initialized between the first read and
            # the anchor acquisition. Never treat a held anchor as "new".
            records = self.record_set()
            return (INSTALL_INITIALIZED if records is not None
                    else INSTALL_UNINITIALIZED_OR_RECORD_LOST)

    def _launch_inputs(self):
        """Capture protected config and current six-path/endpoint observations."""
        if self._config_path is None:
            raise PreflightError("config_unverified", "Effective configuration unavailable")
        runtime = self._root / "config" / "runtime_paths.json"
        config = _capture_business(self._config_path)
        path_config = _capture(runtime, private=False)
        if not config.exists or not path_config.exists:
            raise PreflightError("config_unverified", "Launch configuration unavailable")
        try:
            raw = json.loads(path_config.contents.decode("utf-8"))
            parsed = parse_runtime_paths(raw, project_root=self._root,
                                         local_appdata=os.environ.get("LOCALAPPDATA"))
            paths = {key: str(parsed[key]) for key in PATH_KEYS}
            if paths != self._paths:
                raise PreflightError("runtime_changed", "Runtime paths changed")
            env_port = os.environ.get("MODEL_PROXY_PORT")
            listen = resolve_listen(self._config_path, env_port)
            _check_mac_new_listener(listen)
            # resolve_listen opens the config separately: reject observable
            # replacement or edits around that read.
            if config != _capture_business(self._config_path):
                raise PreflightError("config_changed", "Configuration changed")
            if path_config != _capture(runtime, private=False):
                raise PreflightError("config_changed", "Runtime configuration changed")
            snapshot = inspect_runtime_identities(self._root, paths)
            _check_input_conflicts(snapshot, (self._config_path, runtime))
            ledger = _check_legacy_observations(snapshot)
            return config, path_config, env_port, listen, ledger, snapshot
        except PreflightError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise PreflightError("config_unverified", "Launch configuration unavailable") from None

    def initialize_interactively(self, stdin, stdout) -> bool:
        """Perform the CLI's explicit TTY initialization under all three locks.

        Checks only the configured legacy lock, PID/start-lock and listener.
        Unknown legacy autostart, other installations, shared ledgers and cloud
        synchronization additionally require the operator's explicit attestation;
        this function does not claim a global process/autostart inventory.
        """
        _require_same_terminal(stdin, stdout)
        self._check()
        before = self._launch_inputs()
        if self.record_set() is not None:
            raise PreflightError("already_initialized", "Installation already initialized")
        snapshot = before[-1]
        with acquire_lock(snapshot, "anchor.lock", create=True,
                          access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True,
                              access_verifier=verify_local_access, anchor=anchor) as legacy:
                verify_held_instance_locks(anchor, legacy)
                # The two acquired locks may have been created; compare against
                # the actual post-acquisition snapshot, not their old absence.
                stable = self._launch_inputs()
                old_targets, new_targets = dict(before[-2]), dict(stable[-2])
                if old_targets["lock"] is None:
                    # acquire_lock created this exact protected file.
                    old_targets["lock"] = new_targets["lock"]
                if (before[:4] != stable[:4] or old_targets != new_targets
                        or stable[-1] != legacy.snapshot):
                    raise PreflightError("preflight_changed", "Launch inputs changed")
                if self.record_set() is not None:
                    raise PreflightError("already_initialized", "Installation already initialized")
                with _bound_listener(stable[3]) as listener:
                    bound_socket = (listener.fileno(), listener.family, listener.getsockname())
                    stdout.write(
                        "Confirm no old starter or enabled autostart, no shared ledger, "
                        "and no cloud-synchronized installation or data.\n"
                        "Installation/recovery source: "
                    )
                    stdout.flush()
                    source = stdin.readline(512)
                    stdout.write("Type YES to confirm: ")
                    stdout.flush()
                    confirmation = stdin.readline(64)
                    if (not source.endswith("\n") or not confirmation.endswith("\n")
                            or not source.strip() or len(source.strip()) > 256
                            or "\x00" in source or confirmation.strip() != "YES"):
                        return False
                    def recheck_confirmed_inputs(*, committed=None):
                        self._check()
                        verify_held_instance_locks(anchor, legacy)
                        after = self._launch_inputs()
                        before_ids, after_ids = stable[-1].as_dict(), after[-1].as_dict()
                        same_inputs = (stable[:-1] == after[:-1]
                                       and all(before_ids[name] == after_ids[name]
                                               for name in before_ids if name != "install.json"))
                        records = self.record_set()
                        expected_records = (records is None if committed is None else
                                            records is not None and records["install"] == committed
                                            and records["active"] is None
                                            and records["last_good_paths"] is None)
                        if not same_inputs or not expected_records:
                            raise PreflightError("preflight_changed", "Initialization inputs changed")
                        if committed is None and before_ids["install.json"] != after_ids["install.json"]:
                            raise PreflightError("preflight_changed", "Installation record changed")
                        try:
                            current_socket = (listener.fileno(), listener.family,
                                              listener.getsockname())
                            if (current_socket != bound_socket or listener.fileno() < 0
                                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                                    != socket.SOCK_STREAM):
                                raise PreflightError("listen_unverified", "Bound listener changed")
                        except OSError:
                            raise PreflightError("listen_unverified", "Bound listener unavailable") from None

                    recheck_confirmed_inputs()
                    listen = {"host": stable[3]["host"], "port": stable[3]["port"]}
                    installed = initialize_installation(
                        anchor, legacy, listener, listen=listen, source=source.strip(),
                        confirmed=True, precommit_check=recheck_confirmed_inputs,
                    )
                    try:
                        # Core's precommit callback cannot make a later write
                        # atomic with config changes. Detect observable changes
                        # once more before reporting a committed initialization.
                        recheck_confirmed_inputs(committed=installed)
                        if installed.get("ever_started") is not False:
                            raise PreflightError("initialization_unverified",
                                                 "Initialization not verified")
                    except BaseException:
                        try:
                            self._check()
                            verify_held_instance_locks(anchor, legacy)
                            delete_instance_record_if_matches(
                                self._state, "install.json", installed,
                                anchor=anchor, legacy=legacy,
                            )
                            if self.record_set() is not None:
                                raise PreflightError("initialization_cleanup_failed",
                                                     "Initialization cleanup unknown")
                        except Exception:
                            raise PreflightError("initialization_cleanup_failed",
                                                 "Initialization cleanup unknown") from None
                        raise
                    return True


def _runtime_protection(path: str, info: os.stat_result) -> ProtectionEvidence:
    """Runtime paths are non-secret but must not be writable by other users."""
    if stat.S_ISDIR(info.st_mode):
        return mac_private_protection(path, info)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                    or st.st_uid not in (os.geteuid(), 0) or st.st_mode & 0o022
                    or (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid)
                    != (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)):
                raise SecureSnapshotError("protection_unknown")
            verify_no_extended_acl(fd)
            verify_local_filesystem_fd(fd)
            return ProtectionEvidence(True, (st.st_dev, st.st_ino, st.st_mode,
                                             st.st_uid, st.st_gid, st.st_nlink))
        finally:
            os.close(fd)
    except (OSError, AttributeError, PreflightError):
        raise SecureSnapshotError("protection_unknown") from None


@dataclass(frozen=True, repr=False)
class _PrivateTraversalSnapshot:
    """Keep cancelled-directory evidence in cross-check equality."""
    target: object
    traversal: tuple

    def __getattr__(self, name):
        return getattr(self.target, name)


def _walk_private_config_parents(path: Path):
    """Prove every traversed directory for a private path containing '..'.

    Normalizing lexically before inspecting a cancelled component would make
    link/.. resolve to the wrong file. Reject all links (including cancelled
    components), verify each actually traversed local directory, and repeat
    this observation around the existing no-link capture.
    """
    if not path.is_absolute() or path.anchor != "/" or path.name in ("", ".."):
        raise SecureSnapshotError("invalid_path")
    current = Path("/")
    observations = []
    for component in path.parts[1:-1]:
        current = current.parent if component == ".." else current / component
        try:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise SecureSnapshotError("parent_link_or_type")
            proof = mac_private_protection(str(current), info)
            if not isinstance(proof, ProtectionEvidence) or proof.secure is not True:
                raise SecureSnapshotError("protection_unknown")
            observations.append((str(current), info.st_dev, info.st_ino, info.st_mode,
                                 info.st_uid, info.st_gid, info.st_flags, proof))
        except OSError:
            raise SecureSnapshotError("parent_unknown") from None
    return current / path.name, tuple(observations)


def _verify_private_config_target(path: Path, snapshot) -> None:
    """Bind the original spelling to the same no-link snapshot, read-only."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not snapshot.exists:
            return
        raise SecureSnapshotError("target_changed") from None
    except OSError:
        raise SecureSnapshotError("target_unknown") from None
    if not snapshot.exists or not stat.S_ISREG(info.st_mode):
        raise SecureSnapshotError("target_changed")
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if identity != snapshot.identity:
        raise SecureSnapshotError("target_changed")


def _capture(path: Path, *, private: bool):
    reader = mac_private_protection if private else _runtime_protection
    for attempt in range(4):
        reader_errors = []

        def checked_reader(target, info):
            try:
                return reader(target, info)
            except SecureSnapshotError as exc:
                reader_errors.append(exc.code)
                raise

        try:
            if private and ".." in path.parts:
                canonical, before = _walk_private_config_parents(path)
                result = capture_target(canonical, authorized_root="/",
                                        protection_reader=checked_reader,
                                        max_bytes=2 * 1024 * 1024)
                _verify_private_config_target(path, result)
                after_path, after = _walk_private_config_parents(path)
                if canonical != after_path or before != after:
                    raise SecureSnapshotError("parent_changed")
                _verify_private_config_target(path, result)
                return _PrivateTraversalSnapshot(result, before)
            return capture_target(path, authorized_root="/",
                                  protection_reader=checked_reader,
                                  max_bytes=2 * 1024 * 1024)
        except SecureSnapshotError as exc:
            transient = (exc.code in ("parent_changed", "target_changed")
                         or exc.code == "protection_unknown"
                         and reader_errors in (["identity_changed"], ["acl_changed"]))
            if not transient or attempt == 3:
                raise PreflightError("config_unverified",
                                     "Launch configuration protection unavailable") from None
            time.sleep(0.01)


def _check_input_conflicts(snapshot, inputs) -> None:
    identities = snapshot.as_dict()
    state = Path(identities["state_dir"].canonical)
    try:
        for source in inputs:
            source = Path(source)
            canonical = source.resolve(strict=True)
            if canonical == state or state in canonical.parents:
                raise PreflightError("config_conflict", "Configuration overlaps fixed state")
            for key in PATH_KEYS:
                target = identities[key]
                if canonical == Path(target.canonical):
                    raise PreflightError("config_conflict", "Configuration overlaps runtime target")
                if target.exists and os.path.samefile(source, target.canonical):
                    raise PreflightError("config_conflict", "Configuration overlaps runtime target")
    except PreflightError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise PreflightError("config_unverified", "Configuration identity unavailable") from None


def _check_legacy_observations(snapshot) -> None:
    ids = snapshot.as_dict()
    # An existing old pid or mkdir-style starter lock is not proof of death.
    if ids["pid"].exists or ids["start_lock"].exists:
        raise PreflightError("legacy_state_unknown", "Legacy startup state is unknown")
    observed = []
    verified_parents = set()
    try:
        for key in PATH_KEYS:
            ident = ids[key]
            target = Path(ident.canonical)
            parent = target.parent
            if parent.resolve(strict=True) != parent:
                raise PreflightError("legacy_state_unknown", "Runtime parent is unknown")
            for part in (parent,):
                if part not in verified_parents:
                    _read_retry(lambda: _verify_directory(part, private=False))
                    verified_parents.add(part)
            if not ident.exists:
                observed.append((key, None))
                continue
            before = target.lstat()
            # In particular, a local unlinked ledger is a prerequisite, not
            # proof that another installation does not share it.
            _runtime_protection(str(target), before)
            after = target.lstat()
            marker = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                                 st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if marker(before) != marker(after):
                raise PreflightError("legacy_state_unknown", "Runtime target changed")
            observed.append((key, marker(after)))
        return tuple(observed)
    except PreflightError:
        raise
    except (OSError, ValueError, RuntimeError, SecureSnapshotError):
        raise PreflightError("legacy_state_unknown", "Runtime protection unavailable") from None


def _require_same_terminal(stdin, stdout) -> None:
    try:
        infd, outfd = stdin.fileno(), stdout.fileno()
        ins, outs = os.fstat(infd), os.fstat(outfd)
        if (not os.isatty(infd) or not os.isatty(outfd)
                or not stat.S_ISCHR(ins.st_mode) or not stat.S_ISCHR(outs.st_mode)
                or (ins.st_dev, ins.st_rdev) != (outs.st_dev, outs.st_rdev)):
            raise PreflightError("initialization_required", "Matching TTY required")
    except PreflightError:
        raise
    except (OSError, AttributeError, ValueError, TypeError):
        raise PreflightError("initialization_required", "Matching TTY required") from None


@contextlib.contextmanager
def _bound_listener(listen):
    host, port = listen["host"], listen["port"]
    family, bound, _ = _endpoint(host, port)
    try:
        if ipaddress.ip_address(host).is_unspecified:
            bound = "0.0.0.0" if family == socket.AF_INET else "::"
    except ValueError:
        pass
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if host == "::" else 1)
        listener.bind((bound, port))
        listener.listen(5)
        yield listener


@contextlib.contextmanager
def locked_observation(root: str | os.PathLike[str],
                       runtime_paths: Mapping[str, str],
                       config_path: str | os.PathLike[str] | None = None, *,
                       recovery: bool = False):
    """Observe the fixed state directory under control.lock.

    No implicit initialization or fallback path is performed. Creation of the
    fixed state directory and lock use protected preflight; no installation
    record is created without interactive confirmation.
    This context does not implement the InstallationAuthority protocol.
    """
    if sys.platform != "darwin":
        raise PreflightError("protection_unavailable", "Control lock unavailable")
    if not isinstance(runtime_paths, Mapping) or set(runtime_paths) != set(PATH_KEYS):
        raise PreflightError("invalid_paths", "Runtime paths are incomplete")
    try:
        project = Path(root)
        if not project.is_absolute() or project != project.resolve(strict=True):
            raise PreflightError("invalid_root", "Installation root is not canonical")
        snapshot = inspect_runtime_identities(project, runtime_paths)
        state = project / ".runtime"
        config = None if config_path is None else os.fspath(config_path)
        if config is not None:
            if not isinstance(config, str) or not os.path.isabs(config):
                raise PreflightError("config_unverified", "Configuration path is not absolute")
            if not recovery:
                _capture_business(config)
                runtime = project / "config" / "runtime_paths.json"
                _capture(runtime, private=False)
                _check_input_conflicts(snapshot, (config, runtime))
        if recovery and not snapshot.as_dict()["state_dir"].exists:
            raise PreflightError("records_inconsistent", "Recovery records unavailable")
        if not snapshot.as_dict()["state_dir"].exists:
            _read_retry(lambda: _verify_directory(project, private=False))
            state.mkdir(mode=0o700)
            snapshot = inspect_runtime_identities(project, runtime_paths)
            if config is not None and not recovery:
                _check_input_conflicts(snapshot, (config, runtime))
        _read_retry(lambda: verify_record_directory(state))
        with acquire_lock(snapshot, "control.lock", create=True,
                          access_verifier=verify_local_access) as held:
            observation = InstallationObservation(project, runtime_paths, held, config)
            observation._check()
            yield observation
            observation._check()
    except PreflightError:
        raise
    except (OSError, ValueError, TypeError):
        raise PreflightError("locks_unverified", "Installation observation unavailable") from None


class _InitializationLease:
    """Protocol adapter for testing initialization; active authority is absent."""

    def __init__(self, observation: InstallationObservation):
        self._observation = observation

    def installation_state(self):
        return self._observation.installation_state()

    def initialize_interactively(self, stdin, stdout):
        return self._observation.initialize_interactively(stdin, stdout)

    def records(self):
        return self._observation.records()

    @staticmethod
    def _active_unverified():
        raise ControlError("installation_identity_unknown", "no signal or credential sent")

    def verify_active(self, instance):
        return self._active_unverified()

    def assert_startable(self):
        # Do not spawn a child that this adapter cannot later verify or stop.
        return self._active_unverified()

    def assert_stopped(self, instance):
        return self._active_unverified()

    def admin_token(self):
        return self._active_unverified()


class PartialMacInstallationAuthority:
    """Explicitly incomplete adapter; never use as the dispatcher default.

    This supports the TTY initialization protocol and protected records only.
    It deliberately cannot authorize a running process, a detached start,
    a signal or a token. It exists so those initialization steps can be tested
    through MacControl without pretending the missing OS proof is implemented.
    """

    @contextlib.contextmanager
    def locked(self, root: Path, config_path: Path):
        project = Path(root)
        runtime = project / "config" / "runtime_paths.json"
        captured = _capture(runtime, private=False)
        if not captured.exists:
            raise PreflightError("config_unverified", "Runtime configuration unavailable")
        try:
            raw = json.loads(captured.contents.decode("utf-8"))
            parsed = parse_runtime_paths(raw, project_root=project,
                                         local_appdata=os.environ.get("LOCALAPPDATA"))
            paths = {key: str(parsed[key]) for key in PATH_KEYS}
        except (UnicodeError, ValueError, TypeError, KeyError):
            raise PreflightError("config_unverified", "Runtime configuration unavailable") from None
        with locked_observation(project, paths, config_path) as observation:
            if _capture(runtime, private=False) != captured:
                raise PreflightError("config_changed", "Runtime configuration changed")
            yield _InitializationLease(observation)


class MacAuthorityLease(_InitializationLease):
    """Pragmatic process evidence; does not claim flock-holder attribution."""

    def __init__(self, observation):
        super().__init__(observation)
        self._spawn = None
        self._verified = None
        self._launch_baseline = None
        self._stopping = False
        self._restart_baseline = None
        self._restart_records = None
        self._launch_config_pair = None
        self._token_config_pair = None

    @contextlib.contextmanager
    def stopping(self):
        previous = self._stopping
        self._stopping = True
        try:
            yield
        finally:
            self._stopping = previous
            self._verified = None

    def watch_start(self, pid, nonce):
        if type(pid) is not int or pid <= 1 or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise ControlError("startup_identity_unknown")
        self._spawn = (pid, nonce)

    def _configuration(self, env_port):
        obs = self._observation
        obs._check()
        config = _capture_business(obs._config_path)
        runtime_path = obs._root / "config" / "runtime_paths.json"
        runtime = _capture(runtime_path, private=False)
        if not config.exists or not runtime.exists:
            raise PreflightError("config_unverified", "Configuration unavailable")
        try:
            parsed = parse_runtime_paths(json.loads(runtime.contents.decode("utf-8")),
                                         project_root=obs._root,
                                         local_appdata=os.environ.get("LOCALAPPDATA"))
            paths = {key: str(parsed[key]) for key in PATH_KEYS}
            if paths != obs._paths:
                raise PreflightError("runtime_changed", "Runtime configuration changed")
            listen = resolve_listen(obs._config_path, env_port)
            snapshot = inspect_runtime_identities(obs._root, paths)
            _check_input_conflicts(snapshot, (obs._config_path, runtime_path))
            _check_restart_targets(obs._root, obs._config_path, snapshot)
            _verify_business(config)
            if (config != _capture_business(obs._config_path)
                    or runtime != _capture(runtime_path, private=False)):
                raise PreflightError("config_changed", "Configuration changed")
            return config, runtime, listen, snapshot
        except PreflightError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, KeyError):
            raise PreflightError("config_unverified", "Configuration unavailable") from None

    def _launch(self, pid, start, executable, nonce):
        obs = self._observation
        entry = obs._root / "core" / "cli" / "model_proxy.py"
        snapshot = _capture(entry, private=False)
        if not snapshot.exists:
            raise ControlError("service_entry_unavailable")
        args, env = platform_process.darwin_launch_evidence(pid, start, executable)
        try:
            image = Path(executable)
            python_image = (re.fullmatch(r"python(?:3(?:\.\d+)?)?", image.name)
                            or image.name == "Python"
                            and image.parts[-4:] == ("Python.app", "Contents", "MacOS", "Python")
                            and "Python.framework" in image.parts)
            entry_arg = args[-1] if (len(args) == 2 or len(args) == 3 and args[1] == "-B") else ""
            if (not entry_arg or not os.path.isabs(args[0]) or not os.path.isabs(entry_arg)
                    or Path(entry_arg).resolve(strict=True) != entry
                    or os.path.realpath(args[0]) != os.path.realpath(executable)
                    or not python_image):
                raise ControlError("installation_identity_unknown")
            effective = env.get("MODEL_PROXY_CONFIG") or str(
                obs._root / "config" / "model_proxy_config.json")
            if not os.path.isabs(effective):
                raise ControlError("installation_identity_unknown")
            if self._stopping and self._restart_baseline is None:
                # Plain off is scoped by the protected installation/instance
                # records, not a mutable business path. Records do not preserve
                # the original A/B config association; resolving either path
                # after a link is removed would guess it and break recovery.
                # This branch grants no credential or restart authority.
                self._launch_config_pair = None
            else:
                service = _capture_business(effective)
                cli = _capture_business(obs._config_path)
                _same_business(service, cli)
                self._launch_config_pair = (service, cli)
            if "MODEL_PROXY_START_NONCE" in env and env["MODEL_PROXY_START_NONCE"] != nonce:
                raise ControlError("installation_identity_unknown")
            if snapshot != _capture(entry, private=False):
                raise ControlError("installation_identity_unknown")
            return env
        except (OSError, ValueError, RuntimeError):
            raise ControlError("installation_identity_unknown") from None

    def records(self):
        try:
            return self._observation.records()
        except PreflightError as exc:
            if self.retry_startup_observation(exc) is not True:
                raise
            raise StartupPending() from None

    def retry_startup_observation(self, exc):
        """Permit only waiting for our own child; no action is authorized."""
        if self._spawn is None or not isinstance(exc, PreflightError) or exc.code not in (
                "records_inconsistent", "records_changed", "record_changed", "protection_changed"):
            return False
        pid, nonce = self._spawn
        start, executable = process_start_identity(pid), process_executable(pid)
        if start is None or executable is None:
            raise ControlError("startup_identity_unknown")
        env = self._launch(pid, start, executable, nonce)
        if env.get("MODEL_PROXY_START_NONCE") != nonce:
            raise ControlError("startup_identity_unknown")
        # A later iteration must re-establish all ACL, record and PID checks
        # before reporting success or sending anything. This only sleeps.
        return True

    def _assert_running_anchor(self, instance):
        """Require a real flock conflict on the verified anchor, not its owner.

        F_GETLKPID is only a read-only early rejection, never positive proof:
        a POSIX record lock can also conflict. A nonblocking flock conflict on
        the same verified inode is additionally required. If the anchor was
        released between probes, a briefly acquired probe is immediately
        unlocked and the result is rejected; it can race a new starter.
        """
        obs = self._observation
        root, paths = obs._root, instance.active["runtime_paths"]
        anchor = root / ".runtime" / "anchor.lock"
        try:
            before = inspect_runtime_identities(root, paths).as_dict()["anchor.lock"]
            expected = instance.active["locks"]["anchor"]
            if (not before.exists or before.canonical != str(anchor)
                    or expected != {"device": before.device, "inode": before.inode}
                    or not verify_local_access(anchor, "file")):
                raise ControlError("anchor_unverified")
            verify_record_directory(root / ".runtime")
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = os.open(anchor, flags)
            try:
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or opened.st_uid != os.geteuid() or opened.st_mode & 0o077
                        or opened.st_mode & 0o600 != 0o600
                        or (opened.st_dev, opened.st_ino) != (before.device, before.inode)):
                    raise ControlError("anchor_unverified")
                verify_local_filesystem_fd(fd)
                verify_no_extended_acl(fd)
                if observe_lock_conflict(fd, candidate_pid=instance.pid).blocked is not True:
                    raise ControlError("anchor_unverified")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise ControlError("anchor_unverified") from None
                else:
                    # Do not keep a probe lock or count it as service evidence.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    raise ControlError("anchor_unverified")
                after = anchor.lstat()
                final = os.fstat(fd)
                if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                        or (after.st_dev, after.st_ino) != (before.device, before.inode)
                        or (final.st_dev, final.st_ino, final.st_mode, final.st_uid, final.st_nlink)
                        != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_nlink)
                        or not verify_local_access(anchor, "file")):
                    raise ControlError("anchor_unverified")
                verify_record_directory(root / ".runtime")
            finally:
                os.close(fd)
        except ControlError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, PreflightError):
            raise ControlError("anchor_unverified") from None

    def _assert_stop_locks(self, instance):
        """Verify both recorded lock inodes and observe a conflict on each.

        A conflicting flock probe does not identify its owner or prove the
        conflict came from an exclusive flock. All observations are racy.
        """
        root = self._observation._root
        paths = instance.active["runtime_paths"]
        try:
            before = inspect_runtime_identities(root, paths)
            identity = before.as_dict()["lock"]
            expected = instance.active["locks"]["legacy"]
            target = Path(identity.canonical)
            if (not identity.exists or target != Path(paths["lock"])
                    or expected != {"device": identity.device, "inode": identity.inode}):
                raise ControlError("legacy_lock_unverified")
            self._assert_running_anchor(instance)
            parent_before = _verify_directory(target.parent, private=False)
            if not verify_local_access(target.parent, "directory") or not verify_local_access(target, "file"):
                raise ControlError("legacy_lock_unverified")

            def marker(st):
                if not hasattr(st, "st_flags"):
                    raise ControlError("legacy_lock_unverified")
                return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                        st.st_nlink, st.st_ctime_ns, st.st_flags)

            original = target.lstat()
            if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
                    or original.st_uid != os.geteuid() or original.st_mode & 0o600 != 0o600
                    or (original.st_dev, original.st_ino) != (identity.device, identity.inode)):
                raise ControlError("legacy_lock_unverified")
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = os.open(target, flags)
            try:
                opened = os.fstat(fd)
                if marker(original) != marker(opened):
                    raise ControlError("legacy_lock_unverified")
                verify_local_filesystem_fd(fd)
                verify_no_extended_acl(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise ControlError("legacy_lock_unverified") from None
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    raise ControlError("legacy_lock_unverified")
                verify_no_extended_acl(fd)
                verify_local_filesystem_fd(fd)
                if (marker(os.fstat(fd)) != marker(original)
                        or marker(target.lstat()) != marker(original)
                        or not verify_local_access(target, "file")
                        or not verify_local_access(target.parent, "directory")):
                    raise ControlError("legacy_lock_unverified")
            finally:
                os.close(fd)
            parent_after = _verify_directory(target.parent, private=False)
            parent_marker = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid,
                                        st.st_gid, st.st_flags)
            if parent_marker(parent_before) != parent_marker(parent_after):
                raise ControlError("legacy_lock_unverified")
            if inspect_runtime_identities(root, paths) != before:
                raise ControlError("legacy_lock_unverified")
        except ControlError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, PreflightError):
            raise ControlError("legacy_lock_unverified") from None

    def verify_active(self, instance):
        obs = self._observation
        self._verified = None
        records = obs.record_set()
        if (records is None or records["active"] != instance.active
                or records["last_good_paths"] != instance.last_good):
            raise ControlError("installation_identity_unknown")
        if self._stopping and self._restart_records is not None and records != self._restart_records:
            raise ControlError("instance_changed", "no signal or credential sent")
        env = self._launch(instance.pid, instance.start, instance.executable, instance.nonce)
        launch_pair = self._launch_config_pair
        if (self._spawn is not None and self._spawn[0] == instance.pid
                and (self._spawn[1] != instance.nonce
                     or env.get("MODEL_PROXY_START_NONCE") != instance.nonce)):
            raise ControlError("startup_identity_unknown")
        # Plain off is recovery from the protected last-good set: current
        # mutable files may be missing or malformed and grant no authority.
        # Restart and all credential-bearing operations retain full preflight.
        if not self._stopping or self._restart_baseline is not None:
            config, runtime, listen, snapshot = self._configuration(env.get("MODEL_PROXY_PORT"))
            if launch_pair is None or config != launch_pair[1]:
                raise ControlError("installation_identity_unknown")
            _same_business(*launch_pair)
            _check_restart_targets(obs._root, launch_pair[0].evidence.source_path, snapshot)
            if self._stopping:
                current = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
                if current[0] != config or current[1] != runtime:
                    raise ControlError("installation_identity_unknown")
                if current[:3] != self._restart_baseline:
                    raise ControlError("start_state_changed")
            else:
                identities = snapshot.as_dict()
                if ({key: identities[key].canonical for key in PATH_KEYS} != instance.active["runtime_paths"]
                        or not MacControl._same_listen(instance, listen)):
                    raise ControlError("installation_identity_unknown")
            if config != _capture_business(obs._config_path) or runtime != _capture(
                    obs._root / "config" / "runtime_paths.json", private=False):
                raise ControlError("installation_identity_unknown")
        platform_process.check_identity(instance.pid, instance.start, instance.executable)
        if not self._stopping:
            self._assert_running_anchor(instance)
        if obs.record_set() != records:
            raise ControlError("installation_identity_unknown")
        if self._stopping and self._restart_baseline is not None:
            # Repeat these point-in-time checks near each signal-bound check.
            _check_restart_targets(obs._root, obs._config_path, current[-1])
            _check_restart_log(current[-1])
            self._preflight_restart_ledger(current, records)
        # Refresh the launch evidence after potentially blocking file and
        # record checks, so the caller's final pre-send recheck ends near send.
        if self._launch(instance.pid, instance.start, instance.executable, instance.nonce) != env:
            raise ControlError("installation_identity_unknown")
        if launch_pair is not None:
            if self._launch_config_pair != launch_pair:
                raise ControlError("installation_identity_unknown")
            _same_business(*launch_pair)
        if self._stopping:
            # Both file identities and current conflicts are rechecked near
            # every signal-bound check, without consulting mutable config.
            self._assert_stop_locks(instance)
            if obs.record_set() != records:
                raise ControlError("installation_identity_unknown")
            platform_process.check_identity(instance.pid, instance.start, instance.executable)
        self._verified = instance
        return True

    def _locks_free(self, expected_records):
        obs = self._observation
        before = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
        snapshot = before[-1]
        with acquire_lock(snapshot, "anchor.lock", create=True,
                          access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True,
                              access_verifier=verify_local_access, anchor=anchor) as legacy:
                verify_held_instance_locks(anchor, legacy)
                if obs.record_set() != expected_records:
                    raise ControlError("start_state_changed")
                after = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
                if before[:3] != after[:3] or after[-1] != legacy.snapshot:
                    raise ControlError("start_state_changed")
                active = expected_records["active"]
                if active is not None:
                    ids = legacy.snapshot.as_dict()
                    for name, role in (("anchor", "anchor.lock"), ("legacy", "lock")):
                        if active["locks"][name] != {"device": ids[role].device,
                                                     "inode": ids[role].inode}:
                            raise ControlError("lock_identity_changed")

    def assert_startable(self):
        obs = self._observation
        self._launch_baseline = None
        records = obs.record_set()
        if records is None:
            raise ControlError("installation_identity_unknown")
        if self._restart_records is not None and records != self._restart_records:
            raise ControlError("start_state_changed")
        entry = obs._root / "core" / "cli" / "model_proxy.py"
        entry_snapshot = _capture(entry, private=False)
        if not entry_snapshot.exists:
            raise ControlError("service_entry_unavailable")
        config, runtime, listen, snapshot = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
        _check_mac_new_listener(listen)
        if self._restart_baseline is not None and (config, runtime, listen) != self._restart_baseline:
            raise ControlError("start_state_changed")
        active = records["active"]
        if active is not None:
            instance = _valid_record(obs._root, active, records["last_good_paths"])
            identities = snapshot.as_dict()
            if {key: identities[key].canonical for key in PATH_KEYS} != active["runtime_paths"]:
                raise ControlError("configuration_migration_required",
                                   "runtime paths changed; migration required; no process started")
            if not platform_process.process_gone(instance.pid, instance.start, instance.executable):
                if self.verify_active(instance) is not True:
                    raise ControlError("installation_identity_unknown")
                return False
        else:
            _check_legacy_observations(snapshot)
        self._locks_free(records)
        if active is not None and not MacControl._same_listen(instance, listen):
            # The old endpoint must be released before a new listener is
            # allowed; port reuse does not prove the old owner exited.
            MacControl._port_free(instance.host, instance.port)
        if (config != _capture_business(obs._config_path)
                or runtime != _capture(obs._root / "config" / "runtime_paths.json", private=False)
                or entry_snapshot != _capture(entry, private=False)):
            raise ControlError("start_state_changed")
        self._launch_baseline = (config, runtime, entry_snapshot, listen, records)
        return True

    def retry_exited_child(self, pid, nonce, returncode):
        """Revalidate everything before at most one retry of an exited child."""
        if (returncode != 1 or self._spawn != (pid, nonce)
                or self._launch_baseline is None):
            return False
        baseline = self._launch_baseline
        records = self._observation.record_set()
        if records != baseline[-1]:
            return False
        active = records["active"] if records is not None else None
        if (isinstance(active, dict)
                and (active.get("pid") == pid or active.get("instance_nonce") == nonce)):
            return False
        if self.assert_startable() is not True:
            return False
        return self._launch_baseline == baseline

    def assert_stopped(self, instance):
        if not platform_process.process_gone(instance.pid, instance.start, instance.executable):
            return False
        records = self._observation.record_set()
        if (records is None or records["active"] != instance.active
                or records["last_good_paths"] != instance.last_good):
            raise ControlError("release_unverified")
        obs = self._observation
        snapshot = inspect_runtime_identities(obs._root, instance.active["runtime_paths"])
        ids = snapshot.as_dict()
        for name, role in (("anchor", "anchor.lock"), ("legacy", "lock")):
            if instance.active["locks"][name] != {"device": ids[role].device,
                                                  "inode": ids[role].inode}:
                raise ControlError("lock_identity_changed")
        with acquire_lock(snapshot, "anchor.lock", access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", access_verifier=verify_local_access,
                              anchor=anchor) as legacy:
                verify_held_instance_locks(anchor, legacy)
                if obs.record_set() != records:
                    raise ControlError("release_unverified")
                if not platform_process.process_gone(instance.pid, instance.start, instance.executable):
                    return False
        return True

    def _preflight_restart_ledger(self, current, records):
        """Read the same installation's ledger; retry only proven instability."""
        from core.config_snapshot import ConfigSnapshotError
        expected = current[:3]
        obs = self._observation
        active = records["active"]
        for attempt in range(4):
            if current[:3] != expected:
                raise ControlError("start_state_changed")
            if obs.record_set() != records:
                raise ControlError("instance_changed", "no signal or credential sent")
            identities = current[-1].as_dict()
            if {key: identities[key].canonical for key in PATH_KEYS} != active["runtime_paths"]:
                raise ControlError("configuration_migration_required",
                                   "runtime paths changed; migration required; no restart attempted")
            try:
                # This reads only the totals role in the supplied preflight;
                # None is accepted only if the server contract proves absence
                # (missing ledger). Content that is present but invalid is
                # deliberately degraded to UNAVAILABLE for a running server so
                # conversation continues and the file is never overwritten --
                # but restart stops a live instance that may hold unpersisted
                # counts in memory. Restart must therefore refuse that case
                # instead of inheriting the serving-time degradation.
                captured = _capture_restart_ledger(current[-1])
                if type(captured).__name__ == "LedgerUnavailable":
                    raise PreflightError(
                        "ledger_unverified",
                        "ledger content is invalid; refusing to stop the running instance")
            except PreflightError as exc:
                cause = exc.__context__
                changed = (exc.code == "ledger_changed"
                           or exc.code == "ledger_unverified"
                           and type(cause) is ConfigSnapshotError
                           and cause.code == "config_unstable")
                if not changed or attempt == 3:
                    raise
            else:
                fresh = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
                if fresh[:3] != expected:
                    raise ControlError("start_state_changed")
                if obs.record_set() != records:
                    raise ControlError("instance_changed", "no signal or credential sent")
                if fresh[-1].as_dict()["totals"] == identities["totals"]:
                    return
                if attempt == 3:
                    raise PreflightError("ledger_changed", "Ledger changed during preflight")
            time.sleep(0.01)
            current = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
        raise PreflightError("ledger_changed", "Ledger changed during preflight")

    def assert_restart_supported(self):
        """Reject runtime drift and preflight an independently bindable new port."""
        self._restart_baseline = None
        self._restart_records = None
        records = self._observation.record_set()
        if records is None:
            raise ControlError("installation_identity_unknown")
        if records["active"] is None:
            if (records["install"].get("ever_started") is not False
                    or records["last_good_paths"] is not None):
                raise ControlError("installation_identity_unknown")
            self._restart_records = records
            return True
        instance = _valid_record(self._observation._root, records["active"],
                                 records["last_good_paths"])
        config, runtime, listen, snapshot = self._configuration(os.environ.get("MODEL_PROXY_PORT"))
        # This is the new target, not the recorded old endpoint used by off.
        _check_mac_new_listener(listen)
        ids = snapshot.as_dict()
        if {key: ids[key].canonical for key in PATH_KEYS} != instance.active["runtime_paths"]:
            return False
        _validate_restart_business_config(config)
        _check_restart_targets(self._observation._root, self._observation._config_path, snapshot)
        _check_restart_log(snapshot)
        self._preflight_restart_ledger((config, runtime, listen, snapshot), records)
        _endpoint(listen["host"], listen["port"])
        if not MacControl._same_listen(instance, listen):
            overlap = MacControl._listeners_overlap(instance, listen)
            try:
                new_wild = ipaddress.ip_address(listen["host"]).is_unspecified
            except ValueError:
                new_wild = False
            try:
                old_wild = ipaddress.ip_address(instance.host).is_unspecified
            except ValueError:
                old_wild = False
            if overlap and new_wild and not old_wild:
                if not platform_process.process_gone(instance.pid, instance.start, instance.executable):
                    raise ControlError("wildcard_restart_requires_off",
                        "same-port wildcard expansion cannot be preflighted; use off then on")
                MacControl._port_free(listen["host"], listen["port"])
            elif not overlap:
                MacControl._port_free(listen["host"], listen["port"])
        self._restart_baseline = (config, runtime, listen)
        self._restart_records = records
        return True

    def assert_never_started(self):
        records = self._observation.record_set()
        return (records is not None and records == self._restart_records
                and records["install"].get("ever_started") is False
                and records["active"] is None and records["last_good_paths"] is None)

    def admin_token(self):
        self._token_config_pair = None
        if self._stopping or self._verified is None:
            raise ControlError("installation_identity_unknown")
        instance = self._verified
        self.verify_active(instance)
        pair = self._launch_config_pair
        try:
            if pair is None:
                raise ValueError("missing configuration evidence")
            _same_business(*pair)
            value = json.loads(pair[1].contents.decode("utf-8"))["admin_token"]
            if not isinstance(value, str) or not value:
                raise ValueError("invalid token")
            _same_business(*pair)
            self._token_config_pair = pair
            return value
        except (UnicodeError, ValueError, TypeError, KeyError):
            raise ControlError("admin_token_unavailable") from None

    def verify_admin_token(self):
        """Revalidate the exact two snapshots from which the token was read."""
        if self._stopping or self._token_config_pair is None:
            raise ControlError("admin_token_unavailable", "no credential sent")
        _same_business(*self._token_config_pair)
        return True


class MacInstallationAuthority(PartialMacInstallationAuthority):
    """Production Mac policy, with explicit documented process-identity limits."""

    @contextlib.contextmanager
    def locked(self, root, config_path):
        with super().locked(root, config_path) as partial:
            yield MacAuthorityLease(partial._observation)

    @contextlib.contextmanager
    def locked_for_stop(self, root, config_path):
        """Locate an existing generation via protected records, not live config.

        The initial read is only a locator. The identical complete set must be
        reread after acquiring the verified fixed control lock, and again before
        any signal. An existing protected state directory is required; the
        fixed control lock may be created using the usual protected preflight.
        """
        project = Path(root)
        if not project.is_absolute() or project != project.resolve(strict=True):
            raise PreflightError("invalid_root", "Installation root is not canonical")
        records = _read_retry(lambda: read_instance_set(project / ".runtime"))
        if records is None or records["active"] is None:
            raise ControlError("records_missing", "no signal or credential sent")
        paths = records["last_good_paths"]["runtime_paths"]
        with locked_observation(project, paths, config_path, recovery=True) as obs:
            if obs.record_set() != records:
                raise ControlError("instance_changed", "no signal or credential sent")
            _valid_record(project, records["active"], records["last_good_paths"],
                          allow_unavailable_host=True)
            yield MacAuthorityLease(obs)
