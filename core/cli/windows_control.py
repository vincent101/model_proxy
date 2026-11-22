"""Windows lifecycle control with protected locators and OS-checked targets.

Native Windows behavior still requires validation. Same-user impersonation,
handle transfer and the final check/use window are outside these guarantees.
"""
from __future__ import annotations

import ipaddress
import http.client
import io
import errno
import json
import math
import ntpath
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from core import windows_process
from core.runtime_paths import PATH_KEYS


class WindowsControlError(RuntimeError):
    def __init__(self, code: str, *, diagnostic: str | None = None):
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


def _verified_lock_boundary(path, lease):
    """Recover the selected boundary from the still-held lock path evidence.

    This scopes ancestor ACL checks only; it does not authorize a write.
    """
    from core import windows_paths
    try:
        evidence = windows_paths._live_evidence(lease)
        drive, parts = windows_paths._parse(os.fspath(path))
        depth = evidence.boundary_depth
        components = evidence.components
        if (not evidence.exists or evidence.expected_kind != "file"
                or evidence.private is not True or evidence.single_link is not True
                or drive != evidence.drive
                or tuple(item.name for item in components[1:]) != parts
                or type(depth) is not int or not 0 < depth < len(parts)
                or len(components) != len(parts) + 1
                or components[depth].observation.kind != "directory"
                or components[depth].policy != "ancestor"):
            raise ValueError("invalid boundary evidence")
        return evidence.drive + "\\" + "\\".join(
            item.name for item in components[1:depth + 1])
    except (AttributeError, TypeError, ValueError, windows_paths.PathEvidenceError):
        raise WindowsControlError("lock_identity_unknown") from None


@contextmanager
def _windows_evidence_scope(monitor):
    """Do not turn an unknown handle release into later lock authority."""
    from core import platform_lock
    if platform_lock._windows_release_unknown or getattr(monitor, "release_unknown", False) is True:
        platform_lock._windows_release_unknown = True
        raise WindowsControlError("lock_release_unknown")
    try:
        yield
    except BaseException as exc:
        uncertain = (getattr(monitor, "release_unknown", False) is True
                     or getattr(exc, "release_unknown", False) is True
                     or getattr(exc, "code", None) in (
                         "release_unknown", "lock_release_unknown", "close_failed",
                         "resource_release_failed", "handle_close_failed"))
        if uncertain:
            platform_lock._windows_release_unknown = True
            if isinstance(exc, Exception):
                raise WindowsControlError("lock_release_unknown") from None
            try:
                exc.release_unknown = True
            except BaseException:
                pass
        raise
    if getattr(monitor, "release_unknown", False) is True:
        platform_lock._windows_release_unknown = True
        raise WindowsControlError("lock_release_unknown")


# Only fixed, non-sensitive codes emitted by the service are forwarded. Child
# stderr is untrusted: never forward its raw text, paths, credentials or detail.
_STARTUP_DIAGNOSTICS = frozenset({
    "config_path_unknown", "config_path_conflict", "config_state_conflict", "config_unverified",
    "runtime_drift", "runtime_paths_changed", "startup_config_changed", "preflight_changed",
    "previous_process_alive", "previous_process_unknown", "listen_changed",
    "ledger_changed", "ledger_unverified", "records_changed", "records_inconsistent",
    "locks_unverified", "lock_conflict", "lock_release_unknown", "release_unknown",
    "protection_unavailable", "protection_changed", "filesystem_unverified",
    "windows_launch_unverified", "windows_listener_exclusivity_unavailable",
    "windows_security_unverified", "windows_state_directory_unverified",
    "windows_private_creation_unavailable", "windows_log_create_unverified",
    "windows_creation_unknown", "windows_creation_release_unknown", "target_create_unknown",
    "startup_diagnostics_unavailable", "record_cleanup_failed", "unexpected_error",
    "ListenConfigError", "RuntimePathsError", "OSError",
})


def _capture_startup_stderr(stream):
    """Drain an anonymous child pipe; retain at most 8 KiB, never raw-print it."""
    if stream is None or not callable(getattr(stream, "read", None)):
        return None
    data, guard, done = bytearray(), threading.Lock(), threading.Event()
    def drain():
        try:
            read = getattr(stream, "read1", None) or stream.read
            while True:
                chunk = read(4096)
                if not isinstance(chunk, bytes) or not chunk:
                    break
                with guard:
                    data.extend(chunk[:max(0, 8192 - len(data))])
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass
            done.set()
    threading.Thread(target=drain, daemon=True, name="model-proxy-startup-stderr").start()
    return data, guard, done


def _startup_diagnostic(capture):
    if capture is None:
        return None
    data, guard, done = capture
    done.wait(0.5)  # a descendant may still hold the pipe; never wait indefinitely
    with guard:
        text = bytes(data).decode("utf-8", "replace")
    for match in re.finditer(r"(?m)^model_proxy: startup failed \(([A-Za-z0-9_]+)\)(?:$|:|\r)", text):
        code = match.group(1)
        if code in _STARTUP_DIAGNOSTICS:
            return code
    return None


_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT")
    for number in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³")
}


def _local_absolute(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    drive, tail = ntpath.splitdrive(value)
    if (len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha()
            or not tail.startswith("\\") or ntpath.normpath(value) != value
            or value.startswith(("\\\\", "//"))):
        return False
    for part in tail.split("\\")[1:]:
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.split(".", 1)[0].upper() in _RESERVED):
            return False
    return True


