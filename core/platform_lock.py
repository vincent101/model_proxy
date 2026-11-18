"""Non-blocking instance locks, held until close; lock files are not unlinked.

POSIX fork children close inherited descriptors without unlocking the parent's
open-file description. Windows uses LockFileEx; native behavior is untested.
Filesystem ACLs and hostile same-user replacement are not fully verified.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from pathlib import Path
from typing import Callable

from core.discovery import (PathIdentity, PathPreflight, PreflightError,
                            inspect_runtime_identities, verify_no_extended_acl)
from core.runtime_paths import PATH_KEYS


class LockError(RuntimeError):
    def __init__(self, code: str, *, created: bool | None = False):
        super().__init__(code)
        self.code = code
        # A lock file created before a later failure is never silently deleted.
        self.created = created
        self.release_unknown = code in ("release_unknown", "lock_release_unknown")


# Serialise fork against opening/closing and registering lock descriptors.
_fork_guard = threading.RLock()
_held_locks = set()
_pending_fds = set()
# A Windows release with an unknown result disables further Windows lock
# acquisition in this process. This is not a cross-process persistent freeze.
_windows_release_unknown = False


def _before_fork():
    _fork_guard.acquire()


def _after_fork_parent():
    _fork_guard.release()


def _after_fork_child():
    # Never issue LOCK_UN: parent and child share the open-file description.
    # No per-handle mutex: it may have been held by a vanished thread.
    for held in tuple(_held_locks):
        if not held._closed:
            try:
                os.close(held._fd)
            except OSError:
                pass
            held._closed = True
        # Other threads do not survive fork; their mutex ownership does not
        # either. Child-side inspection/close must never wait for them.
        held._state_lock = threading.RLock()
    _held_locks.clear()
    # Covers fork invoked re-entrantly by a verifier during acquisition.
    for fd in tuple(_pending_fds):
        try:
            os.close(fd)
        except OSError:
            pass
    _pending_fds.clear()
    _fork_guard.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_parent,
                        after_in_child=_after_fork_child)


def _require_verified_fork_safety() -> None:
    if os.name == "posix" and not hasattr(os, "register_at_fork"):
        raise LockError("fork_safety_unavailable")


def verify_local_access(path: Path, kind: str) -> bool:
    """Verify owner/mode and directory or file ACL protection, else reject."""
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return False
        if kind == "directory" and not stat.S_ISDIR(info.st_mode):
            return False
        if kind == "file" and not stat.S_ISREG(info.st_mode):
            return False
        if os.name != "posix" or info.st_uid not in (os.geteuid(), 0):
            return False
        if kind == "directory" and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            return False
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            actual = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
                return False
            if kind == "directory":
                acl_before = verify_no_extended_acl(fd)
                middle = os.fstat(fd)
                acl_after = verify_no_extended_acl(fd)
                final = os.fstat(fd)
                current = path.lstat()
                def marker(st):
                    if not stat.S_ISDIR(st.st_mode) or not hasattr(st, "st_flags") or st.st_nlink < 1:
                        return None
                    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_flags)
                markers = tuple(marker(st) for st in (info, actual, middle, final, current))
                return (acl_before == acl_after and None not in markers
                        and all(item == markers[0] for item in markers[1:]))
            verify_no_extended_acl(fd)
            after = path.lstat()
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_ctime_ns) == (
                after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_ctime_ns)
        finally:
            os.close(fd)
    except (OSError, PreflightError, AttributeError):
        return False


def _lock_fd(fd: int, *, unlock: bool = False) -> None:
    if os.name == "posix":
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if os.name != "nt":
        raise OSError(errno.ENOSYS, "unsupported platform")
    from core.platform_windows import lock_fd
    lock_fd(fd, unlock=unlock)


AccessVerifier = Callable[[Path, str], bool]


class HeldLock:
    """Owns a descriptor; its lock file is never deleted or replaced.

    The per-handle mutex serializes anchor closure with all legacy acquisition
    and child registration.  It is a thread guard, not an interprocess lock.
    """

    def __init__(self, fd: int, role: str, snapshot: PathPreflight,
                 parent: HeldLock | None = None, *, windows_guard=None, windows_lease=None):
        _require_verified_fork_safety()
        self._fd = fd
        self.role = role
        self.snapshot = snapshot
        self._parent = parent
        self._children = 0
        self._pending_children = 0
        self._closed = False
        self._state_lock = threading.RLock()
        if os.name != "nt" and (windows_guard is not None or windows_lease is not None):
            raise TypeError("Windows ownership is unavailable on this platform")
        if os.name == "nt" and (windows_guard is not None or windows_lease is not None):
            if windows_guard is None or windows_lease is None:
                raise LockError("windows_security_unverified")
            self._windows_guard = windows_guard
            self._windows_lease = windows_lease
        if parent is not None:
            with parent._state_lock:
                if parent._closed or parent.role != "anchor.lock":
                    raise LockError("lock_order")
                parent._children += 1
        _held_locks.add(self)

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def fileno(self) -> int:
        with self._state_lock:
            if self._closed:
                raise LockError("lock_closed")
            if os.name == "nt" and hasattr(self, "_windows_guard"):
                try:
                    return self._windows_guard.fileno()
                except Exception:
                    raise LockError("windows_security_unverified") from None
            return self._fd

    def close(self) -> None:
        with _fork_guard:
            if self._parent is None:
                with self._state_lock:
                    self._close_locked()
            else:
                with self._parent._state_lock:
                    with self._state_lock:
                        self._close_locked()

    def _close_locked(self) -> None:
        if self._closed:
            if os.name == "nt" and getattr(self, "_windows_release_unknown", False):
                raise LockError("lock_release_unknown")
            return
        if self._children or self._pending_children:
            raise LockError("lock_order")
        if os.name == "nt" and hasattr(self, "_windows_guard"):
            self._close_windows_locked()
            return
        failed = False
        try:
            _lock_fd(self._fd, unlock=True)
        except OSError:
            failed = True
        try:
            os.close(self._fd)
        except OSError:
            failed = True
        self._closed = True
        _held_locks.discard(self)
        if self._parent is not None:
            if self._parent._children <= 0:
                raise LockError("lock_order")
            self._parent._children -= 1
        if failed:
            raise LockError("unlock_failed")

    def _close_windows_locked(self) -> None:
        """Windows guard is the sole CRT-fd owner; never close fd a second time."""
        global _windows_release_unknown
        failed = False
        try:
            self._windows_guard.close()
        except BaseException:
            failed = True
        try:
            self._windows_lease.close()
        except BaseException:
            failed = True
        self._closed = True
        self._windows_release_unknown = failed
        if failed:
            _windows_release_unknown = True
        _held_locks.discard(self)
        if self._parent is not None:
            if self._parent._children <= 0:
                raise LockError("lock_order")
            # An unknown child release must continue to pin the anchor. Do not
            # pretend that its exclusion/descriptor was actually released.
            if not failed:
                self._parent._children -= 1
        if failed:
            raise LockError("lock_release_unknown")

    def __enter__(self) -> HeldLock:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _verify(callback: AccessVerifier, path: Path, kind: str, *, created: bool = False) -> None:
    try:
        allowed = callback(path, kind) is True
    except Exception:
        raise LockError("access_unverified", created=created) from None
    if not allowed:
        raise LockError("access_unverified", created=created)


def _same_open_file(fd: int, path: Path, expected: PathIdentity | None,
                    *, private: bool, created: bool) -> os.stat_result:
    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
    except OSError:
        raise LockError("identity_unknown", created=created) from None
    if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_nlink != 1 or current.st_nlink != 1
            or (os.name == "posix" and (opened.st_uid != os.geteuid()
                or (opened.st_mode & 0o600) != 0o600
                or (private and (opened.st_mode & 0o077) != 0)))):
        raise LockError("identity_unknown", created=created)
    if expected is not None and (opened.st_dev, opened.st_ino) != (expected.device, expected.inode):
        raise LockError("identity_changed", created=created)
    return opened


def _fresh(snapshot: PathPreflight) -> PathPreflight:
    identities = snapshot.as_dict()
    try:
        paths = {key: identities[key].requested for key in PATH_KEYS}
        if os.name == "nt" and "install.json" not in identities:
            return inspect_runtime_identities(snapshot.project_root, paths, windows_light=True)
        return inspect_runtime_identities(snapshot.project_root, paths)
    except (KeyError, PreflightError):
        raise LockError("preflight_failed") from None


def acquire_lock(
    snapshot: PathPreflight,
    role: str,
    *,
    create: bool = False,
    access_verifier: AccessVerifier | None = None,
    anchor: HeldLock | None = None,
) -> HeldLock:
    """Serialize legacy acquisition/registration with anchor closure.

    Both the mutex and pending counter are needed: a verifier callback can
    re-enter close on the same thread while the re-entrant mutex is held.
    Windows accepts no custom boolean verifier: native path/ACL evidence is
    re-read instead; only None or the existing built-in verifier is accepted.
    """
    with _fork_guard:
        return _acquire_with_guard(snapshot, role, create=create,
                                   access_verifier=access_verifier, anchor=anchor)


def _acquire_with_guard(snapshot, role, *, create, access_verifier, anchor):
    if os.name not in ("posix", "nt"):
        raise LockError("platform_unverified")
    if os.name == "nt":
        return _acquire_windows_with_guard(snapshot, role, create=create,
                                           access_verifier=access_verifier, anchor=anchor)
    _require_verified_fork_safety()
    if role == "lock":
        if not isinstance(anchor, HeldLock):
            raise LockError("lock_order")
        with anchor._state_lock:
            if anchor._closed or anchor._pending_children:
                raise LockError("lock_order")
            anchor._pending_children += 1
            try:
                return _acquire_lock_locked(snapshot, role, create=create,
                                            access_verifier=access_verifier, anchor=anchor)
            finally:
                anchor._pending_children -= 1
    return _acquire_lock_locked(snapshot, role, create=create,
                                access_verifier=access_verifier, anchor=anchor)


def _windows_same_components(before, after) -> bool:
    """For a just-created target only, its formerly absent final name may appear."""
    if before.exists or not after.exists or len(before.missing) != 1:
        return False
    if (before.expected_kind != "file" or after.expected_kind != "file"
            or before.drive != after.drive or before.private != after.private
            or before.single_link != after.single_link
            or before.boundary_depth != after.boundary_depth
            or len(after.components) != len(before.components) + 1
            or after.components[-1].name != before.missing[0]):
        return False
    for index, (old, new) in enumerate(zip(before.components, after.components[:-1])):
        if old.name != new.name or old.observation != new.observation:
            return False
        if index == len(before.components) - 1:
            if (old.policy != "creation_parent" or old.creation_kind != "file"
                    or new.policy != "ancestor" or new.creation_kind is not None):
                return False
        elif old.policy != new.policy or old.creation_kind != new.creation_kind:
            return False
    return True


def _windows_compare_post(before, after, role: str) -> None:
    old, new = before.windows_dict(), after.windows_dict()
    if old.keys() != new.keys():
        raise LockError("preflight_changed")
    for name in old:
        if name != role or old[name].exists:
            if old[name] != new[name]:
                raise LockError("preflight_changed")
        elif not _windows_same_components(old[name], new[name]):
            raise LockError("preflight_changed")


def _windows_verify_guard(guard, lease) -> None:
    """Match the held lock HANDLE to the independently reopened path lease."""
    global _windows_release_unknown
    try:
        if not lease.evidence.exists or not lease.evidence.components:
            raise LockError("identity_unknown")
        observation = lease.evidence.components[-1].observation
        identity = guard.object_evidence.identity
        if (observation.kind != "file" or observation.reparse or observation.links != 1
                or observation.volume != identity.volume_serial
                or int.from_bytes(observation.file_id, "big") != identity.file_index
                or observation.links != identity.link_count):
            raise LockError("identity_changed")
        # Never release an anchor automatically while it may have a child.
        guard.revalidate(retain_on_failure=True)
        lease.revalidate()
    except BaseException as exc:
        if (getattr(exc, "code", None) in ("release_unknown", "lock_release_unknown", "close_failed",
                                          "resource_release_failed", "handle_close_failed")
                or getattr(getattr(lease, "_backend", None), "release_unknown", False) is True):
            _windows_release_unknown = True
        guard.invalidate()
        raise


def _acquire_windows_with_guard(snapshot, role, *, create, access_verifier, anchor):
    from core.discovery import WindowsPathPreflight
    if _windows_release_unknown:
        raise LockError("lock_release_unknown")
    # No caller-provided bool or fabricated PathPreflight authorizes Windows.
    if (not isinstance(snapshot, WindowsPathPreflight)
            or (access_verifier is not None and access_verifier is not verify_local_access)):
        raise LockError("windows_security_unverified")
    if role not in ("anchor.lock", "control.lock", "lock") or type(create) is not bool:
        raise LockError("invalid_lock_role")
    if role == "lock":
        if not isinstance(anchor, HeldLock):
            raise LockError("lock_order")
        with anchor._state_lock:
            if (anchor._closed or anchor._pending_children or anchor.role != "anchor.lock"
                    or anchor.snapshot != snapshot or not hasattr(anchor, "_windows_guard")):
                raise LockError("lock_order")
            anchor._pending_children += 1
            try:
                return _acquire_windows_locked(snapshot, role, create=create, anchor=anchor)
            except BaseException:
                # An unsuccessful legacy check cannot leave anchor evidence
                # usable, but must retain its lock until ordered close.
                anchor._windows_guard.invalidate()
                raise
            finally:
                anchor._pending_children -= 1
    return _acquire_windows_locked(snapshot, role, create=create, anchor=None)


def _acquire_windows_locked(snapshot, role, *, create, anchor):
    global _windows_release_unknown
    from core.discovery import _windows_open_preflight, _windows_close_leases
    from core.platform_windows import acquire_guarded_path_lock, WindowsPathGuardError
    before_leases = None
    after_leases = None
    retained = None
    guard = None
    created = False
    try:
        identities = snapshot.as_dict()
        if "state_dir" not in identities or not identities["state_dir"].exists:
            raise LockError("state_missing")
        if role not in identities:
            raise LockError("preflight_required")
        target = identities[role]
        if not target.exists and not create:
            raise LockError("lock_missing")
        paths = {key: identities[key].requested for key in PATH_KEYS}
        light = "install.json" not in identities
        if light:
            before, before_leases = _windows_open_preflight(snapshot.project_root, paths, light=True)
        else:
            before, before_leases = _windows_open_preflight(snapshot.project_root, paths)
        if before != snapshot:
            raise LockError("preflight_changed")
        if anchor is not None:
            _windows_verify_guard(anchor._windows_guard, anchor._windows_lease)
        try:
            # Reuse the boundary from the held, matching discovery lease, not
            # a caller-selected string or an inferred write permission.
            evidence = before_leases[role].evidence
            depth = evidence.boundary_depth
            if (type(depth) is not int or depth < 1
                    or depth >= len(evidence.components)):
                raise LockError("boundary_unverified")
            boundary = evidence.drive + "\\" + "\\".join(
                item.name for item in evidence.components[1:depth + 1])
            guard = acquire_guarded_path_lock(target.canonical, create=not target.exists,
                                               trusted_boundary=boundary)
        except WindowsPathGuardError as exc:
            code = "lock_release_unknown" if exc.code == "release_unknown" else exc.code
            raise LockError(code, created=exc.created) from None
        created = guard.created
        # A newly created role cannot revalidate its former absence; all other
        # roles must remain unchanged while the lock is already held.
        for name, lease in before_leases.items():
            if name != role or target.exists:
                lease.revalidate()
        if anchor is not None:
            _windows_verify_guard(anchor._windows_guard, anchor._windows_lease)
        if light and role == "control.lock" and target.exists:
            # The roles and alias relationships already matched the snapshot
            # with all leases held. Revalidate those same leases under the
            # acquired control lock instead of reopening the entire preflight.
            # Creation still takes the normal post-creation path below.
            if created is not False:
                raise LockError("identity_changed", created=created)
            _windows_verify_guard(guard, before_leases[role])
            for lease in before_leases.values():
                lease.revalidate()
            retained = before_leases.pop(role)
            old_scope, before_leases = before_leases, None
            _windows_close_leases(old_scope.values())
            held = HeldLock(guard.fileno(), role, before, parent=None,
                            windows_guard=guard, windows_lease=retained)
            guard = None
            retained = None
            return held
        old_scope, before_leases = before_leases, None
        _windows_close_leases(old_scope.values())
        if light:
            after, after_leases = _windows_open_preflight(snapshot.project_root, paths, light=True)
        else:
            after, after_leases = _windows_open_preflight(snapshot.project_root, paths)
        _windows_compare_post(snapshot, after, role)
        _windows_verify_guard(guard, after_leases[role])
        for lease in after_leases.values():
            lease.revalidate()
        if anchor is not None:
            _windows_verify_guard(anchor._windows_guard, anchor._windows_lease)
        retained = after_leases.pop(role)
        old_scope, after_leases = after_leases, None
        _windows_close_leases(old_scope.values())
        held = HeldLock(guard.fileno(), role, after, parent=anchor,
                        windows_guard=guard, windows_lease=retained)
        guard = None
        retained = None
        return held
    except BaseException as exc:
        failure = False
        for scope in (before_leases, after_leases):
            if scope is not None:
                try:
                    _windows_close_leases(scope.values())
                except BaseException:
                    failure = True
        if retained is not None:
            try:
                retained.close()
            except BaseException:
                failure = True
        if guard is not None:
            try:
                guard.close()
            except BaseException:
                failure = True
        if failure:
            _windows_release_unknown = True
            outcome = True if created is True else getattr(exc, "created", created)
            raise LockError("lock_release_unknown", created=outcome) from None
        if getattr(exc, "release_unknown", False) is True:
            _windows_release_unknown = True
            if isinstance(exc, Exception):
                outcome = True if created is True else getattr(exc, "created", created)
                raise LockError("lock_release_unknown", created=outcome) from None
            raise
        if isinstance(exc, LockError):
            if exc.code == "lock_release_unknown":
                _windows_release_unknown = True
            if created and not exc.created:
                raise LockError(exc.code, created=True) from None
            raise
        if (isinstance(exc, PreflightError) and exc.code == "release_unknown") or (
                isinstance(exc, WindowsPathGuardError) and exc.code in ("release_unknown", "lock_release_unknown")):
            _windows_release_unknown = True
            raise LockError("lock_release_unknown", created=created) from None
        if isinstance(exc, Exception):
            raise LockError("windows_security_unverified", created=created) from None
        raise


def _acquire_lock_locked(
    snapshot: PathPreflight,
    role: str,
    *,
    create: bool = False,
    access_verifier: AccessVerifier | None = None,
    anchor: HeldLock | None = None,
) -> HeldLock:
    """Acquire a lock after a current read-only preflight.

    The fixed state directory must already exist. ``lock`` (legacy) requires
    the same still-held anchor. The caller supplies a protection verifier;
    the built-in mode check does not prove ACLs. A successful primitive is
    not proof of service/process ownership. Windows requires native testing.
    """
    if os.name not in ("posix", "nt"):
        raise LockError("platform_unverified")
    _require_verified_fork_safety()
    if role not in ("anchor.lock", "control.lock", "lock"):
        raise LockError("invalid_lock_role")
    if access_verifier is None:
        raise LockError("access_unverified")
    if os.name == "posix" and (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_CLOEXEC")):
        raise LockError("platform_unverified")
    if not isinstance(snapshot, PathPreflight):
        raise LockError("preflight_required")
    if role == "lock":
        if (anchor is None or anchor.closed or anchor.role != "anchor.lock"
                or anchor.snapshot != snapshot):
            raise LockError("lock_order")
        try:
            anchor_stat = os.fstat(anchor.fileno())
            anchor_info = snapshot.as_dict()["anchor.lock"]
        except (OSError, KeyError):
            raise LockError("identity_unknown") from None
        if (anchor_stat.st_dev, anchor_stat.st_ino) != (anchor_info.device, anchor_info.inode):
            raise LockError("identity_changed")
    if _fresh(snapshot) != snapshot:
        raise LockError("preflight_changed")
    identities = snapshot.as_dict()
    expected = identities[role]
    target = Path(expected.canonical)
    private = role != "lock"
    if private:
        state = identities["state_dir"]
        if (not state.exists or state.mode is None
                or (os.name == "posix" and (state.uid != os.geteuid() or (state.mode & 0o077) != 0))
                or target != Path(state.canonical) / role):
            raise LockError("access_unverified")
    _verify(access_verifier, target.parent, "directory")
    if expected.exists:
        _verify(access_verifier, target, "file")
    elif not create:
        raise LockError("lock_missing")

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if not expected.exists:
        flags |= os.O_CREAT | os.O_EXCL
    created = False
    try:
        fd = os.open(target, flags, 0o600)
        _pending_fds.add(fd)
        created = not expected.exists
    except OSError:
        raise LockError("lock_open_failed") from None
    locked = False
    try:
        os.set_inheritable(fd, False)
        _same_open_file(fd, target, expected if expected.exists else None,
                        private=private, created=created)
        _verify(access_verifier, target, "file", created=created)
        try:
            _lock_fd(fd)
            locked = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise LockError("lock_conflict", created=created) from None
            raise LockError("lock_unavailable", created=created) from None
        opened = _same_open_file(fd, target, expected if expected.exists else None,
                                 private=private, created=created)
        try:
            post = _fresh(snapshot)
        except LockError:
            raise LockError("preflight_failed", created=created) from None
        after = post.as_dict()
        for name, before in identities.items():
            current = after[name]
            if name != role or before.exists:
                if before != current:
                    raise LockError("preflight_changed", created=created)
            elif (not current.exists or current.canonical != before.canonical
                  or current.trace[:-1] != before.trace
                  or (current.device, current.inode) != (opened.st_dev, opened.st_ino)):
                raise LockError("preflight_changed", created=created)
        held = HeldLock(fd, role, post, parent=anchor if role == "lock" else None)
        _pending_fds.discard(fd)
        return held
    except BaseException:
        _pending_fds.discard(fd)
        if locked:
            try:
                _lock_fd(fd, unlock=True)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
        raise
