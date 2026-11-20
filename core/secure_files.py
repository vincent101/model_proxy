"""Stable snapshots and conservative macOS private-file installation writes.

Windows and unverified ACL/filesystem states fail closed. No cross-process
serialization or persistent freeze is implemented; replace is not a CAS.
"""
from __future__ import annotations

import os
import stat
import hashlib
import errno
import inspect
import logging
import sys
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.darwin_acl import ACLVerificationError, read_acl_snapshot



# Only these fixed diagnostic tokens may cross a writer's error boundary.
# In particular, backend exceptions and callbacks may carry arbitrary .code.
_WRITE_ERROR_CODES = frozenset((
    'acl_api_unavailable', 'acl_changed', 'acl_copy_failed', 'acl_dup_failed',
    'acl_enumeration_failed', 'acl_free_failed', 'acl_index_inconsistent',
    'acl_invalid', 'acl_not_deny_only', 'acl_permissions_unknown',
    'acl_size_invalid', 'acl_unavailable', 'authorization_identity_changed',
    'backup_staging_not_distinct', 'boundary_guard_unavailable', 'commit_marker_unavailable',
    'config_attributes_preservation_unverified', 'config_attributes_changed',
    'config_changed', 'config_read_error', 'config_too_large',
    'cross_volume_staging_not_authorized', 'directory_attributes_unsupported',
    'directory_attributes_unverified', 'directory_changed', 'directory_identity_unknown',
    'extended_attributes_unverified', 'extended_attributes_copy_failed', 'fd_close_unknown', 'file_acl_present',
    'file_flags_unverified', 'file_not_found', 'file_permissions_unsafe',
    'filesystem_error', 'filesystem_or_backend_error', 'filesystem_unverified',
    'hardlinks_not_preserved', 'identity_changed', 'identity_unknown', 'identity_unverified',
    'interrupted', 'invalid_content', 'invalid_cwd', 'invalid_limit', 'invalid_link',
    'invalid_new_content', 'invalid_path', 'invalid_size_limit', 'invalid_snapshot',
    'invalid_trusted_boundary', 'link_acl_present', 'link_acl_unavailable',
    'link_attributes_unverified', 'link_changed', 'link_or_type_unknown',
    'link_protection_changed', 'new_file_permissions_unverified', 'not_directory',
    'not_regular_file', 'outside_authorized_root', 'parent_changed',
    'parent_close_and_cleanup_unknown', 'parent_close_unknown',
    'parent_close_unknown_after_verified', 'parent_link_or_type',
    'parent_owner_unverified', 'parent_unknown', 'parent_writable', 'path_unverified',
    'permission_denied', 'platform_acl_unverified', 'platform_unverified', 'postcheck_failed',
    'private_directory_authorization_missing', 'private_directory_changed',
    'private_directory_permissions_unsafe', 'private_directory_purpose_invalid',
    'protection_changed', 'protection_unknown', 'staging_changed',
    'staging_cleanup_failed', 'staging_cleanup_unknown',
    'staging_close_and_cleanup_unknown', 'staging_close_unknown',
    'staging_content_unverified', 'staging_identity_changed',
    'staging_protection_mismatch', 'staging_volume_changed',
    'sticky_entry_owner_unverified', 'target_changed', 'target_outside_authorization',
    'target_too_large', 'target_unknown', 'too_many_links', 'volume_unverified',
    'write_authorization_missing', 'write_locations_invalid', 'write_preflight_unverified',
))


def _safe_write_error_code(exc, *, default='filesystem_or_backend_error'):
    try:
        code = getattr(exc, 'code', None)
    except BaseException:
        return default
    return code if type(code) is str and code in _WRITE_ERROR_CODES else default


_MISSING_ERROR_ATTRIBUTE = object()


def _safe_error_flag(exc, name):
    """Read exception metadata without trusting descriptors or truthiness."""
    if exc is None:
        return False
    try:
        value = getattr(exc, name)
    except AttributeError:
        try:
            # A property may itself raise AttributeError; that is not proof
            # the metadata is absent. Custom dynamic lookup is also unknown.
            if inspect.getattr_static(exc, name, _MISSING_ERROR_ATTRIBUTE) is not _MISSING_ERROR_ATTRIBUTE:
                return True
            if inspect.getattr_static(type(exc), '__getattribute__') is not BaseException.__getattribute__:
                return True
            return inspect.getattr_static(type(exc), '__getattr__', _MISSING_ERROR_ATTRIBUTE) is not _MISSING_ERROR_ATTRIBUTE
        except BaseException:
            return True
    except BaseException:
        return True
    return value if type(value) is bool else True


def _safe_error_state(exc):
    try:
        state = getattr(exc, 'state')
    except BaseException:
        return 'UNKNOWN'
    if type(state) is str and state in ('NOT_REPLACED', 'UNKNOWN', 'REPLACED', 'POSTCHECK_FAILED', 'VERIFIED'):
        return state
    return 'UNKNOWN'


class SecureSnapshotError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProtectionEvidence:
    """Opaque, comparable proof supplied by a real platform adapter."""
    secure: bool
    token: Any = field(repr=False)


ProtectionReader = Callable[[str, os.stat_result], ProtectionEvidence]


@dataclass(frozen=True)
class TargetSnapshot:
    path: str = field(repr=False)
    authorized_root: str = field(repr=False)
    exists: bool
    contents: bytes | None = field(repr=False)
    parents: tuple[tuple, ...] = field(repr=False)
    identity: tuple | None = field(repr=False)
    protection: ProtectionEvidence | None = field(repr=False)
    trusted_boundary: str | None = field(default=None, repr=False)

    @property
    def comparable_identity(self) -> tuple:
        """Path, observed parents, target inode and file-protection evidence."""
        return (self.path, self.parents,
                self.identity[:2] if self.exists else None,
                self.protection.token if self.exists else None)


