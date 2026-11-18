"""macOS business-config path snapshots; never use for runtime/install files.

Evidence detects observable changes, not an atomic freeze or compare-and-swap.
Callers authorizing writes must independently constrain the resolved target and
revalidate immediately before replacement. No raw paths or bytes enter errors.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field

from core.secure_files import (SecureSnapshotError, _mac_fd_protection,
                               _mac_local_apfs, _mac_native, _safe_error_flag)

# Retained as an opt-in bound for callers that already use this constant.
MAX_CONFIG_BYTES = 2 * 1024 * 1024
# Darwin SDK sys/param.h MAXSYMLINKS; match the OS traversal limit.
MAX_SYMLINKS = 32


class MacConfigError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, repr=False)
class MacConfigMarker:
    mtime_ns: int
    fingerprint: bytes = field(repr=False)


@dataclass(frozen=True, repr=False)
class MacConfigSnapshot:
    source_path: str
    target_path: str
    data: bytes
    final_identity: tuple
    chain: tuple
    protection: tuple
    target_parent_protection: tuple
    marker: MacConfigMarker
    max_bytes: int | None


def _dir_meta(st):
    if (not stat.S_ISDIR(st.st_mode) or st.st_ino <= 0 or st.st_nlink < 1
            or not hasattr(st, 'st_flags')):
        raise MacConfigError('directory_identity_unknown')
    # Unrelated entries change directory times and nlink, not its protection.
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_flags)


def _entry_meta(st):
    if st.st_ino <= 0 or st.st_nlink < 1 or not hasattr(st, 'st_flags'):
        raise MacConfigError('identity_unknown')
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_flags)


_DIRECTORY_ATTRS = frozenset((
    # Finder presentation and user tags do not grant filesystem access.
    b'com.apple.FinderInfo',
    b'com.apple.metadata:_kMDItemUserTags',
    # Preserve the existing Darwin private-file policy for system provenance.
    # It is tracked, not asserted to be irrelevant to every macOS policy.
    b'com.apple.provenance',
))
_MAX_DIRECTORY_ATTR_BYTES = 65536


def _directory_attributes(fd):
    """Capture allowlisted directory xattrs on the FD without exposing values.

    Unknown/security-sensitive attributes (including macl and quarantine) do
    not have a proven access policy here and are rejected. SHA-256 evidence
    avoids putting raw attribute values into reprs, logs, or the path chain.
    """
    lib = _mac_native()
    count = lib.flistxattr(fd, None, 0, 0)
    if count < 0 or count > _MAX_DIRECTORY_ATTR_BYTES:
        raise MacConfigError('directory_attributes_unverified')
    if not count:
        return ()
    names_buf = ctypes.create_string_buffer(count)
    if lib.flistxattr(fd, names_buf, count, 0) != count:
        raise MacConfigError('directory_attributes_unverified')
    raw_names = names_buf.raw
    if not raw_names.endswith(b'\0'):
        raise MacConfigError('directory_attributes_unverified')
    names = raw_names[:-1].split(b'\0')
    if any(not name for name in names) or len(set(names)) != len(names):
        raise MacConfigError('directory_attributes_unverified')
    if any(name not in _DIRECTORY_ATTRS for name in names):
        raise MacConfigError('directory_attributes_unsupported')
    attributes = []
    for name in sorted(names):
        size = lib.fgetxattr(fd, name, None, 0, 0, 0)
        if size < 0 or size > _MAX_DIRECTORY_ATTR_BYTES:
            raise MacConfigError('directory_attributes_unverified')
        value = ctypes.create_string_buffer(max(size, 1))
        if lib.fgetxattr(fd, name, value, size, 0, 0) != size:
            raise MacConfigError('directory_attributes_unverified')
        attributes.append((name, size, hashlib.sha256(value.raw[:size]).digest()))
    return tuple(attributes)


def _protection(fd, directory):
    before = os.fstat(fd)
    meta = _dir_meta if directory else _entry_meta
    initial = meta(before)
    attributes = _directory_attributes(fd) if directory else None
    proof = _mac_fd_protection(fd, directory=directory)
    if directory and _directory_attributes(fd) != attributes:
        raise MacConfigError('protection_changed')
    after = os.fstat(fd)
    if meta(after) != initial:
        raise MacConfigError('protection_changed')
    if directory:
        # nlink is not protection and can change on unrelated subdirectories.
        proof = proof[:5] + proof[6:] + (attributes,)
    return (initial, proof)


def _link_protection(fd):
    """Inspect the link itself using Darwin O_SYMLINK, never its target.

    APFS + ENOENT means no extended ACL under the same narrow assumption used
    by darwin_acl for regular files. Any link ACL/unknown xattr fails closed.
    """
    lib = _mac_native()
    before = os.fstat(fd)
    if not stat.S_ISLNK(before.st_mode):
        raise MacConfigError('link_changed')
    identity = _entry_meta(before)
    results = []
    for _ in range(2):
        fs = _mac_local_apfs(fd, lib)
        ctypes.set_errno(0)
        acl = lib.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
        if acl:
            if lib.acl_free(acl) != 0:
                raise MacConfigError('link_acl_unavailable')
            raise MacConfigError('link_acl_present')
        if ctypes.get_errno() != errno.ENOENT:
            raise MacConfigError('link_acl_unavailable')
        count = lib.flistxattr(fd, None, 0, 0)
        if count < 0 or count > 4096:
            raise MacConfigError('link_attributes_unverified')
        attrs = []
        if count:
            names_buf = ctypes.create_string_buffer(count)
            if lib.flistxattr(fd, names_buf, count, 0) != count:
                raise MacConfigError('link_attributes_unverified')
            names = sorted(name for name in names_buf.raw.split(b'\0') if name)
            if any(name != b'com.apple.provenance' for name in names):
                raise MacConfigError('link_attributes_unverified')
            for name in names:
                size = lib.fgetxattr(fd, name, None, 0, 0, 0)
                if size < 0 or size > 4096:
                    raise MacConfigError('link_attributes_unverified')
                value = ctypes.create_string_buffer(max(size, 1))
                if lib.fgetxattr(fd, name, value, size, 0, 0) != size:
                    raise MacConfigError('link_attributes_unverified')
                attrs.append((name, value.raw[:size]))
        if _entry_meta(os.fstat(fd)) != identity:
            raise MacConfigError('link_changed')
        results.append((fs, tuple(attrs)))
    if results[0] != results[1]:
        raise MacConfigError('link_protection_changed')
    return (identity, results[0])


def _sticky_entry(parent_stat, entry_stat):
    if (parent_stat.st_mode & 0o022 and parent_stat.st_mode & stat.S_ISVTX
            and entry_stat.st_uid not in (0, os.geteuid())):
        raise MacConfigError('sticky_entry_owner_unverified')


def _parts(path):
    parts = [part for part in path.split('/') if part]
    # A trailing slash requires the preceding component to be a directory.
    if path.endswith('/'):
        parts.append('.')
    return parts


def _absolute_source(path, cwd):
    path = os.fspath(path)
    if not isinstance(path, str) or not path or '\0' in path:
        raise MacConfigError('invalid_path')
    if path.startswith('/'):
        return path
    base = os.getcwd() if cwd is None else os.fspath(cwd)
    if not isinstance(base, str) or not base.startswith('/') or '\0' in base:
        raise MacConfigError('invalid_cwd')
    return base + '/' + path


def _close_fd(fd):
    active = sys.exc_info()[1]
    try:
        os.close(fd)
    except BaseException:
        error = MacConfigError('fd_close_unknown')
        error.close_unknown = True
        error.cleanup_unknown = _safe_error_flag(active, 'cleanup_unknown')
        error.marker_unknown = _safe_error_flag(active, 'marker_unknown')
        raise error from None


class _OwnedFD:
    """One lease, not a reusable fd number; never retry an uncertain close."""
    __slots__ = ('fd', '_active')

    def __init__(self, fd):
        self.fd = fd
        self._active = True

    def close(self):
        if self._active:
            self._active = False
            _close_fd(self.fd)


def _path_operation(operation, *args, changed=None, invalid_means_changed=False, **kwargs):
    """Classify only pathname operations, never ACL/filesystem proof failures."""
    try:
        return operation(*args, **kwargs)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
            code = 'permission_denied'
        elif isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
            code = changed or 'file_not_found'
        elif changed and (exc.errno in (errno.ELOOP, errno.ENOTDIR)
                          or invalid_means_changed and exc.errno == errno.EINVAL):
            code = changed
        else:
            code = 'filesystem_unverified'
        raise MacConfigError(code) from None


def _read_chunk(fd, size):
    """Known data-read failures are distinct from unknown proof failures."""
    try:
        return os.read(fd, size)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
            code = 'permission_denied'
        elif exc.errno == errno.EIO:
            code = 'config_read_error'
        else:
            code = 'filesystem_unverified'
        raise MacConfigError(code) from None


def _walk_inner(source, max_bytes, *, hold_parent, duplicate_sink):
    """Walk once with held directory descriptors; caller does a second walk."""
    dflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fflags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    with ExitStack() as opened:
        def own(fd):
            lease = _OwnedFD(fd)
            opened.callback(lease.close)
            return lease

        def root():
            fd = own(os.open('/', dflags))
            proof = _protection(fd.fd, True)
            events.append(('root', proof))
            return [(fd, '', proof)]

        events = []
        stack = root()
        pending = _parts(source)
        links = 0
        while pending:
            part = pending.pop(0)
            parent, _, parent_proof = stack[-1]
            fd = parent.fd
            if part == '.':
                events.append(('dot', parent_proof))
                continue
            if part == '..':
                up = own(_path_operation(os.open, '..', dflags, dir_fd=fd,
                                         changed='parent_changed'))
                expected = stack[-2][2] if len(stack) > 1 else stack[0][2]
                if _dir_meta(os.fstat(up.fd)) != expected[0]:
                    raise MacConfigError('parent_changed')
                proof = _protection(up.fd, True)
                if proof != expected:
                    raise MacConfigError('parent_changed')
                events.append(('dotdot', proof))
                if len(stack) > 1:
                    stack.pop()[0].close()
                # Use the freshly opened parent, checking it still denotes the
                # already verified ancestor even across filesystem mounts.
                previous = stack[-1][0]
                stack[-1] = (up, stack[-1][1], proof)
                previous.close()
                continue
            info = _path_operation(os.stat, part, dir_fd=fd, follow_symlinks=False)
            _sticky_entry(os.fstat(fd), info)
            if stat.S_ISLNK(info.st_mode):
                before = _entry_meta(info)
                # O_NOFOLLOW rejects symlinks on Darwin; O_SYMLINK instead
                # opens the link object itself and is verified against lstat.
                link_fd = own(_path_operation(
                    os.open, part, os.O_SYMLINK | os.O_CLOEXEC, dir_fd=fd,
                    changed='link_changed'))
                if _entry_meta(os.fstat(link_fd.fd)) != before:
                    raise MacConfigError('link_changed')
                link_proof = _link_protection(link_fd.fd)
                if link_proof[0] != before:
                    raise MacConfigError('link_changed')
                text = _path_operation(os.readlink, part, dir_fd=fd,
                                       changed='link_changed', invalid_means_changed=True)
                after = _path_operation(os.stat, part, dir_fd=fd, follow_symlinks=False,
                                        changed='link_changed')
                if before != _entry_meta(after) or _link_protection(link_fd.fd) != link_proof:
                    raise MacConfigError('link_changed')
                link_fd.close()
                if not text:
                    raise MacConfigError('invalid_link')
                links += 1
                if links > MAX_SYMLINKS:
                    raise MacConfigError('too_many_links')
                events.append(('link', part, before, text, parent_proof, link_proof))
                if text.startswith('/'):
                    new_root = root()
                    for ancestor, _, _ in reversed(stack):
                        ancestor.close()
                    stack = new_root
                pending = _parts(text) + pending
                continue
            if pending:
                if not stat.S_ISDIR(info.st_mode):
                    raise MacConfigError('not_directory')
                child = own(_path_operation(os.open, part, dflags, dir_fd=fd,
                                            changed='directory_changed'))
                if _dir_meta(os.fstat(child.fd)) != _dir_meta(info):
                    raise MacConfigError('directory_changed')
                proof = _protection(child.fd, True)
                if _dir_meta(info) != proof[0]:
                    raise MacConfigError('directory_changed')
                # A second pathname check binds the observation to the fd.
                if _dir_meta(_path_operation(os.stat, part, dir_fd=fd,
                                             follow_symlinks=False,
                                             changed='directory_changed')) != proof[0]:
                    raise MacConfigError('directory_changed')
                events.append(('directory', part, proof))
                stack.append((child, part, proof))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise MacConfigError('not_regular_file')
            initial = _entry_meta(info)
            if info.st_size < 0:
                raise MacConfigError('identity_unknown')
            if max_bytes is not None and info.st_size > max_bytes:
                raise MacConfigError('config_too_large')
            target_fd = own(_path_operation(os.open, part, fflags, dir_fd=fd,
                                           changed='target_changed'))
            if _entry_meta(os.fstat(target_fd.fd)) != initial:
                raise MacConfigError('target_changed')
            before_proof = _protection(target_fd.fd, False)
            if before_proof[0] != initial:
                raise MacConfigError('target_changed')
            chunks = []
            # A stable file ends at its initial size; one extra byte detects
            # growth without introducing a fixed limit or chasing a writer.
            remaining = info.st_size + 1
            while remaining:
                chunk = _read_chunk(target_fd.fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
            if len(data) > info.st_size:
                raise MacConfigError('target_changed')
            if _entry_meta(os.fstat(target_fd.fd)) != initial:
                raise MacConfigError('target_changed')
            after_proof = _protection(target_fd.fd, False)
            current = _path_operation(os.stat, part, dir_fd=fd, follow_symlinks=False,
                                      changed='target_changed')
            if (before_proof != after_proof or _entry_meta(current) != initial
                    or len(data) != initial[6]):
                raise MacConfigError('target_changed')
            # Re-prove held parent after reading, including ACL/filesystem.
            if _dir_meta(os.fstat(fd)) != parent_proof[0]:
                raise MacConfigError('parent_changed')
            if _protection(fd, True) != parent_proof:
                raise MacConfigError('parent_changed')
            target_fd.close()
            target = '/' + '/'.join([item[1] for item in stack[1:]] + [part])
            chain = tuple(events) + (('target', part, initial),)
            fingerprint = hashlib.sha256(repr((chain, after_proof, hashlib.sha256(data).digest())).encode('utf-8', 'surrogatepass')).digest()
            marker = MacConfigMarker(initial[7], fingerprint)
            snapshot = MacConfigSnapshot(source, target, data, initial, chain,
                                         after_proof, parent_proof, marker, max_bytes)
            parent_fd = os.dup(fd) if hold_parent else None
            if parent_fd is not None:
                duplicate_sink.append(parent_fd)
            return snapshot, parent_fd, part
        raise MacConfigError('not_regular_file')


def _walk(source, max_bytes, *, hold_parent=False):
    # A duplicated parent is otherwise orphaned if ExitStack reports a close
    # failure while returning from the walk.
    duplicates = []
    try:
        return _walk_inner(source, max_bytes, hold_parent=hold_parent,
                           duplicate_sink=duplicates)
    except BaseException:
        for fd in duplicates:
            _close_fd(fd)
        raise


def _checked_walk(source, max_bytes, *, hold_parent=False):
    try:
        if sys.platform != 'darwin' or os.name != 'posix':
            raise MacConfigError('platform_unverified')
        return _walk(source, max_bytes, hold_parent=hold_parent)
    except MacConfigError:
        raise
    except SecureSnapshotError as exc:
        raise MacConfigError(exc.code) from None
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        raise MacConfigError('filesystem_unverified') from None


def capture_config(path, *, cwd=None, max_bytes=None):
    """Capture exact bytes and re-walk; None imposes no fixed size limit."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise MacConfigError('invalid_size_limit')
    try:
        source = _absolute_source(path, cwd)
    except MacConfigError:
        raise
    except (OSError, TypeError, ValueError):
        raise MacConfigError('invalid_path') from None
    first, _, _ = _checked_walk(source, max_bytes)
    try:
        second, _, _ = _checked_walk(source, max_bytes)
    except MacConfigError as exc:
        if exc.code in ('file_not_found', 'config_too_large'):
            raise MacConfigError('config_changed') from None
        raise
    if first != second:
        raise MacConfigError('config_changed')
    return second


