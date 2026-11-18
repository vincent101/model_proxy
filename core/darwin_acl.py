"""Descriptor-bound Darwin extended ACL evidence; unknown protection fails closed.

SDK sys/acl.h: ACL_TYPE_EXTENDED=0x100, ACL_EXTENDED_DENY=2,
ACL_FIRST_ENTRY=0, ACL_NEXT_ENTRY=-1, ACL_MAX_ENTRIES=128. Darwin's
acl_get_entry returns 0 for an entry and -1/EINVAL at the end (man 3).
This adapter does not establish ownership, mode safety, or filesystem trust.
Raw ACL flags are retained in the snapshot but not independently decoded;
classification relies on Darwin's validity and tag/permission APIs. Future or
undocumented flag semantics remain a limitation; ALLOW is never accepted.
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys

ACL_TYPE_EXTENDED = 0x100
ACL_FIRST_ENTRY = 0
ACL_NEXT_ENTRY = -1
ACL_EXTENDED_DENY = 2
ACL_MAX_ENTRIES = 128
# Known ACL permissions from the current Darwin sys/acl.h; unknown bits fail closed.
_KNOWN_PERMISSION_MASK = sum(1 << n for n in range(1, 14)) | (1 << 20)
_MAX_EXTERNAL_BYTES = 65536


class ACLVerificationError(RuntimeError):
    """Sanitized failure: no ACL bytes, paths or principals in exceptions."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _library():
    if sys.platform != 'darwin':
        raise ACLVerificationError('platform_unverified')
    try:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        signatures = {
            'acl_get_fd_np': ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            'acl_dup': ([ctypes.c_void_p], ctypes.c_void_p),
            'acl_valid': ([ctypes.c_void_p], ctypes.c_int),
            'acl_get_entry': ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            'acl_get_tag_type': ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            'acl_get_permset_mask_np': ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
            'acl_size': ([ctypes.c_void_p], ctypes.c_ssize_t),
            'acl_copy_ext': ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t], ctypes.c_ssize_t),
            'acl_free': ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            func = getattr(lib, name)
            func.argtypes, func.restype = args, result
        return lib
    except (OSError, AttributeError, TypeError, ValueError):
        raise ACLVerificationError('acl_api_unavailable') from None


def _external_bytes(lib, acl) -> bytes:
    size = lib.acl_size(acl)
    if not isinstance(size, int) or not 0 < size <= _MAX_EXTERNAL_BYTES:
        raise ACLVerificationError('acl_size_invalid')
    buffer = ctypes.create_string_buffer(size)
    if lib.acl_copy_ext(buffer, acl, size) != size:
        raise ACLVerificationError('acl_copy_failed')
    return buffer.raw


def _checked_deny_acl(lib, acl) -> bytes:
    """Validate on a duplicate because acl_valid may reorder its argument."""
    duplicate = lib.acl_dup(acl)
    if not duplicate:
        raise ACLVerificationError('acl_dup_failed')
    try:
        if lib.acl_valid(duplicate) != 0:
            raise ACLVerificationError('acl_invalid')
    finally:
        if lib.acl_free(duplicate) != 0:
            raise ACLVerificationError('acl_free_failed')
    original = _external_bytes(lib, acl)
    count = 0
    entry_id = ACL_FIRST_ENTRY
    while True:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = lib.acl_get_entry(acl, entry_id, ctypes.byref(entry))
        if result == -1:
            # EINVAL is the documented end only after a successful entry and
            # an ACL_NEXT_ENTRY call; it is not an acceptable empty ACL.
            if entry_id == ACL_NEXT_ENTRY and count and ctypes.get_errno() == errno.EINVAL:
                break
            raise ACLVerificationError('acl_enumeration_failed')
        if result != 0 or not entry.value or count >= ACL_MAX_ENTRIES:
            raise ACLVerificationError('acl_enumeration_failed')
        tag = ctypes.c_int()
        if lib.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or tag.value != ACL_EXTENDED_DENY:
            raise ACLVerificationError('acl_not_deny_only')
        permissions = ctypes.c_uint64()
        if (lib.acl_get_permset_mask_np(entry, ctypes.byref(permissions)) != 0
                or permissions.value & ~_KNOWN_PERMISSION_MASK):
            raise ACLVerificationError('acl_permissions_unknown')
        count += 1
        entry_id = ACL_NEXT_ENTRY
    # Darwin documents explicit numeric indexes. EINVAL from NEXT also means
    # invalid ACL, so cross-check that index `count` cannot retrieve another
    # entry. This is a targeted consistency check, not a proof against an OS
    # returning mutually inconsistent errors on both APIs.
    beyond = ctypes.c_void_p()
    ctypes.set_errno(0)
    result = lib.acl_get_entry(acl, count, ctypes.byref(beyond))
    if result != -1 or ctypes.get_errno() != errno.EINVAL:
        raise ACLVerificationError('acl_index_inconsistent')
    if _external_bytes(lib, acl) != original:
        raise ACLVerificationError('acl_changed')
    return original