@dataclass(frozen=True, repr=False)
class StagedCandidate:
    """Evidence for C, not proof that a commit occurred.

    Only compare it with a freshly protected TargetSnapshot; inode alone does
    not authenticate a file. Raw contents never enter the exception.
    """
    path: str = field(repr=False)
    parent: tuple = field(repr=False)
    identity: tuple[int, int]
    protection: tuple = field(repr=False)
    mtime_ns: int
    digest: bytes = field(repr=False)

    @property
    def comparable_identity(self) -> tuple:
        return (self.path, self.parent, self.identity, self.protection)

    def matches(self, snapshot: TargetSnapshot) -> bool:
        return (isinstance(snapshot, TargetSnapshot) and snapshot.exists
                and snapshot.path == self.path and bool(snapshot.parents)
                and snapshot.protection is not None and snapshot.identity is not None
                and isinstance(snapshot.contents, bytes)
                and snapshot.comparable_identity == self.comparable_identity
                and snapshot.identity[7] == self.mtime_ns
                and hashlib.sha256(snapshot.contents).digest() == self.digest)


def _meta(st: os.stat_result) -> tuple:
    if stat.S_ISDIR(st.st_mode):
        if not hasattr(st, 'st_flags') or st.st_nlink < 1:
            raise SecureSnapshotError('identity_unknown')
        return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_flags)
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _evidence(reader: ProtectionReader, path: str, info: os.stat_result) -> ProtectionEvidence:
    try:
        evidence = reader(path, info)
    except _PrivateOpenError:
        raise
    except Exception:
        raise SecureSnapshotError("protection_unknown") from None
    if not isinstance(evidence, ProtectionEvidence) or evidence.secure is not True:
        raise SecureSnapshotError("protection_unknown")
    return evidence


def _parents(parent: str, reader: ProtectionReader, boundary: str | None = None) -> tuple[tuple, ...]:
    """Observe every component; apply protection from the approved boundary."""
    parts = Path(parent).parts
    current = Path(parts[0])
    result = []
    for part in (None, *parts[1:]):
        if part is not None:
            current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            raise SecureSnapshotError("parent_unknown") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SecureSnapshotError("parent_link_or_type")
        # APFS directory link counts change when unrelated entries are added.
        identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
        protected = boundary is None or os.path.commonpath((str(current), boundary)) == boundary
        evidence = _evidence(reader, str(current), info) if protected else None
        result.append((str(current), identity, evidence))
    return tuple(result)


def _normalize(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SecureSnapshotError("invalid_path")
    if not os.path.isabs(value) or value.startswith("//") or ".." in Path(value).parts:
        raise SecureSnapshotError("invalid_path")
    return os.path.normpath(value)


def capture_target(
    target: str | os.PathLike[str],
    *,
    authorized_root: str | os.PathLike[str],
    protection_reader: ProtectionReader | None,
    max_bytes: int,
    trusted_boundary: str | os.PathLike[str] | None = None,
) -> TargetSnapshot:
    """Capture target bytes and observed protection without creating files.

    This primitive rejects symlinks and hardlinked files. An independently
    authorized, normalized ``trusted_boundary`` may limit protection checks
    to that existing ancestor and below; higher components are still observed
    and rechecked for identity and type. None preserves the historical policy.
    The caller, not this function, chooses and authorizes that boundary.
    Callers must compare a fresh snapshot after confirmation and again before
    any future write.  This primitive cannot make a later replace a CAS.
    """
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_CLOEXEC")):
        raise SecureSnapshotError("platform_unverified")
    if protection_reader is None:
        raise SecureSnapshotError("protection_unknown")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise SecureSnapshotError("invalid_limit")
    path, root = _normalize(target), _normalize(authorized_root)
    try:
        if os.path.commonpath((path, root)) != root or path == root:
            raise SecureSnapshotError("outside_authorized_root")
    except ValueError:
        raise SecureSnapshotError("invalid_path") from None
    parent, name = os.path.dirname(path), os.path.basename(path)
    boundary = _normalize(trusted_boundary) if trusted_boundary is not None else None
    if boundary is not None and os.path.commonpath((parent, boundary)) != boundary:
        raise SecureSnapshotError("invalid_trusted_boundary")
    before_parents = _parents(parent, protection_reader, boundary)
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise SecureSnapshotError("parent_unknown") from None
    try:
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_dev, parent_info.st_ino, parent_info.st_mode,
                parent_info.st_uid, parent_info.st_gid) != before_parents[-1][1]:
            raise SecureSnapshotError("parent_changed")
        if _parents(parent, protection_reader, boundary) != before_parents:
            raise SecureSnapshotError("parent_changed")
        try:
            initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if _parents(parent, protection_reader, boundary) != before_parents:
                raise SecureSnapshotError("parent_changed")
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return TargetSnapshot(path, root, False, None, before_parents, None, None, boundary)
            raise SecureSnapshotError("target_changed")
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise SecureSnapshotError("link_or_type_unknown")
        if initial.st_size < 0 or initial.st_size > max_bytes:
            raise SecureSnapshotError("target_too_large")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except OSError:
            raise SecureSnapshotError("target_unknown") from None
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _meta(opened) != _meta(initial)):
                raise SecureSnapshotError("target_changed")
            protection = _evidence(protection_reader, path, opened)
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise SecureSnapshotError("target_too_large")
                chunks.append(chunk)
            final = os.fstat(fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (_meta(opened) != _meta(final) or _meta(opened) != _meta(current)
                    or size != opened.st_size or _evidence(protection_reader, path, final) != protection):
                raise SecureSnapshotError("target_changed")
            if _parents(parent, protection_reader, boundary) != before_parents:
                raise SecureSnapshotError("parent_changed")
            return TargetSnapshot(path, root, True, b"".join(chunks), before_parents,
                                  _meta(opened), protection, boundary)
        finally:
            _close_private_fd(fd)
    except OSError:
        raise SecureSnapshotError("target_unknown") from None
    finally:
        _close_private_fd(parent_fd)


def target_unchanged(
    snapshot: TargetSnapshot,
    *,
    protection_reader: ProtectionReader | None,
    max_bytes: int,
) -> bool:
    """Compare a fresh stable snapshot; inability to verify raises, not False."""
    return snapshot == capture_target(snapshot.path, authorized_root=snapshot.authorized_root,
                                      protection_reader=protection_reader, max_bytes=max_bytes,
                                      trusted_boundary=snapshot.trusted_boundary)

# A deliberately narrow native writer: macOS regular private files without
# extended ACL/xattrs. Anything else is rejected, never silently weakened.
@dataclass(frozen=True)
class WriteOutcome:
    state: str
    backup: str | None = field(default=None, repr=False)
    snapshot: TargetSnapshot | None = field(default=None, repr=False)
    candidate: StagedCandidate | None = field(default=None, repr=False)


class SecureWriteError(RuntimeError):
    def __init__(self, code: str, *, state: str = 'NOT_REPLACED', backup: str | None = None, staging: str | None = None,
                 close_unknown: bool = False, cleanup_unknown: bool = False,
                 candidate: StagedCandidate | None = None):
        super().__init__(code)
        self.code, self.state, self.backup, self.staging = code, state, backup, staging
        self.close_unknown, self.cleanup_unknown = close_unknown, cleanup_unknown
        self.candidate = candidate


class _PrivateOpenError(SecureSnapshotError):
    def __init__(self, *, close_unknown: bool, cleanup_unknown: bool,
                 marker_unknown: bool = False):
        code = ('staging_close_and_cleanup_unknown' if close_unknown and cleanup_unknown else
                'staging_close_unknown' if close_unknown else 'staging_cleanup_failed')
        super().__init__(code)
        self.close_unknown = close_unknown
        self.cleanup_unknown = cleanup_unknown
        self.marker_unknown = marker_unknown


def _close_private_fd(fd):
    """One close attempt only; an exception cannot prove the FD stayed open."""
    active = sys.exc_info()[1]
    try:
        os.close(fd)
    except BaseException:
        raise _PrivateOpenError(close_unknown=True,
                                cleanup_unknown=_safe_error_flag(active, 'cleanup_unknown'),
                                marker_unknown=_safe_error_flag(active, 'marker_unknown')) from None


@dataclass(frozen=True, repr=False)
class PrivateDirectorySnapshot:
    """Evidence for one caller-authorized, existing private directory.

    The purpose is explicit: a backup authorization is not staging authority,
    and neither is target-write authority. No directory is created or repaired.
    """
    path: str
    purpose: str
    protection: tuple = field(repr=False)
    business_xattr_compat: bool = False


def _directory_proof(fd: int, *, business_xattr_compat=False) -> tuple:
    # The config walker also binds relevant directory xattrs; use the same
    # narrow policy rather than accepting unknown access-related attributes.
    from core.mac_config_path import _directory_attributes
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o7077
            or stat.S_IMODE(info.st_mode) & 0o700 != 0o700):
        raise SecureSnapshotError('private_directory_permissions_unsafe')
    attributes = None if business_xattr_compat else _directory_attributes(fd)
    proof = _mac_fd_protection(fd, directory=True)
    if not business_xattr_compat and _directory_attributes(fd) != attributes:
        raise SecureSnapshotError('private_directory_changed')
    # A directory's link count can change when an unrelated child is made.
    return proof[:5] + proof[6:] + (getattr(info, 'st_flags', 0), attributes)