def _loopback_endpoint(host: Any, port: Any) -> tuple[int, bytes, int]:
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        raise WindowsControlError("record_endpoint_invalid")
    lowered = host.lower()
    if lowered in ("localhost", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    try:
        address = ipaddress.ip_address(host)
        if not address.is_loopback or getattr(address, "scope_id", None) is not None:
            raise ValueError("not unscoped loopback")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            raise ValueError("mapped address unsupported")
        return (socket.AF_INET if address.version == 4 else socket.AF_INET6, address.packed, port)
    except (TypeError, ValueError):
        raise WindowsControlError("record_endpoint_invalid") from None


def _control_endpoint(host: Any, port: Any) -> tuple[int, bytes, int]:
    """A literal local destination, or a loopback destination for a wildcard.

    This is only a locator. Actual locality and ownership are checked on the
    connected socket before a credential is sent.
    """
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        raise WindowsControlError("record_endpoint_invalid")
    host = {"localhost": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host.lower(), host)
    try:
        address = ipaddress.ip_address(host)
        if (address.is_unspecified or address.is_multicast or getattr(address, "scope_id", None) is not None
                or isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None):
            raise ValueError("unsupported address")
        return (socket.AF_INET if address.version == 4 else socket.AF_INET6, address.packed, port)
    except ValueError:
        raise WindowsControlError("local_endpoint_unverifiable") from None


def _valid_status_response(data):
    """The fields consumed by the shared formatter are required, not defaults."""
    if not (isinstance(data, dict)
            and all(isinstance(data.get(key), list) for key in ("supplies", "routes", "strategies"))
            and all(isinstance(item, dict) for item in data["supplies"])
            and all(isinstance(item, dict) for item in data["strategies"])
            and isinstance(data.get("cooldown"), dict)):
        return False
    for info in data["cooldown"].values():
        # The current server returns {remain, reason}; retain the formatter's
        # older numeric-only representation too, without inventing a zero.
        remain = info.get("remain") if isinstance(info, dict) else info
        if type(remain) not in (int, float) or not math.isfinite(remain) or remain < 0:
            return False
    return True


def _sanitize_status(data):
    """Mask explicit private fields, leaving keys and routing values intact.

    The final rendered output is checked separately for the admin credential.
    client_token remains an existing user-visible routing identifier.
    """
    private = re.compile(r"(?:^|_)(?:admin_token|token|password|passwd|secret|credential|api_key|appkey|authorization)(?:$|_)", re.I)
    identities = {"id", "route_id", "supply_id", "strategy_id", "client_token", "appkey_tail4"}
    def visit(value, path=()):
        if isinstance(value, dict):
            # These maps are keyed by user-provided supply IDs, not schema
            # field names; an ID such as service_secret is not a secret field.
            if path in (("cooldown",), ("stream_health", "supplies")):
                return {k: visit(v, path + ("<id>",)) for k, v in value.items()}
            return {k: ("[REDACTED]" if isinstance(k, str) and k not in identities
                        and private.search(k.replace("-", "_"))
                        else visit(v, path + (k,))) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(item, path + ("[]",)) for item in value]
        return value
    return visit(data)


def _contains_admin_credential(lines, token):
    # Short tokens are checked at their actual rendered identity sources in
    # the formatter, not against ordinary counts, timestamps or hidden data.
    if len(token) < 8:
        return False
    return any(token in line for line in lines)


@dataclass(frozen=True)
class RecordClaim:
    """Validated record values only; not protected-record or launch evidence."""
    root: str = field(repr=False)
    pid: int
    start_ticks: int
    executable: str = field(repr=False)
    nonce: str = field(repr=False)
    install_id: str = field(repr=False)
    endpoint: tuple = field(repr=False)
    runtime_paths: tuple = field(repr=False)
    locks: tuple = field(repr=False)

    @property
    def start(self) -> dict:
        return {"kind": "windows_filetime", "ticks_100ns": self.start_ticks}


def validate_record_claim(root: str, active: Any, last_good: Any) -> RecordClaim:
    """Check schema and cross-record consistency without claiming authenticity."""
    if not _local_absolute(root) or not isinstance(active, dict) or not isinstance(last_good, dict):
        raise WindowsControlError("record_invalid")
    try:
        # Freeze caller-owned mutable structures; this is not authenticity.
        raw = json.dumps([active, last_good], ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        if len(raw) > 131072:
            raise ValueError("record too large")
        active, last_good = json.loads(raw)
    except (TypeError, ValueError, RecursionError, OverflowError, RuntimeError):
        raise WindowsControlError("record_invalid") from None
    common = ("schema", "project_root", "instance_nonce", "listen", "runtime_paths", "install_id")
    if (any(key not in active or key not in last_good or active[key] != last_good[key] for key in common)
            or type(active.get("schema")) is not int or active["schema"] != 2
            or active.get("record") != "active" or last_good.get("record") != "last_good_paths"
            or "trust" in active or "trust" in last_good or active.get("state") != "READY"
            or active.get("project_root") != root):
        raise WindowsControlError("record_invalid")
    nonce, install_id = active.get("instance_nonce"), active.get("install_id")
    if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or not isinstance(install_id, str) or not re.fullmatch(r"[0-9a-f]{32}", install_id)):
        raise WindowsControlError("record_invalid")
    pid, start, executable = active.get("pid"), active.get("process_start"), active.get("executable")
    if (type(pid) is not int or not 1 < pid <= 0xFFFFFFFF or pid == os.getpid()
            or not isinstance(start, dict) or set(start) != {"kind", "ticks_100ns"}
            or start.get("kind") != "windows_filetime" or type(start.get("ticks_100ns")) is not int
            or not 0 < start["ticks_100ns"] <= 0xFFFFFFFFFFFFFFFF or not _local_absolute(executable)):
        raise WindowsControlError("record_invalid")
    listen, paths, locks = active.get("listen"), active.get("runtime_paths"), active.get("locks")
    if (not isinstance(listen, dict) or not isinstance(paths, dict) or set(paths) != set(PATH_KEYS)
            or any(not _local_absolute(value) for value in paths.values())
            or len({ntpath.normcase(value) for value in paths.values()}) != len(PATH_KEYS)
            or not isinstance(locks, dict) or set(locks) != {"anchor", "legacy"}):
        raise WindowsControlError("record_invalid")
    lock_values = []
    for key in ("anchor", "legacy"):
        value = locks[key]
        if (not isinstance(value, dict) or type(value.get("device")) is not int
                or value["device"] < 0 or type(value.get("inode")) is not int or value["inode"] <= 0):
            raise WindowsControlError("record_invalid")
        lock_values.append((key, value["device"], value["inode"]))
    if lock_values[0][1:] == lock_values[1][1:]:
        raise WindowsControlError("record_invalid")
    endpoint = _loopback_endpoint(listen.get("host"), listen.get("port"))
    return RecordClaim(root, pid, start["ticks_100ns"], executable, nonce, install_id,
                       endpoint, tuple((key, paths[key]) for key in PATH_KEYS), tuple(lock_values))


class RecordPeerObservation:
    """Own process evidence; caller owns the socket and authenticates records.

    `revalidate` checks newly supplied records and the same connection, but is
    not a substitute for a future trusted installation/launch/config authority.
    """
    def __init__(self, root: str, claim: RecordClaim, peer: windows_process.PeerLease, sock):
        self._root, self.claim, self._peer, self._sock = root, claim, peer, sock
        self._failed = False
        self._closed = False

    def revalidate(self, active, last_good) -> None:
        if self._failed or self._closed:
            raise WindowsControlError("observation_invalid")
        try:
            if validate_record_claim(self._root, active, last_good) != self.claim:
                raise WindowsControlError("record_changed")
            self._peer.revalidate()
            if validate_record_claim(self._root, active, last_good) != self.claim:
                raise WindowsControlError("record_changed")
            self._peer.revalidate()
            if validate_record_claim(self._root, active, last_good) != self.claim:
                raise WindowsControlError("record_changed")
        except BaseException as exc:
            self._failed = True
            self._closed = True
            failed = False
            try:
                self._sock.close()
            except BaseException:
                failed = True
            try:
                self._peer.close()
            except BaseException:
                failed = True
            if failed:
                raise WindowsControlError("resource_release_unknown") from None
            if isinstance(exc, (WindowsControlError, windows_process.ProcessEvidenceError)):
                raise
            if isinstance(exc, Exception):
                raise WindowsControlError("observation_invalid") from None
            raise

    def close(self) -> None:
        self._closed = True
        self._peer.close()

    def __enter__(self):
        if self._failed or self._closed:
            raise WindowsControlError("observation_invalid")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class WindowsControl:
    """Windows lifecycle with read-only preflight for unstarted instances."""
    def __init__(self, root: str | os.PathLike[str], *, authority: Any = None,
                 config_path: str | os.PathLike[str] | None = None,
                 startup_timeout: float = 15.0, stop_timeout: float = 8.0,
                 request_timeout: float = 5.0, out=None, err=None,
                 terminal_in=None, terminal_out=None):
        try:
            self.root = os.fspath(root)
            self.config_path = (os.fspath(config_path) if config_path is not None else
                                ntpath.join(self.root, "config", "model_proxy_config.json"))
        except TypeError:
            raise WindowsControlError("configuration_invalid") from None
        if not _local_absolute(self.root) or not _local_absolute(self.config_path):
            raise WindowsControlError("configuration_invalid")
        for timeout in (startup_timeout, stop_timeout, request_timeout):
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                raise WindowsControlError("configuration_invalid")
        self.authority = authority  # observations alone cannot authorize control
        self.startup_timeout = float(startup_timeout)
        self.stop_timeout = float(stop_timeout)
        self.request_timeout = float(request_timeout)
        try:
            self.out = out if out is not None else sys.stdout
            self.err = err if err is not None else sys.stderr
        except BaseException:
            raise WindowsControlError("configuration_invalid") from None

    def _unavailable(self, command: str, reason: str) -> int:
        print(f"Error: {command} unavailable: {reason}; no signal or credential sent.", file=self.err)
        return 1

    def _runtime_configuration(self):
        from pathlib import Path
        from core.runtime_paths import read_runtime_paths
        root = Path(self.root)
        paths = read_runtime_paths(root / "config" / "runtime_paths.json",
                                   project_root=root, local_appdata=os.environ.get("LOCALAPPDATA"))
        return paths.map_values(str)

    def _configuration(self):
        from core.listen_config import resolve_listen
        paths = self._runtime_configuration()
        listen = resolve_listen(self.config_path, os.environ.get("MODEL_PROXY_PORT"))
        return paths, listen

    def _current_listen_for_off(self):
        """Best-effort stable current endpoint; never authorizes a signal."""
        from core.listen_config import resolve_listen
        try:
            def marker(value):
                return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                        value.st_mtime_ns, value.st_ctime_ns,
                        getattr(value, "st_file_attributes", 0))
            before = os.stat(self.config_path, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode)
                    or getattr(before, "st_file_attributes", 0) & 0x400):
                return None
            env_port = os.environ.get("MODEL_PROXY_PORT")
            first = resolve_listen(self.config_path, env_port)
            second = resolve_listen(self.config_path, env_port)
            after = os.stat(self.config_path, follow_symlinks=False)
            if (marker(before) != marker(after) or first != second
                    or os.environ.get("MODEL_PROXY_PORT") != env_port):
                return None
            return first
        except Exception:
            return None

    @staticmethod
    def _same_bind_endpoint(first, second):
        """Compare bound addresses, keeping wildcard and loopback distinct."""
        def key(value):
            host, port = value["host"], value["port"]
            if not isinstance(host, str) or type(port) is not int:
                raise ValueError("invalid endpoint")
            if host.lower() == "localhost":
                host = "127.0.0.1"
            address = ipaddress.ip_address(host)
            return address.version, address.packed, port
        try:
            return key(first) == key(second)
        except (KeyError, TypeError, ValueError):
            return False

    def _verify_configuration_endpoint(self, paths, listen):
        latest_paths, latest_listen = self._configuration()
        if latest_paths != paths or not self._same_bind_endpoint(listen, latest_listen):
            raise WindowsControlError("current_configuration_changed")

    def _report_stopped(self, recorded, current):
        if recorded is None:
            print("model_proxy stopped; locks and port verified free for the configured "
                  "bind endpoint (other addresses not checked).", file=self.out)
        elif current is None:
            print("Recorded instance stopped; locks and recorded endpoint verified free. "
                  "Current configuration endpoint UNKNOWN (not checked); other addresses not checked.", file=self.out)
        else:
            print("Recorded instance stopped; locks, recorded endpoint and current "
                  "configuration endpoint verified free; other addresses not checked.", file=self.out)

    def _probe_current_port(self, listen):
        try:
            self._probe_port(listen)
        except WindowsControlError as exc:
            if exc.code == "port_busy":
                raise WindowsControlError("current_port_busy") from None
            raise WindowsControlError("current_port_unverified") from None
        except OSError:
            raise WindowsControlError("current_port_unverified") from None

    @property
    def _state_dir(self):
        return ntpath.join(self.root, ".runtime")

    def _active(self):
        from core.discovery import read_windows_active
        return read_windows_active(self._state_dir)

    @contextmanager
    def _locked(self, paths):
        from core.discovery import inspect_runtime_identities, verify_record_directory
        from core.platform_lock import acquire_lock, LockError
        deadline = time.monotonic() + max(self.startup_timeout, self.stop_timeout)
        while True:
            snapshot = inspect_runtime_identities(self.root, paths, windows_light=True)
            if not snapshot.as_dict()["state_dir"].exists:
                raise WindowsControlError("windows_state_directory_unverified")
            verify_record_directory(self._state_dir)
            try:
                held = acquire_lock(snapshot, "control.lock", create=True)
                break
            except LockError as exc:
                if exc.code != "lock_conflict" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        with held:
            yield held

    def _probe_locks(self, paths):
        from core.discovery import inspect_runtime_identities, verify_held_instance_locks
        from core.platform_lock import acquire_lock
        snapshot = inspect_runtime_identities(self.root, paths, windows_light=True)
        with acquire_lock(snapshot, "anchor.lock", create=True) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor) as legacy:
                verify_held_instance_locks(anchor, legacy)

    def _probe_start_locks(self, paths, held_control):
        """Reuse the control snapshot; check only active/anchor/legacy roles.

        Existing locks are acquired together without creating or truncating
        them. Missing roles remain held absence observations; the spawned
        server independently acquires both instance locks before binding.
        """
        if held_control is None:
            return self._probe_locks(paths)
        from core import platform_lock, platform_windows, windows_paths
        from core.discovery import WindowsPathPreflight, _windows_trusted_boundary
        snapshot = getattr(held_control, "snapshot", None)
        if (not isinstance(snapshot, WindowsPathPreflight)
                or getattr(held_control, "role", None) != "control.lock"
                or getattr(held_control, "closed", True)
                or not hasattr(held_control, "_windows_guard")
                or not hasattr(held_control, "_windows_lease")
                or ntpath.normcase(snapshot.project_root) != ntpath.normcase(self.root)):
            raise WindowsControlError("start_state_unknown")
        identities, evidence = snapshot.as_dict(), snapshot.windows_dict()
        if (set(paths) != set(PATH_KEYS)
                or any(os.fspath(paths[key]) != identities[key].requested for key in PATH_KEYS)):
            raise WindowsControlError("runtime_paths_changed")
        monitor = held_control._windows_lease._backend
        platform_lock._windows_verify_guard(held_control._windows_guard, held_control._windows_lease)
        with _windows_evidence_scope(monitor), ExitStack() as stack:
            checked, guards = [], []
            for role in ("active.json", "anchor.lock", "lock"):
                lease = stack.enter_context(windows_paths.inspect(
                    identities[role].canonical, backend=monitor, kind="file",
                    private=True, single_link=True,
                    trusted_boundary=_windows_trusted_boundary(
                        self.root, identities[role].canonical)))
                if lease.evidence != evidence[role]:
                    raise WindowsControlError("start_state_changed")
                checked.append(lease)
                if role != "active.json" and lease.evidence.exists:
                    boundary = _verified_lock_boundary(identities[role].canonical, lease)
                    guard = stack.enter_context(platform_windows.acquire_guarded_path_lock(
                        identities[role].canonical, create=False, trusted_boundary=boundary))
                    platform_lock._windows_verify_guard(guard, lease)
                    guards.append((guard, lease))
            for guard, lease in guards:
                platform_lock._windows_verify_guard(guard, lease)
            for lease in checked:
                lease.revalidate()
            platform_lock._windows_verify_guard(held_control._windows_guard, held_control._windows_lease)
        if getattr(monitor, "release_unknown", False):
            raise WindowsControlError("release_unknown")

    def _locator_hint(self):
        """A cheap hint only: false never proves that an instance is absent."""
        try:
            return os.path.lexists(ntpath.join(self._state_dir, "active.json"))
        except (OSError, ValueError):
            return True

    def _reject_busy_without_locator(self, listen):
        """A busy port proves occupancy, not who owns it; never connect or send."""
        try:
            self._probe_port(listen)
        except WindowsControlError as exc:
            if exc.code == "port_busy":
                raise WindowsControlError("port_busy_identity_unknown") from None
            raise

    def _idle_readonly(self, paths, listen, *, expected_active=None, held_control=None,
                       current_listen=None, backend=None, lock_factory=None):
        """Verify no active instance without creating or changing any files.

        Missing parents are only observations; absence of fixed state alone is
        never enough. Existing control/instance locks are held non-blockingly,
        legacy PID/start markers must be absent, and the configured port must
        be exclusively bindable. All held path evidence is revalidated.
        """
        from core import windows_paths, platform_windows
        from core.discovery import (_WindowsPathMonitor, _windows_path_backend,
                                    _windows_evidence_contains, _windows_trusted_boundary)
        if set(paths) != set(PATH_KEYS) or any(not _local_absolute(value) for value in paths.values()):
            raise WindowsControlError("runtime_identity_unknown")
        values = [ntpath.normcase(value) for value in paths.values()]
        if len(set(values)) != len(values):
            raise WindowsControlError("runtime_identity_unknown")
        api = _windows_path_backend() if backend is None else backend
        monitor = _WindowsPathMonitor(api)
        locker = platform_windows.acquire_guarded_path_lock if lock_factory is None else lock_factory
        guards, leases = [], {}
        with _windows_evidence_scope(monitor), ExitStack() as stack:
            def inspect(role, path, *, kind="file", private=True, single_link=False,
                        boundary=None):
                lease = windows_paths.inspect(path, backend=monitor, kind=kind,
                                              private=private, single_link=single_link,
                                              trusted_boundary=(boundary or
                                                  _windows_trusted_boundary(self.root, path)))
                leases[role] = stack.enter_context(lease)
                return lease

            state = inspect("state", self._state_dir, kind="directory")
            # The state lease already contains the held, validated project
            # root chain. A missing root would leave a longer missing suffix;
            # avoid a second complete (and very costly) ancestor walk.
            if (not state.evidence.exists
                    and state.evidence.missing != (".runtime",)):
                raise WindowsControlError("runtime_identity_unknown")
            if state.evidence.exists:
                active = inspect("active", ntpath.join(self._state_dir, "active.json"),
                                 single_link=True)
                if active.evidence.exists:
                    if expected_active is None or self._active() != expected_active:
                        raise WindowsControlError("active_record_present")
                elif expected_active is not None:
                    raise WindowsControlError("record_changed")
                inspect("control", ntpath.join(self._state_dir, "control.lock"), single_link=True)
                inspect("anchor", ntpath.join(self._state_dir, "anchor.lock"), single_link=True)
            elif expected_active is not None:
                raise WindowsControlError("record_changed")
            if (held_control is not None
                    and ("control" not in leases or not leases["control"].evidence.exists)):
                raise WindowsControlError("lock_identity_unknown")

            # When all three legacy markers are exact mapped defaults and
            # their runtime directory is absent, one protected absence lease
            # covers all three instead of repeatedly walking the same parents.
            #
            # 这里**不能**只认 ResolvedRuntimePaths 的溯源标记：本方法也会收到
            # 来自 active.json 的 runtime_paths（JSON 反序列化后是普通 dict，
            # 结构上无法携带溯源）。若因此退回逐个 inspect，父目录不存在时
            # 会以 boundary_unavailable 失败，把「legacy 目录尚未创建」误报成
            # 「证据不可用」。替代判据是逐字相等 + 仅作**缺席**观测：三条路径
            # 必须精确等于 LOCALAPPDATA 下的映射默认值，且随后只用于证明该
            # 目录不存在（存在则整组判定失效，回到逐项核验）。
            from core.runtime_paths import _WINDOWS_LEGACY_NAMES
            appdata = os.environ.get("LOCALAPPDATA")
            default_dir = None
            if appdata and _local_absolute(ntpath.normpath(appdata)):
                candidate = ntpath.join(ntpath.normpath(appdata), "model_proxy", "runtime")
                if all(ntpath.normcase(paths[key]) == ntpath.normcase(
                        ntpath.join(candidate, _WINDOWS_LEGACY_NAMES[key]))
                        for key in ("lock", "pid", "start_lock")):
                    default_dir = candidate
            grouped_absence = False
            if default_dir:
                runtime = inspect("default_runtime", default_dir, kind="directory",
                                  boundary=ntpath.normpath(appdata))
                if not runtime.evidence.exists:
                    grouped_absence = True
            if not grouped_absence:
                inspect("lock", paths["lock"], single_link=True)
                inspect("pid", paths["pid"])
                try:
                    inspect("start_lock", paths["start_lock"])
                except windows_paths.PathEvidenceError as exc:
                    if exc.code not in ("invalid_type", "observation_unavailable"):
                        raise
                    inspect("start_lock", paths["start_lock"], kind="directory", private=False)
                if leases["pid"].evidence.exists or leases["start_lock"].evidence.exists:
                    raise WindowsControlError("legacy_state_unknown")

            # The fixed state is a container, not a forbidden parent of the
            # six files. A file may not be the container or contain it.
            for role in ("default_runtime", "lock", "pid", "start_lock"):
                if role in leases:
                    evidence = leases[role].evidence
                    if (_windows_evidence_contains(evidence, state.evidence)
                            or (role == "default_runtime"
                                and _windows_evidence_contains(state.evidence, evidence))):
                        raise WindowsControlError("runtime_identity_unknown")
                    for fixed in ("active", "control", "anchor"):
                        if fixed in leases and (
                                _windows_evidence_contains(evidence, leases[fixed].evidence)
                                or _windows_evidence_contains(leases[fixed].evidence, evidence)):
                            raise WindowsControlError("runtime_identity_unknown")
            locks = [role for role in ("control", "anchor", "lock")
                     if role in leases and leases[role].evidence.exists]
            for i, role in enumerate(locks):
                for other in locks[i + 1:]:
                    left, right = leases[role].evidence, leases[other].evidence
                    if (_windows_evidence_contains(left, right)
                            or _windows_evidence_contains(right, left)):
                        raise WindowsControlError("runtime_identity_unknown")
                path = (paths["lock"] if role == "lock" else
                        ntpath.join(self._state_dir, role + ".lock"))
                borrowed = role == "control" and held_control is not None
                if borrowed:
                    if (getattr(held_control, "role", None) != "control.lock"
                            or getattr(held_control, "closed", True)
                            or not hasattr(held_control, "_windows_guard")):
                        raise WindowsControlError("lock_identity_unknown")
                    guard = held_control._windows_guard
                else:
                    boundary = _verified_lock_boundary(path, leases[role])
                    guard = stack.enter_context(locker(path, create=False, trusted_boundary=boundary))
                if ((borrowed and type(guard.created) is not bool)
                        or (not borrowed and guard.created is not False)):
                    raise WindowsControlError("lock_identity_unknown")
                actual = guard.object_evidence.identity
                observed = leases[role].evidence.components[-1].observation
                if (actual.volume_serial, actual.file_index) != (
                        observed.volume, int.from_bytes(observed.file_id, "big")):
                    raise WindowsControlError("lock_identity_unknown")
                guards.append((guard, borrowed))
            for guard, borrowed in guards:
                guard.revalidate(retain_on_failure=True) if borrowed else guard.revalidate()
            self._probe_port(listen)
            if current_listen is not None and not self._same_bind_endpoint(listen, current_listen):
                self._probe_current_port(current_listen)
            for guard, borrowed in guards:
                guard.revalidate(retain_on_failure=True) if borrowed else guard.revalidate()
            for lease in leases.values():
                lease.revalidate()
            if expected_active is not None and self._active() != expected_active:
                raise WindowsControlError("record_changed")
            if monitor.release_unknown:
                raise WindowsControlError("release_unknown")

    @staticmethod
    def _probe_port(listen):
        host, port = listen["host"], listen["port"]
        family, _, _ = _control_endpoint(host, port)
        if host.lower() == "localhost":
            host = "127.0.0.1"
        if not isinstance(getattr(socket, "SO_EXCLUSIVEADDRUSE", None), int):
            raise WindowsControlError("windows_listener_exclusivity_unavailable")
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if host == "::" else 1)
            try:
                sock.bind((host, port))
                sock.listen(1)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                    raise WindowsControlError("port_busy") from None
                raise WindowsControlError("port_check_unavailable") from None

    def _match_record(self, active, paths, *, require_config=True):
        if (active is None
                or (require_config and
                    ntpath.normcase(active["config"]) != ntpath.normcase(self.config_path))
                or active["runtime_paths"] != paths):
            raise WindowsControlError("record_configuration_changed")

    @staticmethod
    def _match_listener(active, listen):
        expected, actual = listen["host"], active["listen"]["host"]
        if expected.lower() == "localhost":
            expected = "127.0.0.1"
        try:
            same = ipaddress.ip_address(expected) == ipaddress.ip_address(actual)
        except ValueError:
            raise WindowsControlError("listen_identity_unavailable") from None
        if not same or listen["port"] != active["listen"]["port"]:
            raise WindowsControlError("listen_changed")

    def _verify_entry(self, active, revalidate):
        from core.cli.windows_command_line import PowerShellWmiReader, verify_shared_launch
        revalidate()
        observed = PowerShellWmiReader().read_command_line(active["pid"])
        revalidate()
        verify_shared_launch(observed, entry=active["entry"], config=active["config"],
                             port=active["listen"]["port"])
        if self._active() != active:
            raise WindowsControlError("record_changed")
        revalidate()

    def _request(self, active, paths, command):
        from core.windows_secure_files import capture_target
        from core.discovery import _windows_trusted_boundary
        self._match_record(active, paths)
        if active["state"] != "READY":
            raise WindowsControlError("startup_incomplete")
        family, packed, port = _control_endpoint(active["listen"]["host"], active["listen"]["port"])
        host = str(ipaddress.ip_address(packed))
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.request_timeout)
            sock.connect((host, port))
            with windows_process.verify_socket_owner(
                    sock, active["pid"], active["process_start"], active["executable"]) as peer:
                actual = peer.endpoint
                if (actual.family, actual.peer_ip, actual.peer_port) != (family, packed, port):
                    raise WindowsControlError("record_endpoint_changed")
                self._verify_entry(active, peer.revalidate)
                with capture_target(self.config_path, authorized_root=ntpath.dirname(self.config_path),
                                    max_bytes=2 * 1024 * 1024,
                                    trusted_boundary=_windows_trusted_boundary(
                                        self.root, self.config_path)) as snapshot:
                    if not snapshot.exists:
                        raise WindowsControlError("config_unavailable")
                    config = json.loads(snapshot.contents.decode("utf-8"))
                    token = config.get("admin_token") if isinstance(config, dict) else None
                    if (not isinstance(token, str) or not 0 < len(token) <= 4096
                            or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)):
                        raise WindowsControlError("admin_token_unavailable")
                    snapshot.revalidate()
                    self._verify_entry(active, peer.revalidate)
                    snapshot.revalidate()
                    peer.revalidate()
                    method = "GET" if command == "status" else "POST"
                    host_header = f"[::1]:{port}" if family == socket.AF_INET6 else f"127.0.0.1:{port}"
                    request = (f"{method} /model_proxy/{command} HTTP/1.1\r\n"
                               f"Host: {host_header}\r\nX-Proxy-Admin-Token: {token}\r\n"
                               "Connection: close\r\nContent-Length: 0\r\n\r\n").encode("ascii")
                    # Send only on the exact socket whose accepted peer was checked;
                    # no redirects, proxy, reconnection or automatic retry.
                    sock.sendall(request)
                    response = http.client.HTTPResponse(sock)
                    response.begin()
                    data = response.read(2 * 1024 * 1024 + 1)
                    if len(data) > 2 * 1024 * 1024 or response.length not in (None, 0):
                        raise WindowsControlError("response_unknown")
                    result = json.loads(data.decode("utf-8"))
                    if not isinstance(result, dict):
                        raise WindowsControlError("response_unknown")
                    if command == "reload" and (response.status != 200 or result.get("ok") is not True):
                        loaded, cleared = result.get("loaded"), result.get("cooldown_cleared")
                        def flag(value):
                            return "yes" if value is True else "no" if value is False else "unknown"
                        print(f"Reload failed: loaded={flag(loaded)}, cooldown_cleared={flag(cleared)}.",
                              file=self.err)
                        raise WindowsControlError("reload_failed")
                    if response.status != 200:
                        raise WindowsControlError("authentication_or_admin_unavailable")
                    if command == "status":
                        if not _valid_status_response(result):
                            raise WindowsControlError("response_unknown")
                        return _sanitize_status(result), token

    def _display_status(self, active, paths, result):
        from core.cli._format_ops import (_format_status_from_json, _format_status_header,
                                          StatusReferenceConflict)
        try:
            data, token = result
            if not isinstance(token, str) or not token:
                raise WindowsControlError("status_sensitive_conflict")
            lines = _format_status_from_json(data, self.config_path, paths["totals"], paths["log"],
                                             forbidden_reference_token=token)
            start = active.get("process_start", {})
            ticks = start.get("ticks_100ns") if start.get("kind") == "windows_filetime" else None
            started = ticks // 10_000_000 - 11_644_473_600 if type(ticks) is int else None
            if started is not None and started < 0:
                started = None
            header = _format_status_header(active['listen']['port'], active['pid'], started)
            lines = [header, *lines]
            if any(not isinstance(line, str) for line in lines):
                raise ValueError("invalid status formatting")
            if _contains_admin_credential(lines, token):
                raise WindowsControlError("status_sensitive_conflict")
        except WindowsControlError:
            raise
        except StatusReferenceConflict:
            raise WindowsControlError("status_sensitive_conflict") from None
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError, RuntimeError):
            raise WindowsControlError("status_failed") from None
        return lines

    def _on_locked(self, paths, listen, *, held_control=None):
        snapshot = getattr(held_control, "snapshot", None)
        # A protected control snapshot may prove the locator was absent at
        # preflight; _probe_start_locks checks that observation again before
        # spawn. Otherwise retain the protected record read.
        if snapshot is not None and not snapshot.as_dict()["active.json"].exists:
            active = None
        else:
            active = self._active()
        if active is not None:
            state = windows_process.recorded_process_state(
                active["pid"], active["process_start"], active["executable"])
            if state == "running":
                self._match_listener(active, listen)
                self._request(active, paths, "status")
                print("model_proxy already running and verified.", file=self.out)
                return
            self._match_record(active, paths)
        self._probe_start_locks(paths, held_control)
        self._probe_port(listen)
        entry = ntpath.join(self.root, "core", "cli", "model_proxy.py")
        nonce = uuid.uuid4().hex
        env = dict(os.environ, MODEL_PROXY_START_NONCE=nonce,
                   MODEL_PROXY_STARTUP_DIAGNOSTIC_PIPE="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        child = subprocess.Popen([sys.executable, "-B", entry, "--config", self.config_path,
                                  "--port", str(listen["port"])],
                                 cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 close_fds=True, creationflags=flags)
        capture = _capture_startup_stderr(getattr(child, "stderr", None))
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise WindowsControlError("startup_failed", diagnostic=_startup_diagnostic(capture))
            # This is only a cheap polling hint, never a readiness proof.
            candidate = self._active() if self._locator_hint() else None
            if candidate is not None and candidate["pid"] == child.pid and candidate["instance_nonce"] == nonce:
                if candidate["state"] == "READY":
                    self._match_listener(candidate, listen)
                    self._request(candidate, paths, "status")
                    print("model_proxy started and verified.", file=self.out)
                    return
            time.sleep(0.1)
        raise WindowsControlError("startup_unconfirmed")

    def _off_locked(self, paths, listen, *, held_control=None, current_listen=None,
                    verify_current=False):
        active = self._active()
        if active is not None:
            # Stop is anchored by the actual recorded argv/config and held
            # process handle, not by a currently selected or readable config.
            self._match_record(active, paths, require_config=False)
            state = windows_process.recorded_process_state(
                active["pid"], active["process_start"], active["executable"])
            if state == "running":
                with windows_process.open_verified_process(
                        active["pid"], active["process_start"], active["executable"],
                        terminate=True) as process:
                    def verify_entry(_identity):
                        self._verify_entry(active, process.revalidate)
                        return True
                    process.terminate_checked(verify_entry, timeout=self.stop_timeout)
                    # Record this irreversible fact before __exit__ closes the
                    # same handle, which may itself fail. Public command output
                    # remains buffered until the control scope is released.
                    print("Recorded model_proxy process exited; Windows forced stop may "
                          "interrupt in-flight requests.", file=self.out)
        if active is None and listen is None:
            raise WindowsControlError("not_running_unknown")
        deadline = time.monotonic() + self.stop_timeout
        while True:
            try:
                self._idle_readonly(paths, active["listen"] if active is not None else listen,
                                    expected_active=active, held_control=held_control,
                                    current_listen=current_listen if active is not None else None)
                break
            except RuntimeError as exc:
                # Wait only for actual contention; unsafe ACLs, changed
                # records and other unknown evidence fail immediately.
                if getattr(exc, "code", None) not in ("port_busy", "lock_conflict"):
                    raise
                if time.monotonic() >= deadline:
                    raise WindowsControlError("release_unverified") from None
                time.sleep(0.1)
        if verify_current and current_listen is not None:
            latest = self._current_listen_for_off()
            if latest is None or not self._same_bind_endpoint(current_listen, latest):
                raise WindowsControlError("current_configuration_changed")
        self._report_stopped(active["listen"] if active is not None else None,
                             current_listen if active is not None else listen)

    def _run_lifecycle(self, command):
        # Publish success only after control-lock/handle context exits.  Keep
        # the one irreversible stop fact visible on failure, but not as a
        # claim that lock or endpoint release succeeded.
        original = self.out
        pending = io.StringIO()
        self.out = pending
        try:
            result = self._run_lifecycle_impl(command)
        finally:
            self.out = original
        output = pending.getvalue()
        if result == 0:
            try:
                if output:
                    print(output, end="", file=original)
            except Exception:
                print(f"Error: {command}: output_failed; state may be unknown.", file=self.err)
                return 1
        elif "Recorded model_proxy process exited;" in output:
            print("Recorded model_proxy process exited; later checks or release failed; "
                  "Windows forced stop may interrupt in-flight requests.", file=self.err)
        return result

    def _run_lifecycle_impl(self, command):
        if os.name != "nt":
            return self._unavailable(command, "Windows control is unavailable")
        try:
            observed = None
            if command == "status":
                paths, listen = self._configuration()
                if not self._locator_hint():
                    # Occupancy is useful even when an inaccessible directory
                    # prevents identifying its owner. It never grants control.
                    self._reject_busy_without_locator(listen)
                    self._idle_readonly(paths, listen)
                    self._verify_configuration_endpoint(paths, listen)
                    self._report_stopped(None, listen)
                    return 0
                active = self._active()
                if active is None:
                    self._reject_busy_without_locator(listen)
                    self._idle_readonly(paths, listen)
                    self._verify_configuration_endpoint(paths, listen)
                    self._report_stopped(None, listen)
                    return 0
                self._match_record(active, paths)
                state = windows_process.recorded_process_state(
                    active["pid"], active["process_start"], active["executable"])
                if state != "running":
                    self._idle_readonly(paths, active["listen"], expected_active=active,
                                        current_listen=listen)
                    self._verify_configuration_endpoint(paths, listen)
                    self._report_stopped(active["listen"], listen)
                    return 0
                # Read-only status does not create a control lock. The actual
                # peer, entry and active record are checked on the same socket.
                changed_endpoint = not self._same_bind_endpoint(active["listen"], listen)
                if changed_endpoint:
                    self._probe_current_port(listen)
                result = self._request(active, paths, "status")
                lines = self._display_status(active, paths, result)
                self._verify_configuration_endpoint(paths, listen)
                if changed_endpoint:
                    lines.append("model_proxy running at the recorded endpoint; the current "
                                 "configuration endpoint differs and was verified free.")
                else:
                    lines.append("model_proxy running and verified.")
                if _contains_admin_credential(lines, result[1]):
                    raise WindowsControlError("status_sensitive_conflict")
                print("\n".join(lines), file=self.out)
                return 0
            if command == "off":
                # The locator is only a hint until it is reread under the
                # control lock and the process/entry is checked on a handle.
                # In particular, do not parse a broken business configuration.
                if not self._locator_hint():
                    paths, listen = self._configuration()
                    self._reject_busy_without_locator(listen)
                    self._idle_readonly(paths, listen)
                    self._verify_configuration_endpoint(paths, listen)
                    self._report_stopped(None, listen)
                    return 0
                observed = self._active()
                if observed is not None:
                    paths, listen = dict(observed["runtime_paths"]), None
                    current_listen = self._current_listen_for_off()
                    # A direct service may have no control.lock. Do not create
                    # it solely on an unauthenticated locator: first pin the
                    # actual process and check its observed entry.
                    if not os.path.lexists(ntpath.join(self._state_dir, "control.lock")):
                        state = windows_process.recorded_process_state(
                            observed["pid"], observed["process_start"], observed["executable"])
                        if state != "running":
                            self._idle_readonly(paths, observed["listen"], expected_active=observed,
                                                current_listen=current_listen)
                            if current_listen is not None:
                                latest = self._current_listen_for_off()
                                if latest is None or not self._same_bind_endpoint(current_listen, latest):
                                    raise WindowsControlError("current_configuration_changed")
                            self._report_stopped(observed["listen"], current_listen)
                            return 0
                        with windows_process.open_verified_process(
                                observed["pid"], observed["process_start"], observed["executable"]) as process:
                            self._verify_entry(observed, process.revalidate)
                else:
                    paths, listen = self._configuration()
                    self._reject_busy_without_locator(listen)
                    self._idle_readonly(paths, listen)
                    self._verify_configuration_endpoint(paths, listen)
                    self._report_stopped(None, listen)
                    return 0
            else:
                paths, listen = self._configuration()
                if command == "on":
                    # A missing locator is not proof of absence, but an
                    # already-busy port is a safe early UNKNOWN: do not create
                    # directories or enter repeated path preflights first.
                    if not self._locator_hint():
                        self._reject_busy_without_locator(listen)
                    from core.discovery import (prepare_windows_runtime_directories,
                                                windows_initial_missing_parents)
                    from core.server import _windows_check_config_targets
                    _windows_check_config_targets(
                        paths, self.config_path, project_root=self.root,
                        initial_missing_parents=windows_initial_missing_parents(
                            paths, os.environ.get("LOCALAPPDATA")))
                    prepare_windows_runtime_directories(self.root, paths, os.environ.get("LOCALAPPDATA"))
                elif command == "restart" and not self._locator_hint():
                    self._reject_busy_without_locator(listen)
                    self._idle_readonly(paths, listen)
                    if self._configuration() != (paths, listen):
                        raise WindowsControlError("configuration_changed")
                    from core.discovery import (prepare_windows_runtime_directories,
                                                windows_initial_missing_parents)
                    from core.server import _windows_check_config_targets
                    _windows_check_config_targets(
                        paths, self.config_path, project_root=self.root,
                        initial_missing_parents=windows_initial_missing_parents(
                            paths, os.environ.get("LOCALAPPDATA")))
                    prepare_windows_runtime_directories(self.root, paths, os.environ.get("LOCALAPPDATA"))
                    with self._locked(paths) as held_control:
                        if self._active() is not None or self._configuration() != (paths, listen):
                            raise WindowsControlError("configuration_changed")
                        self._on_locked(paths, listen, held_control=held_control)
                    return 0
            with self._locked(paths) as held_control:
                if command == "off" and self._active() != observed:
                    raise WindowsControlError("record_changed")
                if command == "on":
                    self._on_locked(paths, listen, held_control=held_control)
                elif command == "off":
                    self._off_locked(paths, listen, held_control=held_control,
                                     current_listen=current_listen, verify_current=True)
                elif command == "restart":
                    active = self._active()
                    if active is not None:
                        self._match_record(active, paths)
                        self._match_listener(active, listen)
                    if self._configuration() != (paths, listen):
                        raise WindowsControlError("configuration_changed")
                    self._off_locked(paths, listen, held_control=held_control, current_listen=listen)
                    if self._configuration() != (paths, listen):
                        raise WindowsControlError("configuration_changed")
                    self._on_locked(paths, listen, held_control=held_control)
                else:
                    active = self._active()
                    if active is None:
                        self._probe_locks(paths)
                        self._probe_port(listen)
                        raise WindowsControlError("not_running")
                    self._match_record(active, paths)
                    process_state = windows_process.recorded_process_state(
                        active["pid"], active["process_start"], active["executable"])
                    if process_state != "running":
                        self._probe_locks(paths)
                        self._probe_port(active["listen"])
                        raise WindowsControlError("not_running")
                    self._request(active, paths, command)
                    print("Reload succeeded; configuration loaded and cooldown cleared.", file=self.out)
            return 0
        except Exception as exc:
            code = getattr(exc, "code", "control_failed")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                code = "control_failed"
            diagnostic = getattr(exc, "diagnostic", None)
            detail = (f"; child reported {diagnostic}" if type(diagnostic) is str
                      and diagnostic in _STARTUP_DIAGNOSTICS else "")
            print(f"Error: {command}: {code}{detail}; state may be unknown.", file=self.err)
            return 1

    def on(self): return self._run_lifecycle("on")
    def off(self): return self._run_lifecycle("off")
    def restart(self): return self._run_lifecycle("restart")
    def status(self): return self._run_lifecycle("status")

    def _observe_recorded_connection(self, records, read_records) -> bool:
        """Observe an actual connection, never authorize a request or launch."""
        state_dir = ntpath.join(self.root, ".runtime")
        try:
            active, last = records["active"], records["last_good_paths"]
            claim = validate_record_claim(self.root, active, last)
            family, address, port = claim.endpoint
            host = str(ipaddress.ip_address(address))
            endpoint = (host, port) if family == socket.AF_INET else (host, port, 0, 0)
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(min(self.request_timeout, 2.0))
                sock.connect(endpoint)
                with self.observe_peer(sock, active, last, timeout=0.5) as peer:
                    current = read_records(state_dir)
                    if current != records:
                        raise WindowsControlError("record_changed")
                    peer.revalidate(current["active"], current["last_good_paths"])
                    if read_records(state_dir) != records:
                        raise WindowsControlError("record_changed")
                    peer.revalidate(current["active"], current["last_good_paths"])
            return True
        except Exception:
            # No raw record values, paths, socket/OS errors or credentials.
            return False

    def reload(self): return self._run_lifecycle("reload")

    def observe_peer(self, sock: socket.socket, active: Any, last_good: Any, *,
                     backend: Any = None, timeout: float = 2.0, interval: float = 0.05,
                     clock=None, sleep=None) -> RecordPeerObservation:
        """Check a caller-owned connected socket without reading or sending secrets.

        On failure the socket is closed. On success the returned observation
        owns the process handle; the caller still owns and must close the socket.
        Record values remain untrusted until a future real authority validates
        their protected storage, installation, launch entry and effective config.
        """
        peer = None
        try:
            claim = validate_record_claim(self.root, active, last_good)
            options = {"backend": backend, "timeout": timeout, "interval": interval}
            if clock is not None: options["clock"] = clock
            if sleep is not None: options["sleep"] = sleep
            peer = windows_process.verify_socket_owner(sock, claim.pid, claim.start,
                                                        claim.executable, **options)
            actual = peer.endpoint
            if (actual.family, actual.peer_ip, actual.peer_port) != claim.endpoint:
                raise WindowsControlError("record_endpoint_changed")
            if validate_record_claim(self.root, active, last_good) != claim:
                raise WindowsControlError("record_changed")
            peer.revalidate()
            if validate_record_claim(self.root, active, last_good) != claim:
                raise WindowsControlError("record_changed")
            return RecordPeerObservation(self.root, claim, peer, sock)
        except BaseException as exc:
            failed = False
            try:
                sock.close()
            except BaseException:
                failed = True
            if peer is not None:
                try:
                    peer.close()
                except BaseException:
                    failed = True
            if failed:
                raise WindowsControlError("resource_release_unknown") from None
            if isinstance(exc, (WindowsControlError, windows_process.ProcessEvidenceError)):
                raise
            if isinstance(exc, Exception):
                raise WindowsControlError("peer_unverified") from None
            raise