def verify_config(snapshot):
    if not isinstance(snapshot, MacConfigSnapshot):
        raise MacConfigError('invalid_snapshot')
    try:
        current = capture_config(snapshot.source_path, max_bytes=snapshot.max_bytes)
    except MacConfigError as exc:
        if exc.code in ('file_not_found', 'config_too_large'):
            raise MacConfigError('config_changed') from None
        raise
    if current != snapshot:
        raise MacConfigError('config_changed')
    return current


def same_target(a, b):
    """Both chains are independently revalidated; their spelling may differ."""
    a = verify_config(a)
    b = verify_config(b)
    return (a.final_identity == b.final_identity and a.data == b.data
            and a.protection == b.protection)


@contextmanager
def open_target_parent(snapshot):
    """Yield a held, verified (parent_fd, basename); not a write authorization."""
    verify_config(snapshot)
    current, fd, name = _checked_walk(snapshot.source_path, snapshot.max_bytes,
                                      hold_parent=True)
    try:
        if current != snapshot:
            raise MacConfigError('config_changed')
        verify_config(snapshot)
        if _dir_meta(os.fstat(fd)) != snapshot.target_parent_protection[0]:
            raise MacConfigError('parent_changed')
        parent_proof = _protection(fd, True)
        if parent_proof != snapshot.target_parent_protection:
            raise MacConfigError('parent_changed')
        info = _path_operation(os.stat, name, dir_fd=fd, follow_symlinks=False,
                               changed='target_changed')
        if _entry_meta(info) != snapshot.final_identity:
            raise MacConfigError('target_changed')
        # Ensure the descriptor itself remained stable throughout the last checks.
        if _protection(fd, True) != parent_proof:
            raise MacConfigError('parent_changed')
    except SecureSnapshotError as exc:
        _close_fd(fd)
        raise MacConfigError(exc.code) from None
    except OSError:
        _close_fd(fd)
        raise MacConfigError('filesystem_unverified') from None
    except BaseException:
        _close_fd(fd)
        raise
    try:
        yield fd, name
    finally:
        _close_fd(fd)