def verify_private_directory(snapshot: PrivateDirectorySnapshot, fd: int) -> None:
    if not isinstance(snapshot, PrivateDirectorySnapshot):
        raise SecureSnapshotError('private_directory_authorization_missing')
    before = _directory_proof(fd, business_xattr_compat=snapshot.business_xattr_compat)
    try:
        entry = os.stat(snapshot.path, follow_symlinks=False)
    except OSError:
        raise SecureSnapshotError('private_directory_changed') from None
    if (not stat.S_ISDIR(entry.st_mode)
            or (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_uid, entry.st_gid)
            != before[:5] or before != snapshot.protection
            or _directory_proof(fd, business_xattr_compat=snapshot.business_xattr_compat) != before):
        raise SecureSnapshotError('private_directory_changed')


@contextmanager
def open_private_directory(snapshot: PrivateDirectorySnapshot, *, purpose: str):
    if (not isinstance(snapshot, PrivateDirectorySnapshot)
            or snapshot.purpose != purpose):
        raise SecureSnapshotError('private_directory_authorization_missing')
    fd = os.open(snapshot.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        verify_private_directory(snapshot, fd)
        yield fd
    finally:
        _close_private_fd(fd)


def capture_private_directory(path: str | os.PathLike[str], *, purpose: str,
                              business_xattr_compat: bool = False) -> PrivateDirectorySnapshot:
    """Capture an independently authorized staging or backup directory.

    Call only for a location approved for this purpose. This function checks
    the directory, but cannot infer or grant the caller's authorization.
    """
    if purpose not in ('staging', 'backup') or type(business_xattr_compat) is not bool:
        raise SecureSnapshotError('private_directory_purpose_invalid')
    path = _normalize(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        result = PrivateDirectorySnapshot(
            path, purpose, _directory_proof(fd, business_xattr_compat=business_xattr_compat),
            business_xattr_compat)
        verify_private_directory(result, fd)
        return result
    finally:
        _close_private_fd(fd)


@dataclass(frozen=True, repr=False)
class RuntimeWriteLocations:
    """Internal policy inputs, not user-configurable paths or an authorization.

    Callers independently approve staging and backup for their roles. A true
    target-side exception is allowed only for the approved external roles and
    only when the actual target and runtime staging are on different volumes.
    """
    staging: PrivateDirectorySnapshot
    backup: PrivateDirectorySnapshot | None
    allow_target_staging: bool = False


def _same_volume(source_fd: int, target_fd: int) -> bool:
    source, target = os.fstat(source_fd), os.fstat(target_fd)
    if source.st_dev <= 0 or target.st_dev <= 0:
        raise SecureSnapshotError('volume_unverified')
    return source.st_dev == target.st_dev


def _mac_native():
    import ctypes
    import sys
    if sys.platform != 'darwin' or os.name != 'posix':
        raise SecureSnapshotError('platform_acl_unverified')
    try:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        lib.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.acl_get_fd_np.restype = ctypes.c_void_p
        lib.acl_free.argtypes = [ctypes.c_void_p]
        lib.acl_free.restype = ctypes.c_int
        lib.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.flistxattr.restype = ctypes.c_ssize_t
        lib.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        lib.fgetxattr.restype = ctypes.c_ssize_t
        return lib
    except (OSError, AttributeError):
        raise SecureSnapshotError('platform_acl_unverified') from None


def _mac_local_apfs(fd: int, lib) -> tuple:
    """Native fstatfs on an open fd; reject remote and unverified filesystems."""
    import ctypes
    class StatFS(ctypes.Structure):
        _fields_ = [('bsize', ctypes.c_uint32), ('iosize', ctypes.c_int32),
                    ('blocks', ctypes.c_uint64), ('bfree', ctypes.c_uint64),
                    ('bavail', ctypes.c_uint64), ('files', ctypes.c_uint64),
                    ('ffree', ctypes.c_uint64), ('fsid', ctypes.c_int32 * 2),
                    ('owner', ctypes.c_uint32), ('type', ctypes.c_uint32),
                    ('flags', ctypes.c_uint32), ('subtype', ctypes.c_uint32),
                    ('fstypename', ctypes.c_char * 16), ('mntonname', ctypes.c_char * 1024),
                    ('mntfromname', ctypes.c_char * 1024), ('flags_ext', ctypes.c_uint32),
                    ('reserved', ctypes.c_uint32 * 7)]
    try:
        lib.fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(StatFS)]
        lib.fstatfs.restype = ctypes.c_int
        fs = StatFS()
        if lib.fstatfs(fd, ctypes.byref(fs)) != 0:
            raise SecureSnapshotError('filesystem_unverified')
        if fs.fstypename != b'apfs' or not fs.flags & 0x1000:  # MNT_LOCAL
            raise SecureSnapshotError('filesystem_unverified')
        return (fs.fstypename, fs.fsid[0], fs.fsid[1], fs.flags)
    except (AttributeError, OSError):
        raise SecureSnapshotError('filesystem_unverified') from None


def _mac_fd_protection(fd: int, *, directory: bool,
                       business_config_xattrs: bool = False) -> tuple:
    """Accept only stable deny-only directory ACLs; files require no ACL.

    Business-config reads may omit xattr inspection. None explicitly means
    unobserved, not an empty attribute set; all other callers remain strict.
    """
    import ctypes
    lib = _mac_native()
    st = os.fstat(fd)
    if (directory and not stat.S_ISDIR(st.st_mode)) or (not directory and not stat.S_ISREG(st.st_mode)):
        raise SecureSnapshotError('identity_changed')
    fs_info = _mac_local_apfs(fd, lib)
    if not directory and getattr(st, 'st_flags', 0):
        raise SecureSnapshotError('file_flags_unverified')
    try:
        acl_evidence = read_acl_snapshot(fd)
    except ACLVerificationError as exc:
        raise SecureSnapshotError(exc.code) from None
    attrs = None if business_config_xattrs and not directory else ()
    if not directory and not business_config_xattrs:
        ctypes.set_errno(0)
        count = lib.flistxattr(fd, None, 0, 0)
        if count < 0 or count > 4096:
            raise SecureSnapshotError('extended_attributes_unverified')
        if count:
            names_buf = ctypes.create_string_buffer(count)
            if lib.flistxattr(fd, names_buf, count, 0) != count:
                raise SecureSnapshotError('extended_attributes_unverified')
            names = tuple(sorted(n for n in names_buf.raw.split(b'\0') if n))
            # The macOS sandbox attaches provenance to newly created files.
            # It may be preserved only when the new file has identical bytes.
            if any(name != b'com.apple.provenance' for name in names):
                raise SecureSnapshotError('extended_attributes_unverified')
            vals = []
            for name in names:
                size = lib.fgetxattr(fd, name, None, 0, 0, 0)
                if size < 0 or size > 4096:
                    raise SecureSnapshotError('extended_attributes_unverified')
                buf = ctypes.create_string_buffer(max(size, 1))
                if lib.fgetxattr(fd, name, buf, size, 0, 0) != size:
                    raise SecureSnapshotError('extended_attributes_unverified')
                vals.append((name, buf.raw[:size]))
            attrs = tuple(vals)
    mode = stat.S_IMODE(st.st_mode)
    if directory:
        if st.st_uid not in (0, os.geteuid()):
            raise SecureSnapshotError('parent_owner_unverified')
        if mode & 0o022 and not (mode & stat.S_ISVTX and st.st_uid in (0, os.geteuid())):
            raise SecureSnapshotError('parent_writable')
    elif not business_config_xattrs and (st.st_uid != os.geteuid() or mode & 0o7077 or not mode & 0o400):
        raise SecureSnapshotError('file_permissions_unsafe')
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_nlink, acl_evidence, fs_info, attrs)