def _read_once(fd: int, lib, *, directory: bool) -> bytes | None:
    ctypes.set_errno(0)
    acl = lib.acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
    if not acl:
        # Observed on this Darwin/APFS: ENOENT on a valid linked fd means no
        # extended ACL. Not proven for every future OS/filesystem; the caller
        # checks fd identity before/after. Filesystem-gated callers verify it
        # separately; this inference is not a universal OS guarantee.
        if ctypes.get_errno() == errno.ENOENT:
            return None
        raise ACLVerificationError('acl_unavailable')
    try:
        if not directory:
            raise ACLVerificationError('file_acl_present')
        return _checked_deny_acl(lib, acl)
    finally:
        if lib.acl_free(acl) != 0:
            raise ACLVerificationError('acl_free_failed')


def _marker(st: os.stat_result) -> tuple:
    if stat.S_ISDIR(st.st_mode):
        if not hasattr(st, 'st_flags') or st.st_nlink < 1:
            raise ACLVerificationError('identity_unverified')
        # Directory contents can change nlink/ctime without changing access.
        return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_flags)
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_nlink, st.st_ctime_ns)


def _directory_path_snapshot(fd: int, info: os.stat_result) -> str:
    """Bind a Darwin directory FD back to a still-existing pathname."""
    import fcntl
    if not hasattr(fcntl, 'F_GETPATH'):
        raise ACLVerificationError('path_unverified')
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b'\0' * 1024)
        end = raw.find(b'\0')
        if end <= 0:
            raise ACLVerificationError('path_unverified')
        path = os.fsdecode(raw[:end])
        if not os.path.isabs(path):
            raise ACLVerificationError('path_unverified')
        current = os.lstat(path)
        if not stat.S_ISDIR(current.st_mode) or _marker(current) != _marker(info):
            raise ACLVerificationError('identity_unverified')
        return path
    except ACLVerificationError:
        raise
    except (OSError, ValueError, TypeError):
        raise ACLVerificationError('path_unverified') from None


def read_acl_snapshot(fd: int) -> bytes | None:
    """Verify twice on the same FD and return its complete stable ACL bytes.

    Only deny-only directory ACLs are accepted. Any ACL on a regular file is
    rejected, including a deny-only ACL. Reads do not change the filesystem.
    """
    try:
        before = os.fstat(fd)
        if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)) or before.st_nlink < 1:
            raise ACLVerificationError('identity_unverified')
        lib = _library()
        directory = stat.S_ISDIR(before.st_mode)
        # APFS may retain a positive directory nlink after unlink. The FD
        # alone cannot establish that its pathname still exists.
        directory_path = _directory_path_snapshot(fd, before) if directory else None
        first = _read_once(fd, lib, directory=directory)
        middle = os.fstat(fd)
        second = _read_once(fd, lib, directory=directory)
        after = os.fstat(fd)
        if _marker(before) != _marker(middle) or _marker(middle) != _marker(after) or first != second:
            raise ACLVerificationError('acl_changed')
        if directory and _directory_path_snapshot(fd, after) != directory_path:
            raise ACLVerificationError('identity_unverified')
        return first
    except ACLVerificationError:
        raise
    except (OSError, AttributeError, TypeError, ValueError, OverflowError):
        raise ACLVerificationError('acl_unavailable') from None