def mac_private_protection(path: str, info: os.stat_result, *,
                           business_config_xattrs: bool = False) -> ProtectionEvidence:
    """Check an opened object rather than relying on chmod or path alone."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if stat.S_ISDIR(info.st_mode):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
        try:
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino, actual.st_mode, actual.st_uid, actual.st_gid) != (
                    info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid):
                raise SecureSnapshotError('identity_changed')
            if stat.S_ISDIR(actual.st_mode) and _meta(info) != _meta(actual):
                raise SecureSnapshotError('identity_changed')
            if business_config_xattrs and not stat.S_ISDIR(actual.st_mode):
                checked = _mac_fd_protection(fd, directory=False, business_config_xattrs=True)
            else:
                checked = _mac_fd_protection(fd, directory=stat.S_ISDIR(actual.st_mode))
            if stat.S_ISDIR(actual.st_mode):
                rechecked = _mac_fd_protection(fd, directory=True)
                # Directory nlink may change when unrelated children change;
                # ACL bytes and all other protection evidence must not.
                if checked[:5] != rechecked[:5] or checked[6:] != rechecked[6:]:
                    raise SecureSnapshotError('protection_changed')
            after = os.fstat(fd)
            if _meta(actual) != _meta(after):
                raise SecureSnapshotError('identity_changed')
            if stat.S_ISDIR(actual.st_mode):
                try:
                    current = os.lstat(path)
                except OSError:
                    raise SecureSnapshotError('identity_changed') from None
                if not stat.S_ISDIR(current.st_mode) or _meta(after) != _meta(current):
                    raise SecureSnapshotError('identity_changed')
            return ProtectionEvidence(True, (stat.S_IMODE(actual.st_mode), actual.st_uid, actual.st_gid, checked[-3], getattr(actual, 'st_flags', 0), checked[-2], checked[-1]))
        finally:
            _close_private_fd(fd)
    except OSError:
        raise SecureSnapshotError('protection_unknown') from None


def _copy_extended_attributes(source_fd, destination_fd):
    """Copy visible xattrs on already-open FDs, without bytewise comparison."""
    import ctypes
    try:
        copy_xattrs = _mac_native().fcopyfile
        copy_xattrs.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        copy_xattrs.restype = ctypes.c_int
        if copy_xattrs(source_fd, destination_fd, None, 1 << 2) != 0:
            raise SecureSnapshotError('extended_attributes_copy_failed')
    except (AttributeError, OSError):
        raise SecureSnapshotError('extended_attributes_copy_failed') from None


def _open_private(parent_fd: int, name: str, contents: bytes, mode: int, uid: int, gid: int,
                  expected_attrs=None, *, business_config_xattrs=False,
                  copy_xattrs_from_fd=None) -> tuple[int, int]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        before = None
    except OSError:
        before = _MISSING_ERROR_ATTRIBUTE
    try:
        fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        if (type(exc) is FileExistsError and exc.errno == errno.EEXIST
                and before is not None and before is not _MISSING_ERROR_ATTRIBUTE):
            try:
                after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                unchanged_collision = _meta(before) == _meta(after)
            except BaseException:
                unchanged_collision = False
            if unchanged_collision:
                raise
        # Even an OSError may arrive from a wrapper after creation/open.
        # Never guess ownership or retry an FD we did not receive.
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                cleanup_unknown = False
            except BaseException:
                cleanup_unknown = True
            else:
                cleanup_unknown = True
        except BaseException:
            cleanup_unknown = True
        else:
            cleanup_unknown = True
        raise _PrivateOpenError(close_unknown=True, cleanup_unknown=cleanup_unknown) from None
    except BaseException:
        # The call may have created the name and opened an unretrievable FD.
        raise _PrivateOpenError(close_unknown=True, cleanup_unknown=True) from None
    created = None
    failure = None
    try:
        created = os.fstat(fd)
        os.fchmod(fd, mode)
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid) != (uid, gid):
            if business_config_xattrs:
                try:
                    os.fchown(fd, uid, gid)
                except OSError:
                    logging.getLogger(__name__).warning(
                        'business file ownership could not be preserved on the new file')
            else:
                os.fchown(fd, uid, gid)
        info = os.fstat(fd)
        if ((stat.S_IMODE(info.st_mode), info.st_nlink) != (mode, 1)
                or (not business_config_xattrs and (info.st_uid, info.st_gid) != (uid, gid))):
            raise SecureSnapshotError('new_file_permissions_unverified')
        checked = (_mac_fd_protection(fd, directory=False, business_config_xattrs=True)
                   if business_config_xattrs else _mac_fd_protection(fd, directory=False))
        if expected_attrs is not None and checked[-1] != expected_attrs:
            raise SecureSnapshotError('extended_attributes_unverified')
        if copy_xattrs_from_fd is not None:
            try:
                _copy_extended_attributes(copy_xattrs_from_fd, fd)
            except (SecureSnapshotError, OSError, AttributeError, TypeError):
                logging.getLogger(__name__).warning(
                    'extended attributes could not be copied; new file or backup may lack attributes')
            if _mac_fd_protection(fd, directory=False, business_config_xattrs=True) != checked:
                raise SecureSnapshotError('new_file_permissions_unverified')
        view = memoryview(contents)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short write')
            view = view[written:]
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        restored = bytearray()
        while len(restored) <= len(contents):
            part = os.read(fd, min(1024 * 1024, len(contents) + 1 - len(restored)))
            if not part:
                break
            restored.extend(part)
        if bytes(restored) != contents:
            raise SecureSnapshotError('staging_content_unverified')
        after = os.fstat(fd)
        if ((stat.S_IMODE(after.st_mode), after.st_nlink) != (mode, 1)
                or (not business_config_xattrs and (after.st_uid, after.st_gid) != (uid, gid))):
            raise SecureSnapshotError('new_file_permissions_unverified')
        after_proof = (_mac_fd_protection(fd, directory=False, business_config_xattrs=True)
                       if business_config_xattrs else _mac_fd_protection(fd, directory=False))
        if after_proof[-1] != checked[-1]:
            raise SecureSnapshotError('extended_attributes_unverified')
        identity = (after.st_dev, after.st_ino)
    except BaseException as exc:
        failure = exc

    # A failed close may already have closed the descriptor. Never retry it:
    # another thread could have reused its number in the meantime.
    close_unknown = False
    try:
        os.close(fd)
    except BaseException:
        close_unknown = True
    close_unknown = close_unknown or _safe_error_flag(failure, 'close_unknown')

    if failure is not None or close_unknown:
        cleanup_unknown = _safe_error_flag(failure, 'cleanup_unknown')
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if created is None or (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
                cleanup_unknown = True
            else:
                os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except BaseException:
            cleanup_unknown = True
        if close_unknown or cleanup_unknown:
            raise _PrivateOpenError(close_unknown=close_unknown, cleanup_unknown=cleanup_unknown,
                                    marker_unknown=_safe_error_flag(failure, 'marker_unknown')) from None
        raise failure
    return identity


def _verify_private_entry(fd, name, identity, contents, expected, *, business_config_xattrs=False):
    """Reopen one exclusively created file and verify identity, bytes and ACL."""
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                      dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != identity):
            raise SecureSnapshotError('staging_identity_changed')
        def protection():
            if business_config_xattrs:
                return _mac_fd_protection(file_fd, directory=False, business_config_xattrs=True)
            return _mac_fd_protection(file_fd, directory=False)
        proof = protection()
        if ((stat.S_IMODE(proof[2]), proof[3], proof[4], proof[6], proof[8])
                != expected):
            raise SecureSnapshotError('staging_protection_mismatch')
        data = bytearray()
        while len(data) <= len(contents):
            part = os.read(file_fd, min(65536, len(contents) + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (bytes(data) != contents or protection() != proof
                or _meta(info) != _meta(os.fstat(file_fd)) or _meta(info) != _meta(current)):
            raise SecureSnapshotError('staging_changed')
        return info, proof
    finally:
        _close_private_fd(file_fd)


def secure_replace_runtime(snapshot: TargetSnapshot, new_bytes: bytes, *, max_bytes: int,
                           locations: RuntimeWriteLocations,
                           make_backup: bool = True,
                           boundary_guard: Callable | None = None,
                           sidecar_xattr_compat: bool = False) -> WriteOutcome:
    """Opt-in protected runtime staging; caller separately authorizes target.

    ``capture_target``'s target and parent constraints still apply. A None
    backup is permitted only with make_backup=False (e.g. frequent runtime
    writes). The caller must authorize the exact target and, for an external
    cross-volume exception, expressly set allow_target_staging=True. Snapshot
    and locations are not themselves policy decisions. No fallback copies over
    the target. UNKNOWN means a commit syscall may have taken effect.
    boundary_guard, when supplied, revalidates caller-held path-chain/role
    evidence before creation and commit, and again after commit; it grants no
    authority and must raise if that evidence cannot be verified.

    A sidecar adapter can map capture_target's contents, comparable_identity
    and identity[7] / 1e9 (zero if absent) into its snapshot. For UNKNOWN or
    POSTCHECK_FAILED, the candidate's comparable_identity and mtime_ns / 1e9,
    together with the caller's new_bytes, are candidate evidence, not a
    verified commit. A close/cleanup-unknown failure must also map to UNKNOWN.
    The adapter must guard and revalidate the original sidecar path chain on
    capture as well as on commit; a failed read is never a proved absence.
    """
    import secrets
    if (not isinstance(snapshot, TargetSnapshot) or not isinstance(new_bytes, bytes)
            or type(max_bytes) is not int or max_bytes < 0 or len(new_bytes) > max_bytes
            or type(make_backup) is not bool or type(sidecar_xattr_compat) is not bool
            or (boundary_guard is not None and not callable(boundary_guard))
            or not isinstance(locations, RuntimeWriteLocations)
            or type(locations.allow_target_staging) is not bool
            or (make_backup and locations.backup is None)
            or (not make_backup and locations.backup is not None)):
        raise SecureWriteError('write_locations_invalid')
    parent, name = os.path.dirname(snapshot.path), os.path.basename(snapshot.path)
    temp = temp_identity = temp_fd = runtime_fd = None
    source_fd = None
    backup = backup_id = backup_fd = backup_entry = None
    state = 'NOT_REPLACED'
    candidate = None
    cleanup_unknown = close_unknown = False

    def protection_reader(path, info):
        if sidecar_xattr_compat:
            return mac_private_protection(path, info, business_config_xattrs=True)
        return mac_private_protection(path, info)

    def open_entry(*args):
        if sidecar_xattr_compat:
            return _open_private(*args, business_config_xattrs=True,
                                 copy_xattrs_from_fd=source_fd)
        return _open_private(*args)

    def verify_entry(*args):
        if sidecar_xattr_compat:
            return _verify_private_entry(*args, business_config_xattrs=True)
        return _verify_private_entry(*args)

    def guard():
        if boundary_guard is not None:
            boundary_guard()

    def close_checked(fd):
        nonlocal close_unknown
        try:
            _close_private_fd(fd)
        except BaseException:
            close_unknown = True
            raise

    try:
        with ExitStack() as stack:
            guard()
            if not target_unchanged(snapshot, protection_reader=protection_reader,
                                    max_bytes=max_bytes):
                raise SecureSnapshotError('target_changed')
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(close_checked, parent_fd)
            info = os.fstat(parent_fd)
            if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) != snapshot.parents[-1][1]:
                raise SecureSnapshotError('parent_changed')
            if sidecar_xattr_compat and snapshot.exists:
                source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                    dir_fd=parent_fd)
                stack.callback(close_checked, source_fd)
                if _meta(os.fstat(source_fd)) != snapshot.identity:
                    raise SecureSnapshotError('target_changed')
            runtime_fd = stack.enter_context(open_private_directory(locations.staging, purpose='staging'))
            if make_backup:
                backup_fd = stack.enter_context(open_private_directory(locations.backup, purpose='backup'))
                a, b = os.fstat(runtime_fd), os.fstat(backup_fd)
                if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                    raise SecureSnapshotError('backup_staging_not_distinct')
            same = _same_volume(runtime_fd, parent_fd)
            if not same and locations.allow_target_staging is not True:
                raise SecureSnapshotError('cross_volume_staging_not_authorized')
            temp_fd = runtime_fd if same else parent_fd

            def verify_dirs():
                verify_private_directory(locations.staging, runtime_fd)
                if make_backup:
                    verify_private_directory(locations.backup, backup_fd)
                if _same_volume(runtime_fd, parent_fd) != same:
                    raise SecureSnapshotError('staging_volume_changed')

            try:
                if snapshot.exists:
                    mode = stat.S_IMODE(snapshot.identity[2])
                    uid, gid = snapshot.identity[3:5]
                    token = snapshot.protection.token
                    expected = (mode, uid, gid, token[3], token[-1])
                    if make_backup:
                        backup_entry = name + '.bak.' + secrets.token_hex(8)
                        backup = os.path.join(locations.backup.path, backup_entry)
                        guard()
                        verify_dirs()
                        try:
                            backup_id = open_entry(backup_fd, backup_entry, snapshot.contents,
                                                      mode, uid, gid, token[-1])
                        except BaseException as exc:
                            if not _safe_error_flag(exc, 'cleanup_unknown'):
                                backup = None
                            raise
                        verify_entry(backup_fd, backup_entry, backup_id,
                                              snapshot.contents, expected)
                else:
                    mode, uid, gid = 0o600, os.geteuid(), info.st_gid
                    expected = (mode, uid, gid, None, ())
                temp = '.model-proxy-runtime-' + secrets.token_hex(12)
                guard()
                verify_dirs()
                temp_identity = open_entry(temp_fd, temp, new_bytes, mode, uid, gid,
                                              snapshot.protection.token[-1] if snapshot.exists else None)
                if not snapshot.exists:
                    # A platform provenance attribute may be attached at
                    # creation; use the exact verified evidence, not a guess.
                    test_fd = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=temp_fd)
                    try:
                        p = (_mac_fd_protection(test_fd, directory=False, business_config_xattrs=True)
                             if sidecar_xattr_compat else _mac_fd_protection(test_fd, directory=False))
                        expected = (mode, uid, gid, p[6], p[8])
                    finally:
                        close_checked(test_fd)
                verify_entry(temp_fd, temp, temp_identity, new_bytes, expected)
                if not target_unchanged(snapshot, protection_reader=protection_reader,
                                        max_bytes=max_bytes):
                    raise SecureSnapshotError('target_changed')
                verify_dirs()
                if backup_id:
                    verify_entry(backup_fd, backup_entry, backup_id, snapshot.contents, expected)
                verify_entry(temp_fd, temp, temp_identity, new_bytes, expected)
                # Immediately before the syscall, rebind both target and source.
                guard()
                if not target_unchanged(snapshot, protection_reader=protection_reader,
                                        max_bytes=max_bytes):
                    raise SecureSnapshotError('target_changed')
                verify_dirs()
                staged, staged_proof = verify_entry(
                    temp_fd, temp, temp_identity, new_bytes, expected)
                staged_token = (stat.S_IMODE(staged.st_mode), staged.st_uid, staged.st_gid,
                                staged_proof[6], getattr(staged, 'st_flags', 0),
                                staged_proof[7], staged_proof[8])
                candidate = StagedCandidate(snapshot.path, snapshot.parents,
                                            temp_identity, staged_token, staged.st_mtime_ns,
                                            hashlib.sha256(new_bytes).digest())
                state = 'UNKNOWN'
                if snapshot.exists:
                    os.replace(temp, name, src_dir_fd=temp_fd, dst_dir_fd=parent_fd)
                    temp = None
                    state = 'REPLACED'
                else:
                    os.link(temp, name, src_dir_fd=temp_fd, dst_dir_fd=parent_fd,
                            follow_symlinks=False)
                    state = 'REPLACED'
                    os.unlink(temp, dir_fd=temp_fd)
                    temp = None
                guard()
                final = capture_target(snapshot.path, authorized_root=snapshot.authorized_root,
                                       protection_reader=protection_reader, max_bytes=max_bytes,
                                       trusted_boundary=snapshot.trusted_boundary)
                if (not final.exists or final.contents != new_bytes
                        or tuple(final.identity[:2]) != temp_identity or final.identity[5] != 1
                        or final.parents != snapshot.parents
                        or (final.protection.token[0], final.protection.token[1],
                            final.protection.token[2], final.protection.token[3],
                            final.protection.token[-1]) != expected):
                    raise SecureSnapshotError('postcheck_failed')
                verify_dirs()
                if backup_id:
                    verify_entry(backup_fd, backup_entry, backup_id, snapshot.contents, expected)
                if not candidate.matches(final):
                    raise SecureSnapshotError('postcheck_failed')
                state = 'VERIFIED'
                return WriteOutcome('VERIFIED', backup, final, candidate)
            except BaseException as exc:
                cleanup_unknown = cleanup_unknown or _safe_error_flag(exc, 'cleanup_unknown')
                close_unknown = close_unknown or _safe_error_flag(exc, 'close_unknown')
                raise
            finally:
                staging_cleanup_unknown = False
                if temp and temp_identity:
                    try:
                        current = os.stat(temp, dir_fd=temp_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == temp_identity:
                            os.unlink(temp, dir_fd=temp_fd)
                        else:
                            staging_cleanup_unknown = True
                    except FileNotFoundError:
                        pass
                    except BaseException:
                        staging_cleanup_unknown = True
                    cleanup_unknown = cleanup_unknown or staging_cleanup_unknown
                    if staging_cleanup_unknown:
                        raise _PrivateOpenError(close_unknown=close_unknown,
                                                cleanup_unknown=True) from None
    except BaseException as exc:
        cleanup_unknown = cleanup_unknown or _safe_error_flag(exc, 'cleanup_unknown')
        if state == 'NOT_REPLACED' and issubclass(type(exc), SecureWriteError):
            state = _safe_error_state(exc)
        staging_path = None
        if cleanup_unknown and temp:
            staging_path = os.path.join(locations.staging.path if temp_fd == runtime_fd else parent, temp)
        raise SecureWriteError(_safe_write_error_code(
                                   exc, default='interrupted' if issubclass(type(exc), KeyboardInterrupt)
                                   else 'filesystem_or_backend_error'),
                               state='POSTCHECK_FAILED' if state in ('REPLACED', 'VERIFIED') else state,
                               backup=backup, staging=staging_path,
                               close_unknown=close_unknown or _safe_error_flag(exc, 'close_unknown'),
                               cleanup_unknown=cleanup_unknown,
                               candidate=candidate if state != 'NOT_REPLACED' else None) from None


def secure_replace(snapshot: TargetSnapshot, new_bytes: bytes, *, max_bytes: int,
                   locations: RuntimeWriteLocations | None = None) -> WriteOutcome:
    """Protect, back up, recheck, atomically replace/create, then verify.

    The last identity check and replace are not a CAS: non-cooperating writers
    can still race. Users must stop editing during installation. No symlink, extended ACL, unknown xattr, Windows or unverified platform
    fallback exists; matching macOS provenance is preserved.
    """
    if locations is not None:
        return secure_replace_runtime(snapshot, new_bytes, max_bytes=max_bytes, locations=locations)
    import secrets
    if not isinstance(new_bytes, bytes) or len(new_bytes) > max_bytes:
        raise SecureWriteError('invalid_content')
    parent = os.path.dirname(snapshot.path)
    name = os.path.basename(snapshot.path)
    temp = None
    temp_identity = None
    backup = None
    state = 'NOT_REPLACED'
    postcheck_complete = False
    parent_fd = None
    try:
        if not target_unchanged(snapshot, protection_reader=mac_private_protection, max_bytes=max_bytes):
            raise SecureSnapshotError('target_changed')
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(parent_fd)
        if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) != snapshot.parents[-1][1]:
            raise SecureSnapshotError('parent_changed')
        if snapshot.exists:
            mode = stat.S_IMODE(snapshot.identity[2])
            uid, gid = snapshot.identity[3:5]
            backup_name = name + '.bak.' + secrets.token_hex(8)
            backup = os.path.join(parent, backup_name)
            try:
                _open_private(parent_fd, backup_name, snapshot.contents, mode, uid, gid, snapshot.protection.token[-1])
            except BaseException as exc:
                if not (_safe_error_flag(exc, 'cleanup_unknown') or
                        issubclass(type(exc), SecureSnapshotError)
                        and _safe_write_error_code(exc) == 'staging_cleanup_failed'):
                    backup = None
                raise
        else:
            mode, uid, gid = 0o600, os.geteuid(), os.fstat(parent_fd).st_gid
        temp = '.model-proxy-install-' + secrets.token_hex(12)
        temp_identity = _open_private(parent_fd, temp, new_bytes, mode, uid, gid, snapshot.protection.token[-1] if snapshot.exists else None)
        if not target_unchanged(snapshot, protection_reader=mac_private_protection, max_bytes=max_bytes):
            raise SecureSnapshotError('target_changed')
        staged = os.stat(temp, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(staged.st_mode) or staged.st_nlink != 1 or (staged.st_dev, staged.st_ino) != temp_identity:
            raise SecureSnapshotError('staging_identity_changed')
        state = 'UNKNOWN'
        if snapshot.exists:
            os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temp = None
            state = 'REPLACED'
        else:
            # Atomic no-overwrite creation; link count is restored immediately.
            os.link(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            state = 'REPLACED'
            os.unlink(temp, dir_fd=parent_fd)
            temp = None
        final = capture_target(snapshot.path, authorized_root=snapshot.authorized_root,
                               protection_reader=mac_private_protection, max_bytes=max_bytes,
                               trusted_boundary=snapshot.trusted_boundary)
        if (final.contents != new_bytes or final.protection.token[:3] != (mode, uid, gid)
                or final.parents != snapshot.parents
                or (snapshot.exists and final.protection.token[-1] != snapshot.protection.token[-1])):
            raise SecureSnapshotError('postcheck_failed')
        postcheck_complete = True
        return WriteOutcome('VERIFIED', backup)
    except BaseException as exc:
        code = _safe_write_error_code(
            exc, default=('filesystem_error' if issubclass(type(exc), OSError) else
                          'interrupted' if issubclass(type(exc), KeyboardInterrupt) else
                          'filesystem_or_backend_error'))
        if state == 'NOT_REPLACED' and issubclass(type(exc), SecureWriteError):
            state = _safe_error_state(exc)
        cleanup_unknown = (_safe_error_flag(exc, 'cleanup_unknown') or code == 'staging_cleanup_failed')
        possible_staging = os.path.join(parent, temp) if temp and cleanup_unknown else None
        raise SecureWriteError(code, state='POSTCHECK_FAILED' if state == 'REPLACED' else state,
                               backup=backup, staging=possible_staging,
                               close_unknown=_safe_error_flag(exc, 'close_unknown'), cleanup_unknown=cleanup_unknown) from None
    finally:
        cleanup_unknown = False
        if temp and temp_identity and parent_fd is not None:
            try:
                staged = os.stat(temp, dir_fd=parent_fd, follow_symlinks=False)
                if (staged.st_dev, staged.st_ino) == temp_identity:
                    os.unlink(temp, dir_fd=parent_fd)
                else:
                    cleanup_unknown = True
            except FileNotFoundError:
                pass
            except BaseException:
                cleanup_unknown = True
        # Preserve error evidence if closing the directory itself fails.
        import sys
        active_error = sys.exc_info()[1]
        parent_close_unknown = False
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except BaseException:
                parent_close_unknown = True
        if cleanup_unknown or parent_close_unknown:
            combined_cleanup = cleanup_unknown or _safe_error_flag(active_error, 'cleanup_unknown')
            combined_close = parent_close_unknown or _safe_error_flag(active_error, 'close_unknown')
            code = ('parent_close_and_cleanup_unknown' if parent_close_unknown and combined_cleanup else
                    'parent_close_unknown_after_verified' if parent_close_unknown and postcheck_complete else
                    'parent_close_unknown' if parent_close_unknown else
                    'staging_close_and_cleanup_unknown' if combined_close else 'staging_cleanup_failed')
            raise SecureWriteError(code,
                                   state='POSTCHECK_FAILED' if state == 'REPLACED' else state,
                                   backup=backup,
                                   staging=(os.path.join(parent, temp) if combined_cleanup and temp else None),
                                   close_unknown=combined_close, cleanup_unknown=combined_cleanup) from None
