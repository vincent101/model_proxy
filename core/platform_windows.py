"""Win32 file/lock primitives and a conservative guarded-path adapter.

Individual handle helpers do not prove parents or ACLs. Guarded-path evidence
covers only one target and its observed parent chain; it is not permission to
start a service or proof of cross-role disjointness. Path reopens are not
handle-relative; native behavior and filesystem boundaries need Windows tests.
Injectable backends are for isolated tests, not evidence of OS behavior.
"""
from __future__ import annotations

import ctypes
import errno
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any

# Fixed-width Windows ABI types (ctypes.wintypes can have host-specific sizes
# when this module is imported on a non-Windows test host).
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


class FILETIME(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("attributes", DWORD), ("creation_time", FILETIME),
                ("access_time", FILETIME), ("write_time", FILETIME),
                ("volume_serial", DWORD), ("size_high", DWORD),
                ("size_low", DWORD), ("links", DWORD),
                ("index_high", DWORD), ("index_low", DWORD)]


class FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [("creation", ctypes.c_int64), ("access", ctypes.c_int64),
                ("write", ctypes.c_int64), ("change", ctypes.c_int64),
                ("attributes", DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("length", DWORD), ("security_descriptor", ctypes.c_void_p),
                ("inherit_handle", BOOL)]


FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _error(code: int, *, operation: str, conflict: bool = False) -> OSError:
    # Never put a path, credential, or raw system message into an exception.
    exc = OSError(errno.EAGAIN if conflict else errno.EIO,
                  "Windows " + operation + " failed")
    exc.winerror = code
    return exc


class _NativeAPI:
    """Typed API bindings; constructed lazily and only on native Windows."""
    def __init__(self):
        if os.name != "nt":
            raise OSError(errno.ENOSYS, "Windows API unavailable")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, DWORD, DWORD,
                                       ctypes.POINTER(SECURITY_ATTRIBUTES), DWORD,
                                       DWORD, HANDLE]
        kernel.CreateFileW.restype = HANDLE
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = DWORD
        kernel.QueryDosDeviceW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD]
        kernel.QueryDosDeviceW.restype = DWORD
        kernel.GetVolumeInformationW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD,
                                                 ctypes.POINTER(DWORD), ctypes.POINTER(DWORD),
                                                 ctypes.POINTER(DWORD), ctypes.c_wchar_p, DWORD]
        kernel.GetVolumeInformationW.restype = BOOL
        kernel.CloseHandle.argtypes = [HANDLE]
        kernel.CloseHandle.restype = BOOL
        kernel.GetFileInformationByHandle.argtypes = [HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        kernel.GetFileInformationByHandle.restype = BOOL
        kernel.GetFileInformationByHandleEx.argtypes = [HANDLE, DWORD, ctypes.c_void_p, DWORD]
        kernel.GetFileInformationByHandleEx.restype = BOOL
        kernel.ReadFile.argtypes = [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p]
        kernel.ReadFile.restype = BOOL
        kernel.WriteFile.argtypes = [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p]
        kernel.WriteFile.restype = BOOL
        kernel.FlushFileBuffers.argtypes = [HANDLE]
        kernel.FlushFileBuffers.restype = BOOL
        kernel.ReplaceFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                        DWORD, ctypes.c_void_p, ctypes.c_void_p]
        kernel.ReplaceFileW.restype = BOOL
        kernel.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD]
        kernel.MoveFileExW.restype = BOOL
        kernel.LockFileEx.argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, ctypes.POINTER(OVERLAPPED)]
        kernel.LockFileEx.restype = BOOL
        kernel.UnlockFileEx.argtypes = [HANDLE, DWORD, DWORD, DWORD, ctypes.POINTER(OVERLAPPED)]
        kernel.UnlockFileEx.restype = BOOL

    def open_file(self, path: str, create: bool) -> int:
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, False)
        # Do not share delete: while this handle is open, another ordinary open
        # cannot rename/delete the file.  This is not parent-path protection.
        handle = self._kernel.CreateFileW(path, 0x80000000 | 0x40000000,
                                          0x1 | 0x2, ctypes.byref(attributes),
                                          1 if create else 3, 0x00200000, None)
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise _error(ctypes.get_last_error(), operation="open")
        return handle

    def open_private_candidate(self, path: str, *, original_handle=None, user_sid=None) -> int:
        """CREATE_NEW with an explicit descriptor, never inherited-by-default.

        Existing targets use their observed owner/group/DACL descriptor; new
        targets use a protected DACL granting only the effective user. The
        caller MUST verify the resulting descriptor and identity before writing.
        """
        descriptor = ctypes.c_void_p()
        handle = None
        create_started = False
        try:
            from core import windows_acl
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi.GetSecurityInfo.argtypes = [HANDLE, DWORD, DWORD, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            advapi.GetSecurityInfo.restype = DWORD
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                ctypes.c_wchar_p, DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
            self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
            self._kernel.LocalFree.restype = ctypes.c_void_p
            if original_handle is not None:
                result = advapi.GetSecurityInfo(original_handle, 1, 0x7, None, None,
                                                 None, None, ctypes.byref(descriptor))
                if type(result) is not int or result != 0 or not descriptor.value:
                    raise _error(result if type(result) is int else 0, operation="descriptor")
            else:
                if not windows_acl._valid_sid(user_sid):
                    raise _error(0, operation="descriptor")
                authority = int.from_bytes(user_sid[2:8], "big")
                components = [str(int.from_bytes(user_sid[i:i+4], "little"))
                              for i in range(8, len(user_sid), 4)]
                sid = "S-" + str(user_sid[0]) + "-" + str(authority)
                if components:
                    sid += "-" + "-".join(components)
                sddl = "O:" + sid + "D:P(A;;FA;;;" + sid + ")"
                if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                        sddl, 1, ctypes.byref(descriptor), None) or not descriptor.value:
                    raise _error(ctypes.get_last_error(), operation="descriptor")
            attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
            create_started = True
            handle = self._kernel.CreateFileW(path, 0x80000000 | 0x40000000, 0x1 | 0x2,
                                              ctypes.byref(attributes), 1, 0x00200000, None)
            if handle is None or handle == INVALID_HANDLE_VALUE:
                error = _error(ctypes.get_last_error(), operation="create")
                error.release_unknown = False  # No usable HANDLE was returned.
                raise error
            return handle
        except BaseException as exc:
            if not create_started:
                code = getattr(exc, "winerror", 0)
                error = _error(code if type(code) is int else 0, operation="descriptor")
                error.created = False
                error.release_unknown = False
                raise error from None
            raise
        finally:
            if descriptor.value:
                try:
                    if self._kernel.LocalFree(descriptor):
                        raise OSError(errno.EIO, "Windows descriptor release failed")
                except BaseException:
                    # The return has not reached the caller. Close a known
                    # handle once; do not retry an unconfirmed CloseHandle.
                    created = _valid_handle(handle)
                    if created:
                        try:
                            self.close(handle)
                        except BaseException:
                            pass
                    error = _error(0, operation="descriptor release")
                    error.code = "release_unknown"
                    error.created = True if created else None if create_started else False
                    error.release_unknown = True
                    raise error from None

    def create_private_directory(self, path: str, *, user_sid: bytes) -> None:
        """Create one directory with an explicit protected user-only DACL."""
        from core import windows_acl
        if not windows_acl._valid_sid(user_sid):
            raise _error(0, operation="descriptor")
        authority = int.from_bytes(user_sid[2:8], "big")
        components = [str(int.from_bytes(user_sid[i:i + 4], "little"))
                      for i in range(8, len(user_sid), 4)]
        sid = "S-" + str(user_sid[0]) + "-" + str(authority)
        if components:
            sid += "-" + "-".join(components)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [ctypes.c_wchar_p, DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        convert.restype = BOOL
        self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel.LocalFree.restype = ctypes.c_void_p
        self._kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SECURITY_ATTRIBUTES)]
        self._kernel.CreateDirectoryW.restype = BOOL
        descriptor = ctypes.c_void_p()
        if not convert("O:" + sid + "D:P(A;OICI;FA;;;" + sid + ")", 1,
                       ctypes.byref(descriptor), None) or not descriptor.value:
            raise _error(ctypes.get_last_error(), operation="descriptor")
        try:
            attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
            if not self._kernel.CreateDirectoryW(path, ctypes.byref(attributes)):
                raise _error(ctypes.get_last_error(), operation="mkdir")
        finally:
            self._kernel.LocalFree(descriptor)

    def open_read_file(self, path: str) -> int:
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, False)
        handle = self._kernel.CreateFileW(path, 0x80000000, 0x1 | 0x2,
                                          ctypes.byref(attributes), 3, 0x00200000, None)
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise _error(ctypes.get_last_error(), operation="open")
        return handle

    def file_marker(self, handle: int) -> tuple:
        info = self.file_info(handle)
        basic = FILE_BASIC_INFO()
        if not self._kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise _error(ctypes.get_last_error(), operation="identity")
        index = (int(info.index_high) << 32) | int(info.index_low)
        size = (int(info.size_high) << 32) | int(info.size_low)
        if (not index or info.links != 1 or info.attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
                or basic.attributes != info.attributes or basic.write <= 0 or basic.change <= 0):
            raise _error(0, operation="identity")
        return (int(info.volume_serial), index, int(basic.write) * 100,
                int(basic.change) * 100, size)

    def read_all(self, handle: int, *, max_bytes: int | None = None) -> bytes:
        parts = []
        total = 0
        while True:
            data = ctypes.create_string_buffer(65536)
            count = DWORD()
            if not self._kernel.ReadFile(handle, data, len(data), ctypes.byref(count), None):
                raise _error(ctypes.get_last_error(), operation="read")
            if count.value > len(data):
                raise _error(0, operation="read")
            if not count.value:
                return b"".join(parts)
            total += count.value
            if max_bytes is not None and total > max_bytes:
                raise _error(0, operation="read")
            parts.append(data.raw[:count.value])

    def replace_existing(self, target: str, source: str) -> None:
        # No backup and no ignore-ACL/merge or unsupported write-through flags.
        if not self._kernel.ReplaceFileW(target, source, None, 0, None, None):
            raise _error(ctypes.get_last_error(), operation="replace")

    def move_new(self, source: str, target: str) -> None:
        # No replace, COPY_ALLOWED, delayed reboot, or write-through flags.
        if not self._kernel.MoveFileExW(source, target, 0):
            raise _error(ctypes.get_last_error(), operation="move")

    def write_all_and_flush(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            block = data[offset:offset + 65536]
            buffer = ctypes.create_string_buffer(block, len(block))
            count = DWORD()
            if not self._kernel.WriteFile(handle, buffer, len(block), ctypes.byref(count), None):
                raise _error(ctypes.get_last_error(), operation="write")
            if not 0 < count.value <= len(block):
                raise _error(0, operation="write")
            offset += count.value
        if not self._kernel.FlushFileBuffers(handle):
            raise _error(ctypes.get_last_error(), operation="flush")

    def open_directory(self, path: str) -> int:
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, False)
        # FILE_LIST_DIRECTORY participates in sharing checks; attribute-only
        # access would not suffice. Share read/write, never share delete.
        handle = self._kernel.CreateFileW(path, 0x00020081, 0x1 | 0x2,
                                          ctypes.byref(attributes), 3,
                                          0x02000000 | 0x00200000, None)
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise _error(ctypes.get_last_error(), operation="open")
        return handle

    def volume_info(self, root: str) -> tuple:
        kind = int(self._kernel.GetDriveTypeW(root))
        if kind != 3:
            return (kind, None, None, None)
        device = ctypes.create_unicode_buffer(32768)
        if not self._kernel.QueryDosDeviceW(root[:2], device, len(device)):
            raise _error(ctypes.get_last_error(), operation="volume")
        serial, component, flags = DWORD(), DWORD(), DWORD()
        filesystem = ctypes.create_unicode_buffer(128)
        if not self._kernel.GetVolumeInformationW(root, None, 0, ctypes.byref(serial),
                                                   ctypes.byref(component), ctypes.byref(flags),
                                                   filesystem, len(filesystem)):
            raise _error(ctypes.get_last_error(), operation="volume")
        if not flags.value & 0x8:  # FILE_PERSISTENT_ACLS
            raise _error(0, operation="volume")
        return (kind, filesystem.value.upper(), int(serial.value), device.value)

    def close(self, handle: int) -> None:
        if not self._kernel.CloseHandle(handle):
            raise _error(ctypes.get_last_error(), operation="close")

    def file_info(self, handle: int) -> BY_HANDLE_FILE_INFORMATION:
        result = BY_HANDLE_FILE_INFORMATION()
        if not self._kernel.GetFileInformationByHandle(handle, ctypes.byref(result)):
            raise _error(ctypes.get_last_error(), operation="identity")
        return result

    def lock(self, handle: int, *, unlock: bool) -> None:
        overlapped = OVERLAPPED()  # zero offset, synchronous, one byte
        if unlock:
            ok = self._kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            ok = self._kernel.LockFileEx(handle, 0x1 | 0x2, 0, 1, 0,
                                         ctypes.byref(overlapped))
        if not ok:
            code = ctypes.get_last_error()
            raise _error(code, operation="unlock" if unlock else "lock",
                         conflict=not unlock and code in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION))


def _api(backend: Any = None):
    return _NativeAPI() if backend is None else backend


def _valid_handle(handle: int) -> bool:
    return isinstance(handle, int) and handle not in (0, -1, INVALID_HANDLE_VALUE)


@dataclass(frozen=True)
class FileIdentity:
    volume_serial: int
    file_index: int
    link_count: int
    attributes: int


def file_identity(handle: int, *, backend: Any = None) -> FileIdentity:
    """Read identity from the same handle; reject reparse points and aliases.

    This is only an individual-handle check, not ancestor or ACL verification.
    """
    if not _valid_handle(handle):
        raise _error(0, operation="identity")
    info = _api(backend).file_info(handle)
    index = (int(info.index_high) << 32) | int(info.index_low)
    if (not index or info.links != 1
            or info.attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)):
        raise _error(0, operation="identity")
    return FileIdentity(int(info.volume_serial), index, int(info.links), int(info.attributes))


@dataclass(frozen=True)
class DirectoryIdentity:
    volume_serial: int
    file_index: int
    link_count: int
    attributes: int


def directory_identity(handle: int, *, backend: Any = None) -> DirectoryIdentity:
    """Directory HANDLE identity; does not establish its pathname or ACL."""
    if not _valid_handle(handle):
        raise _error(0, operation="identity")
    info = _api(backend).file_info(handle)
    index = (int(info.index_high) << 32) | int(info.index_low)
    if (not index or info.links < 1 or not info.attributes & FILE_ATTRIBUTE_DIRECTORY
            or info.attributes & FILE_ATTRIBUTE_REPARSE_POINT):
        raise _error(0, operation="identity")
    return DirectoryIdentity(int(info.volume_serial), index, int(info.links), int(info.attributes))


class WindowsFileHandle:
    """Own a CreateFileW handle.  It carries no ACL/parent-safety guarantee."""
    def __init__(self, handle: int, backend: Any, *, created: bool = False):
        if not _valid_handle(handle):
            raise _error(0, operation="open")
        self._handle = handle
        self._backend = backend
        self.created = created
        self._state_lock = threading.RLock()

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._handle is None

    @property
    def handle(self) -> int:
        """Native HANDLE value, not a CRT file descriptor."""
        with self._state_lock:
            if self._handle is None:
                raise OSError(errno.EBADF, "Windows handle closed")
            return self._handle

    def close(self) -> None:
        with self._state_lock:
            if self._handle is not None:
                self._backend.close(self._handle)
                self._handle = None

    def _close_once(self) -> bool:
        """Atomically relinquish and close a raw HANDLE at most once.

        Return False if this wrapper no longer owns a HANDLE, not a claim that
        the resource was released. Once a close is attempted its outcome may
        be unknown even if interrupted; callers must never retry that value.
        """
        with self._state_lock:
            raw = self._handle
            self._handle = None
            if raw is None:
                return False
            self._backend.close(raw)
            return True

    def _transfer_to_fd(self, fdops):
        # Serializes ownership transfer with concurrent close/transfer.
        with self._state_lock:
            if self._handle is None:
                raise WindowsObjectLockError("handle_invalid", created=self.created)
            raw = self._handle
            try:
                fd = fdops.adopt(raw)
                if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
                    raise RuntimeError("ambiguous fd")
                self._handle = None
                return raw, fd
            except OSError:
                # open_osfhandle reports a normal failed transfer as OSError.
                # Ownership remains here, allowing one raw-handle close.
                raise
            except BaseException:
                # An asynchronous/unexpected failure may have occurred after
                # ownership transferred. Never guess which owner should close.
                self._handle = None
                raise WindowsObjectLockError("lock_release_unknown", created=self.created) from None

    def __enter__(self):
        self.handle
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def open_file(path: str, *, create: bool = False, backend: Any = None) -> WindowsFileHandle:
    """Open/create without truncation or inheritance; caller must verify ACLs.

    CREATE_NEW is used for creation; existing files are never overwritten.
    Same-handle identity rejects reparse points and hardlinks before return.
    No directory is created. Parent and ACL safety are NOT established. A
    rejected newly created file is left in place and the error has created=True.
    """
    if not isinstance(path, str) or not path or "\0" in path or not isinstance(create, bool):
        raise ValueError("invalid Windows file arguments")
    api = _api(backend)
    owned = WindowsFileHandle(api.open_file(path, create), api, created=create)
    try:
        file_identity(owned.handle, backend=api)
    except BaseException as exc:
        # Never silently delete a file, including one created before rejection.
        try:
            owned.close()
        except OSError as close_error:
            close_error.created = create
            close_error.owned_handle = owned  # caller may retry the failed close
            raise close_error from exc
        if isinstance(exc, OSError):
            exc.created = create
        raise
    return owned


def open_read_file(path: str, *, backend: Any = None) -> WindowsFileHandle:
    """Read-only, no-follow final component, no delete sharing, no inheritance."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("invalid Windows file arguments")
    api = _api(backend)
    owned = WindowsFileHandle(api.open_read_file(path), api)
    try:
        file_identity(owned.handle, backend=api)
    except BaseException as exc:
        try:
            owned.close()
        except OSError as close_error:
            close_error.owned_handle = owned
            raise close_error from exc
        raise
    return owned


def open_directory(path: str, *, backend: Any = None) -> WindowsFileHandle:
    """Open an existing non-inheritable directory handle, without share-delete.

    Rejects a final reparse point. It does NOT prove ancestors or bind names.
    """
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("invalid Windows directory arguments")
    api = _api(backend)
    owned = WindowsFileHandle(api.open_directory(path), api)
    try:
        directory_identity(owned.handle, backend=api)
    except BaseException as exc:
        try:
            owned.close()
        except OSError as close_error:
            close_error.owned_handle = owned
            raise close_error from exc
        raise
    return owned


@dataclass(frozen=True)
class DirectoryEvidence:
    """Caller-owned directory handle and prior ancestor ACL observation.

    A path string here is only a claim; it is NOT proven bound to the handle.
    """
    path: str = field(repr=False)
    acl: Any = field(repr=False)


@dataclass(frozen=True)
class ParentChainEvidence:
    """Candidate immutable parent evidence, NOT an authorization capability.

    Producer must retain non-delete-sharing handles and independently prove
    each path/handle binding, local filesystem, and all runtime-role aliases.
    Discovery has no such Windows implementation yet; the service gate stays.
    """
    target: str = field(repr=False)
    parents: tuple[DirectoryEvidence, ...] = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.target, str) or not self.target or "\0" in self.target
                or not isinstance(self.parents, tuple)
                or any(not isinstance(item, DirectoryEvidence) for item in self.parents)):
            raise ValueError("invalid parent evidence")


def revalidate_parent_chain(candidate: ParentChainEvidence, *, acl_backend: Any = None) -> None:
    """Recheck candidate directory ACLs; never establishes path binding.

    Requires a complete lexical drive-root-to-parent chain and held handles.
    This routine deliberately cannot open the platform_lock service gate.
    """
    from core import windows_acl
    if not isinstance(candidate, ParentChainEvidence):
        raise ValueError("invalid parent evidence")
    target = PureWindowsPath(candidate.target)
    if (not target.is_absolute() or target.drive.startswith("\\")
            or ".." in target.parts or target == target.parent):
        raise ValueError("invalid target")
    parent = target.parent
    required = tuple(reversed((parent, *parent.parents)))
    if len(required) != len(candidate.parents) or len(required) > 128:
        raise ValueError("incomplete parent chain")
    handles = set()
    identities = set()
    for expected, item in zip(required, candidate.parents):
        proof = item.acl
        if (item.path != str(expected) or not isinstance(proof, windows_acl.ACLEvidence)
                or proof.kind != "directory" or proof.policy != "ancestor"
                or proof.required_access & 0x000200A1 != 0x000200A1
                or proof.handle in handles):
            raise ValueError("invalid parent evidence")
        identity = proof.observation.identity[:2]
        if identity in identities:
            raise ValueError("aliased parent evidence")
        windows_acl.verify_unchanged(proof.handle, proof, backend=acl_backend)
        handles.add(proof.handle)
        identities.add(identity)


def lock_handle(handle: int, *, unlock: bool = False, backend: Any = None) -> None:
    """Lock/unlock byte [0,1), exclusive and nonblocking; does not own handle."""
    if not _valid_handle(handle):
        raise OSError(errno.EBADF, "invalid Windows handle")
    _api(backend).lock(handle, unlock=unlock)


def lock_fd(fd: int, *, unlock: bool = False, backend: Any = None,
            handle_from_fd=None) -> None:
    """Lock using a borrowed CRT descriptor; descriptor closure stays with caller."""
    if handle_from_fd is None:
        if os.name != "nt":
            raise OSError(errno.ENOSYS, "Windows API unavailable")
        import msvcrt
        handle_from_fd = msvcrt.get_osfhandle
    lock_handle(handle_from_fd(fd), unlock=unlock, backend=backend)


class WindowsObjectLockError(RuntimeError):
    """Sanitized object-level failure; a created file is not deleted."""
    def __init__(self, code: str, *, created: bool = False):
        super().__init__(code)
        self.code = code
        self.created = created
        self.release_unknown = code in ("release_unknown", "lock_release_unknown")


class _NativeFDAPI:
    """Win32 HANDLE to CRT fd adapter; created only on native Windows."""
    def __init__(self):
        if os.name != "nt":
            raise OSError(errno.ENOSYS, "Windows CRT unavailable")
        import msvcrt
        self._crt = msvcrt

    def adopt(self, handle: int) -> int:
        if not all(hasattr(os, name) for name in ("O_BINARY", "O_NOINHERIT")):
            raise OSError(errno.ENOSYS, "non-inheritable CRT fd unavailable")
        return self._crt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)

    def handle(self, fd: int) -> int:
        return self._crt.get_osfhandle(fd)

    def make_noninheritable(self, fd: int) -> None:
        os.set_inheritable(fd, False)
        if os.get_inheritable(fd):
            raise OSError(errno.EIO, "CRT fd is inheritable")

    def close(self, fd: int) -> None:
        os.close(fd)


@dataclass(frozen=True)
class ObjectLockEvidence:
    """Evidence for an opened object only; it does not authorize its path."""
    identity: FileIdentity
    acl: Any = field(repr=False)
    created: bool


class LockedObjectFD:
    """Owns one locked CRT fd until close or transfer to a future HeldLock.

    This is NOT a path/instance lock authority. The discovery gate remains
    closed until parent-path and cross-role alias evidence is integrated.
    """
    def __init__(self, fd: int, handle: int, evidence: ObjectLockEvidence,
                 fd_api: Any, win_api: Any):
        self._fd = fd
        self._handle = handle
        self.evidence = evidence
        self._fd_api = fd_api
        self._win_api = win_api
        self._state_lock = threading.RLock()

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._fd is None

    def fileno(self) -> int:
        with self._state_lock:
            if self._fd is None:
                raise WindowsObjectLockError("lock_closed")
            return self._fd

    def detach(self) -> int:
        """Transfer fd ownership once; receiver must close on *all* paths."""
        with self._state_lock:
            if self._fd is None:
                raise WindowsObjectLockError("lock_closed")
            fd = self._fd
            self._fd = None
            return fd

    def close(self) -> None:
        # Invalidate under a mutex before performing OS calls: no other thread
        # may retry close or detach a descriptor with an uncertain outcome.
        with self._state_lock:
            if self._fd is None:
                return
            fd = self._fd
            self._fd = None
            try:
                same = self._fd_api.handle(fd) == self._handle
            except BaseException:
                same = False
            if not same:
                # Do not close a possibly reused, unrelated CRT descriptor.
                raise WindowsObjectLockError("lock_release_unknown", created=self.evidence.created)
            failed = False
            try:
                lock_handle(self._handle, unlock=True, backend=self._win_api)
            except BaseException:
                failed = True
            try:
                self._fd_api.close(fd)
            except BaseException:
                failed = True
            if failed:
                raise WindowsObjectLockError("lock_release_unknown", created=self.evidence.created)

    def __enter__(self):
        self.fileno()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def lock_private_object(owned: WindowsFileHandle, *, fd_backend: Any = None,
                        acl_backend: Any = None) -> LockedObjectFD:
    """Transfer an opened file HANDLE to CRT and lock that same object.

    CreateFileW has already checked the final component. This object-only
    primitive NEVER proves parent path, installation, cross-role aliases, or
    filesystem locality. It cannot unlock platform_lock's Windows gate. On
    success the returned owner can later transfer its fd to a HeldLock after
    a separately reviewed path authority is available.
    """
    from core import windows_acl
    if not isinstance(owned, WindowsFileHandle):
        raise WindowsObjectLockError("handle_invalid")
    created, api = owned.created, owned._backend
    raw = None
    fd = None
    locked = False
    try:
        fdops = _NativeFDAPI() if fd_backend is None else fd_backend
        raw, fd = owned._transfer_to_fd(fdops)
        # open_osfhandle succeeded: the CRT is now the *only* owner.
        fdops.make_noninheritable(fd)
        handle = fdops.handle(fd)
        if handle != raw:
            raise WindowsObjectLockError("identity_changed", created=created)
        before = file_identity(handle, backend=api)
        access = windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE
        try:
            proof = windows_acl.read_stable_evidence(handle, kind="file", required_access=access,
                                                     policy="private", backend=acl_backend)
        except windows_acl.ACLVerificationError as exc:
            code = "lock_release_unknown" if exc.code in ("resource_release_failed", "handle_close_failed") else "access_unverified"
            raise WindowsObjectLockError(code, created=created) from None
        if proof.observation.identity != (before.volume_serial, before.file_index,
                                          before.link_count, before.attributes):
            raise WindowsObjectLockError("identity_changed", created=created)
        try:
            lock_handle(handle, backend=api)
        except OSError as exc:
            if (exc.errno == errno.EAGAIN and getattr(exc, "winerror", None)
                    in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION)):
                raise WindowsObjectLockError("lock_conflict", created=created) from None
            raise WindowsObjectLockError("lock_unavailable", created=created) from None
        locked = True
        if fdops.handle(fd) != handle or file_identity(handle, backend=api) != before:
            raise WindowsObjectLockError("identity_changed", created=created)
        try:
            windows_acl.verify_unchanged(handle, proof, backend=acl_backend)
        except windows_acl.ACLVerificationError as exc:
            code = ("lock_release_unknown" if exc.code in ("resource_release_failed", "handle_close_failed")
                    else "security_changed" if exc.code == "evidence_changed" else "access_unverified")
            raise WindowsObjectLockError(code, created=created) from None
        return LockedObjectFD(fd, handle, ObjectLockEvidence(before, proof, created), fdops, api)
    except BaseException as exc:
        failure = False
        interrupted = exc if not isinstance(exc, Exception) else None
        if fd is not None and isinstance(fd, int) and not isinstance(fd, bool) and fd >= 0:
            try:
                same = fdops.handle(fd) == raw
            except BaseException as cleanup_error:
                same = False
                if interrupted is None and not isinstance(cleanup_error, Exception):
                    interrupted = cleanup_error
            if not same:
                # Ownership is ambiguous; never close a possibly reused fd.
                failure = True
            else:
                if locked:
                    try:
                        lock_handle(raw, unlock=True, backend=api)
                    except BaseException as cleanup_error:
                        failure = True
                        if interrupted is None and not isinstance(cleanup_error, Exception):
                            interrupted = cleanup_error
                try:
                    fdops.close(fd)
                except BaseException as cleanup_error:
                    failure = True
                    if interrupted is None and not isinstance(cleanup_error, Exception):
                        interrupted = cleanup_error
        else:
            try:
                # Test ownership and relinquish it in one critical section:
                # another thread may transfer the HANDLE during cleanup.
                closed_raw = owned._close_once()
                if not closed_raw:
                    # The CRT may have adopted the HANDLE before interruption
                    # prevented assigning the returned (raw, fd) tuple. Without
                    # the fd, neither a close nor a successful release can be
                    # inferred. A pre-closed owner rejected before transfer is
                    # the only definite no-handoff case.
                    if not (isinstance(exc, WindowsObjectLockError)
                            and exc.code == "handle_invalid"):
                        failure = True
            except BaseException as cleanup_error:
                failure = True
                if interrupted is None and not isinstance(cleanup_error, Exception):
                    interrupted = cleanup_error
        if interrupted is not None:
            try:
                interrupted.created = created
                interrupted.release_unknown = failure or getattr(interrupted, "release_unknown", False) is True
            except BaseException:
                pass
            raise interrupted from None
        if failure:
            raise WindowsObjectLockError("lock_release_unknown", created=created) from None
        if isinstance(exc, WindowsObjectLockError):
            raise
        if isinstance(exc, Exception):
            raise WindowsObjectLockError("lock_unavailable", created=created) from None
        raise


def verify_local_access(path, kind) -> bool:
    """Fail closed: owner, DACL, parent and reparse safety are not yet proven."""
    return False


class WindowsPathGuardError(RuntimeError):
    """Sanitized guarded-path failure; created lock files are never deleted."""
    def __init__(self, code: str, *, created: bool | None = False):
        super().__init__(code)
        self.code = code
        self.created = created
        self.release_unknown = code in ("release_unknown", "lock_release_unknown")


# The directory's open requests LIST_DIRECTORY, READ_ATTRIBUTES and
# READ_CONTROL; require traverse in addition for use as a parent.
_ANCESTOR_ACCESS = 0x000200A1
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT")
    for number in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³")
}


def _local_absolute_target(path: str) -> PureWindowsPath:
    """Accept a strict drive-letter path; never normalize ambiguous spellings."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise WindowsPathGuardError("path_unverified")
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        raise WindowsPathGuardError("path_unverified")
    if path.startswith(("\\\\", "//")):
        raise WindowsPathGuardError("remote_unsupported")
    target = PureWindowsPath(path)
    drive = target.drive
    if (not target.is_absolute() or len(drive) != 2 or drive[1] != ":"
            or drive[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            or target.root != "\\" or str(target) != path or target == target.parent):
        raise WindowsPathGuardError("path_unverified")
    for part in target.parts[1:]:
        if (part in (".", "..") or part.endswith((" ", "."))
                or any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part)
                or part.split(".", 1)[0].upper() in _RESERVED_NAMES):
            raise WindowsPathGuardError("path_unverified")
    return target


def _check_fixed_drive(api: Any, target: PureWindowsPath, expected: tuple | None = None,
                       *, created: bool = False) -> tuple:
    """Require a fixed, directly mapped NTFS volume with stable serial/device.

    Cloud-sync or privileged filesystem redirection cannot be excluded and
    remain unsupported even if these conventional checks pass.
    """
    try:
        info = api.volume_info(target.anchor)
    except Exception:
        raise WindowsPathGuardError("drive_unverified", created=created) from None
    if type(info) is not tuple or len(info) != 4:
        raise WindowsPathGuardError("drive_unverified", created=created)
    kind, fs, serial, device = info
    if kind == 4:
        raise WindowsPathGuardError("remote_unsupported", created=created)
    if (kind != 3 or fs != "NTFS" or type(serial) is not int or not 0 <= serial <= 0xFFFFFFFF
            or not isinstance(device, str)
            or not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", device, re.IGNORECASE)):
        raise WindowsPathGuardError("drive_unverified", created=created)
    if expected is not None and info != expected:
        raise WindowsPathGuardError("volume_changed", created=created)
    return info


@dataclass(frozen=True)
class _OpenedParent:
    path: str = field(repr=False)
    owner: WindowsFileHandle = field(repr=False)
    identity: DirectoryIdentity
    acl: Any = field(repr=False)


def _directory_tuple(identity: DirectoryIdentity) -> tuple[int, int, int, int]:
    return (identity.volume_serial, identity.file_index, identity.link_count, identity.attributes)


def _read_parent_acl(handle: int, backend: Any, required_access: int = _ANCESTOR_ACCESS):
    from core import windows_acl
    return windows_acl.read_stable_evidence(handle, kind="directory", required_access=required_access,
                                             policy="ancestor", backend=backend)


def _close_temporary_handle(owner: WindowsFileHandle, *, created: bool = False) -> None:
    """One close attempt only; do not retry an uncertain native handle."""
    try:
        owner.close()
    except BaseException:
        raise WindowsPathGuardError("release_unknown", created=created) from None


def _rewalk_parents(target: PureWindowsPath, parents: tuple[_OpenedParent, ...],
                    *, volume: tuple, api: Any, acl_backend: Any, created: bool) -> None:
    """Compare every still-open directory handle to a newly opened handle.

    This is not handle-relative traversal. Same-user ABA races are outside the
    stated threat model; ordinary rename exclusion needs real Windows tests.
    """
    from core import windows_acl
    _check_fixed_drive(api, target, volume, created=created)
    for parent in parents:
        try:
            if directory_identity(parent.owner.handle, backend=api) != parent.identity:
                raise WindowsPathGuardError("parent_changed", created=created)
            if parent.acl is not None:
                windows_acl.verify_unchanged(parent.owner.handle, parent.acl, backend=acl_backend)
            current = open_directory(parent.path, backend=api)
        except WindowsPathGuardError:
            raise
        except Exception as exc:
            code = ("release_unknown" if (getattr(exc, "owned_handle", None) is not None
                    or getattr(exc, "code", None) in ("resource_release_failed", "handle_close_failed"))
                    else "parent_unverified")
            raise WindowsPathGuardError(code, created=created) from None
        try:
            current_identity = directory_identity(current.handle, backend=api)
            if current_identity != parent.identity:
                raise WindowsPathGuardError("parent_changed", created=created)
            if parent.acl is not None:
                current_acl = _read_parent_acl(current.handle, acl_backend, parent.acl.required_access)
                if current_acl.observation != parent.acl.observation:
                    raise WindowsPathGuardError("parent_changed", created=created)
            # Bracket the reopened observation with checks on the original.
            if directory_identity(parent.owner.handle, backend=api) != parent.identity:
                raise WindowsPathGuardError("parent_changed", created=created)
            if parent.acl is not None:
                windows_acl.verify_unchanged(parent.owner.handle, parent.acl, backend=acl_backend)
        finally:
            _close_temporary_handle(current, created=created)


def _check_locked_target(locked: LockedObjectFD, *, api: Any, acl_backend: Any):
    from core import windows_acl
    created = locked.evidence.created
    try:
        fd = locked.fileno()
        handle = locked._fd_api.handle(fd)
        proof = locked.evidence.acl
        if handle != proof.handle or file_identity(handle, backend=api) != locked.evidence.identity:
            raise WindowsPathGuardError("target_changed", created=created)
        windows_acl.verify_unchanged(handle, proof, backend=acl_backend)
        return fd, handle, proof
    except WindowsPathGuardError:
        raise
    except Exception as exc:
        code = ("release_unknown" if getattr(exc, "code", None) in
                ("resource_release_failed", "handle_close_failed") else "target_unverified")
        raise WindowsPathGuardError(code, created=created) from None


def _rewalk_target(target: PureWindowsPath, locked: LockedObjectFD,
                   *, api: Any, acl_backend: Any) -> None:
    from core import windows_acl
    created = locked.evidence.created
    try:
        fd, handle, proof = _check_locked_target(locked, api=api, acl_backend=acl_backend)
        current = open_file(str(target), backend=api)
    except WindowsPathGuardError:
        raise
    except Exception as exc:
        code = ("release_unknown" if (getattr(exc, "owned_handle", None) is not None
                or getattr(exc, "code", None) in ("resource_release_failed", "handle_close_failed"))
                else "target_unverified")
        raise WindowsPathGuardError(code, created=created) from None
    try:
        identity = file_identity(current.handle, backend=api)
        access = windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE
        current_acl = windows_acl.read_stable_evidence(current.handle, kind="file", required_access=access,
                                                        policy="private", backend=acl_backend)
        if identity != locked.evidence.identity or current_acl.observation != proof.observation:
            raise WindowsPathGuardError("target_changed", created=created)
        _check_locked_target(locked, api=api, acl_backend=acl_backend)
    finally:
        _close_temporary_handle(current, created=created)


class GuardedPathLock:
    """Own a locked fd and all ancestor handles; no service authority yet.

    The fd returned by fileno() is borrowed. Do not close/transfer it outside
    this owner. This checks one target and its ancestors, not all runtime-role
    aliases, installation ownership, or the complete discovery snapshot.
    """
    def __init__(self, target: PureWindowsPath, parents: tuple[_OpenedParent, ...],
                 locked: LockedObjectFD, *, volume: tuple, api: Any, acl_backend: Any):
        self._target = target
        self._parents = parents
        self._locked = locked
        self._volume = volume
        self._api = api
        self._acl_backend = acl_backend
        self._closed = False
        self._invalid = False
        self._state_lock = threading.RLock()

    @property
    def created(self) -> bool:
        return self._locked.evidence.created

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    @property
    def object_evidence(self) -> ObjectLockEvidence:
        """Borrowed immutable evidence; valid only while this guard is held."""
        with self._state_lock:
            if self._closed:
                raise WindowsPathGuardError("lock_closed", created=self.created)
            if self._invalid:
                raise WindowsPathGuardError("lock_unverified", created=self.created)
            return self._locked.evidence

    @property
    def volume_evidence(self) -> tuple:
        with self._state_lock:
            if self._closed:
                raise WindowsPathGuardError("lock_closed", created=self.created)
            if self._invalid:
                raise WindowsPathGuardError("lock_unverified", created=self.created)
            return self._volume

    @property
    def parent_evidence(self) -> ParentChainEvidence:
        with self._state_lock:
            if self._closed:
                raise WindowsPathGuardError("lock_closed", created=self.created)
            if self._invalid:
                raise WindowsPathGuardError("lock_unverified", created=self.created)
            return ParentChainEvidence(str(self._target), tuple(
                DirectoryEvidence(p.path, p.acl) for p in self._parents))

    def fileno(self) -> int:
        with self._state_lock:
            if self._closed:
                raise WindowsPathGuardError("lock_closed", created=self.created)
            if self._invalid:
                raise WindowsPathGuardError("lock_unverified", created=self.created)
            return self._locked.fileno()

    def invalidate(self) -> None:
        """Mark evidence unusable without releasing a possibly parented lock."""
        with self._state_lock:
            self._invalid = True

    def revalidate(self, *, retain_on_failure: bool = False) -> None:
        """Rewalk; by default close on failure, or retain for ordered teardown."""
        if type(retain_on_failure) is not bool:
            raise WindowsPathGuardError("path_unverified", created=self.created)
        with self._state_lock:
            if self._closed:
                raise WindowsPathGuardError("lock_closed", created=self.created)
            if self._invalid:
                raise WindowsPathGuardError("lock_unverified", created=self.created)
            try:
                _rewalk_parents(self._target, self._parents, volume=self._volume, api=self._api,
                                acl_backend=self._acl_backend, created=self.created)
                _rewalk_target(self._target, self._locked, api=self._api,
                               acl_backend=self._acl_backend)
                _rewalk_parents(self._target, self._parents, volume=self._volume, api=self._api,
                                acl_backend=self._acl_backend, created=self.created)
                _check_locked_target(self._locked, api=self._api, acl_backend=self._acl_backend)
            except BaseException as exc:
                self._invalid = True
                if not retain_on_failure:
                    try:
                        self.close()
                    except WindowsPathGuardError:
                        raise
                if isinstance(exc, WindowsPathGuardError):
                    raise
                if isinstance(exc, Exception):
                    code = ("release_unknown" if getattr(exc, "code", None) in
                            ("resource_release_failed", "handle_close_failed") else "path_unverified")
                    raise WindowsPathGuardError(code, created=self.created) from None
                raise

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            failed = False
            try:
                self._locked.close()
            except BaseException:
                failed = True
            for parent in reversed(self._parents):
                try:
                    parent.owner.close()
                except BaseException:
                    failed = True
            if failed:
                raise WindowsPathGuardError("release_unknown", created=self.created)

    def __enter__(self):
        self.fileno()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def acquire_guarded_path_lock(path: str, *, create: bool = False, backend: Any = None,
                              fd_backend: Any = None, acl_backend: Any = None,
                              trusted_boundary: str | None = None) -> GuardedPathLock:
    """Hold and rewalk one local drive path's ancestors, then lock its target.

    Does not establish all runtime-role identities or authorize server/CLI
    startup. An optional internally selected trusted boundary relaxes only
    ACL policy above it; held identity, local-volume and reparse checks remain.
    The boundary does not grant permission to create or write. Omission keeps
    the original strict walk. Not equivalent to handle-relative opens;
    same-user malicious ABA and filesystem/network virtualization are outside
    this evidence. Do not
    use until the separately reviewed discovery gate supplies the remaining
    cross-role and locality evidence. Windows native behavior is not tested.
    """
    if not isinstance(create, bool):
        raise WindowsPathGuardError("path_unverified")
    target = _local_absolute_target(path)
    boundary_depth = None
    if trusted_boundary is not None:
        # Apply discovery's non-root, exact-prefix rules. This is not authority.
        from core import windows_paths
        try:
            drive, parts = windows_paths._parse(path)
            boundary_drive, boundary_parts = windows_paths._parse(trusted_boundary)
            if (drive != boundary_drive or len(boundary_parts) >= len(parts)
                    or boundary_parts != parts[:len(boundary_parts)]):
                raise ValueError("boundary mismatch")
            boundary_depth = len(boundary_parts)
        except (ValueError, windows_paths.PathEvidenceError):
            raise WindowsPathGuardError("boundary_unverified") from None
    parents: list[_OpenedParent] = []
    opened_directory: WindowsFileHandle | None = None
    opened_file: WindowsFileHandle | None = None
    locked: LockedObjectFD | None = None
    created = False
    try:
        api = _api(backend)
        volume = _check_fixed_drive(api, target)
        paths = tuple(reversed((target.parent, *target.parent.parents)))
        if len(paths) > 128:
            raise WindowsPathGuardError("path_unverified")
        seen = set()
        for index, pathpart in enumerate(paths):
            opened_directory = open_directory(str(pathpart), backend=api)
            identity = directory_identity(opened_directory.handle, backend=api)
            # Above the selected boundary retain held identity/reparse/volume
            # evidence, but do not require a private ancestor DACL.
            strict = boundary_depth is None or index >= boundary_depth
            access = _ANCESTOR_ACCESS | (0x2 if create and index == len(paths) - 1 else 0)
            proof = _read_parent_acl(opened_directory.handle, acl_backend, access) if strict else None
            if (identity.volume_serial != volume[2]
                    or (proof is not None and proof.observation.identity != _directory_tuple(identity))):
                raise WindowsPathGuardError("parent_unverified")
            key = (identity.volume_serial, identity.file_index)
            if key in seen:
                raise WindowsPathGuardError("parent_unverified")
            seen.add(key)
            parents.append(_OpenedParent(str(pathpart), opened_directory, identity, proof))
            opened_directory = None
        parent_tuple = tuple(parents)
        _rewalk_parents(target, parent_tuple, volume=volume, api=api, acl_backend=acl_backend, created=False)
        if create:
            # CREATE_NEW with a protected descriptor, not an inherited parent
            # DACL that might expose the lock before its post-check. The SID
            # comes from the separately verified direct creation parent.
            user = parent_tuple[-1].acl.observation.user
            raw = None
            try:
                raw = api.open_private_candidate(str(target), user_sid=user)
                if type(raw) is not int or not _valid_handle(raw):
                    # A malformed return cannot prove no native handle was lost.
                    raise WindowsPathGuardError("release_unknown", created=None)
                created = True
                opened_file = WindowsFileHandle(raw, api, created=True)
                raw = None  # ownership moved to opened_file
            except BaseException as exc:
                # If wrapping failed or was interrupted, close the still-owned
                # raw handle once; once wrapped the outer cleanup owns it.
                raw_owned = type(raw) is int and _valid_handle(raw) and opened_file is None
                close_failed = False
                if raw_owned:
                    created = True
                    try:
                        api.close(raw)
                    except BaseException:
                        close_failed = True
                outcome = True if created is True else getattr(exc, "created", None)
                created = outcome if type(outcome) is bool else None
                release = (close_failed or getattr(exc, "release_unknown", None) is True
                           or (not raw_owned and opened_file is None
                               and getattr(exc, "release_unknown", None) is not False
                               and outcome is not False)
                           or getattr(exc, "owned_handle", None) is not None
                           or getattr(exc, "code", None) in
                           ("release_unknown", "resource_release_failed", "handle_close_failed"))
                if not isinstance(exc, Exception):
                    try:
                        exc.created = created
                        exc.release_unknown = release
                    except BaseException:
                        pass
                    raise
                code = ("release_unknown" if release else
                        "target_create_unknown" if created is None else "target_unverified")
                raise WindowsPathGuardError(code, created=created) from None
        else:
            try:
                opened_file = open_file(str(target), backend=api)
            except OSError as exc:
                code = "release_unknown" if getattr(exc, "owned_handle", None) is not None else "target_unverified"
                raise WindowsPathGuardError(code, created=False) from None
        if file_identity(opened_file.handle, backend=api).volume_serial != volume[2]:
            raise WindowsPathGuardError("volume_changed", created=created)
        try:
            locked = lock_private_object(opened_file, fd_backend=fd_backend, acl_backend=acl_backend)
        except WindowsObjectLockError as exc:
            # lock_private_object consumed or reported ambiguous ownership;
            # do not attempt a second close on its failure path.
            opened_file = None
            raise WindowsPathGuardError(exc.code, created=exc.created) from None
        except BaseException:
            # The raw wrapper was invalidated before its uncertain close, or
            # ownership was transferred to the CRT. Never retry that HANDLE.
            if opened_file.closed:
                opened_file = None
            raise
        opened_file = None
        if locked.evidence.identity.volume_serial != volume[2]:
            raise WindowsPathGuardError("volume_changed", created=created)
        if created and (locked.evidence.acl.observation.owner != user
                        or not locked.evidence.acl.observation.control & 0x1000):
            raise WindowsPathGuardError("protection_unverified", created=True)
        _rewalk_parents(target, parent_tuple, volume=volume, api=api, acl_backend=acl_backend, created=created)
        _rewalk_target(target, locked, api=api, acl_backend=acl_backend)
        _rewalk_parents(target, parent_tuple, volume=volume, api=api, acl_backend=acl_backend, created=created)
        _check_locked_target(locked, api=api, acl_backend=acl_backend)
        return GuardedPathLock(target, parent_tuple, locked, volume=volume, api=api, acl_backend=acl_backend)
    except BaseException as exc:
        failed = False
        if locked is not None:
            try:
                locked.close()
            except BaseException:
                failed = True
        if opened_file is not None:
            try:
                opened_file.close()
            except BaseException:
                failed = True
        if opened_directory is not None:
            try:
                opened_directory.close()
            except BaseException:
                failed = True
        for parent in reversed(parents):
            try:
                parent.owner.close()
            except BaseException:
                failed = True
        if failed:
            raise WindowsPathGuardError("release_unknown", created=created) from None
        if isinstance(exc, WindowsPathGuardError):
            raise
        if isinstance(exc, Exception):
            code = ("release_unknown" if (getattr(exc, "release_unknown", False) is True
                    or getattr(exc, "owned_handle", None) is not None
                    or getattr(exc, "code", None) in ("resource_release_failed", "handle_close_failed"))
                    else "path_unverified")
            raise WindowsPathGuardError(code, created=created) from None
        # Keep an interrupt visible without losing the known creation result.
        try:
            if created is True or not hasattr(exc, "created"):
                exc.created = created
            if not hasattr(exc, "release_unknown"):
                # An interruption outside the explicit handoff may have left
                # an unreturned OS handle; absence of evidence is not release.
                exc.release_unknown = True
        except BaseException:
            pass
        raise


class WindowsReadError(RuntimeError):
    """Sanitized protected read failure, including uncertain release."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GuardedFileRead:
    marker: tuple[int, int, int, int, int]
    data: bytes | None = field(repr=False)
    protection: tuple = field(repr=False)
    identity: FileIdentity = field(repr=False)


def acl_protection(proof) -> tuple:
    """Exact owner/group/DACL evidence; no claim about SACLs or privileges."""
    observation = proof.observation
    return (observation.owner, observation.group, observation.control,
            observation.revision, observation.dacl_present, observation.dacl_null,
            observation.acl_header, tuple(ace.raw for ace in observation.aces))


def read_guarded_file(path: str, *, with_data: bool = True, max_bytes: int | None = None,
                      backend: Any = None, acl_backend: Any = None,
                      paths_backend: Any = None, trusted_boundary: str | None = None) -> GuardedFileRead:
    """Read a private local file with held parents and same-HANDLE markers.

    ``FileBasicInfo.ChangeTime`` is captured from the open HANDLE, not Python's
    platform-dependent st_ctime. No guarantee against changed-and-restored
    metadata or same-user ABA. The path lease and all opened handles are closed
    before return; any uncertain release is a failure.
    """
    from core import windows_acl, windows_paths
    from core.discovery import _WindowsPathMonitor, _windows_path_backend
    if type(with_data) is not bool or (max_bytes is not None and
            (type(max_bytes) is not int or max_bytes < 0)):
        raise WindowsReadError("read_unavailable")
    try:
        target = _local_absolute_target(path)
        api = _api(backend)
        volume = _check_fixed_drive(api, target)
        monitor = _WindowsPathMonitor(_windows_path_backend() if paths_backend is None else paths_backend)
    except Exception:
        raise WindowsReadError("read_unavailable") from None
    lease = None
    opened = None
    current = None
    result = None
    error = None
    try:
        lease = windows_paths.inspect(str(target), backend=monitor, kind="file", private=True,
                                      single_link=True, trusted_boundary=trusted_boundary)
        if not lease.evidence.exists:
            raise WindowsReadError("file_missing")
        opened = open_read_file(str(target), backend=api)
        first_id = file_identity(opened.handle, backend=api)
        observed = lease.evidence.components[-1].observation
        if (observed.volume != first_id.volume_serial or
                int.from_bytes(observed.file_id, "big") != first_id.file_index or
                observed.links != first_id.link_count or first_id.volume_serial != volume[2]):
            raise WindowsReadError("file_changed")
        proof = windows_acl.read_stable_evidence(opened.handle, kind="file",
                         required_access=windows_acl.FILE_GENERIC_READ, backend=acl_backend)
        if proof.observation.identity != (first_id.volume_serial, first_id.file_index,
                                          first_id.link_count, first_id.attributes):
            raise WindowsReadError("file_changed")
        before = api.file_marker(opened.handle)
        if not isinstance(before, tuple) or len(before) != 5 or before[:2] != (first_id.volume_serial, first_id.file_index):
            raise WindowsReadError("file_unverified")
        if any(type(value) is not int or value < 0 for value in before) or before[2] <= 0 or before[3] <= 0:
            raise WindowsReadError("file_unverified")
        data = api.read_all(opened.handle, max_bytes=max_bytes) if with_data else None
        if with_data and (not isinstance(data, bytes) or len(data) != before[4]):
            raise WindowsReadError("file_changed")
        if api.file_marker(opened.handle) != before:
            raise WindowsReadError("file_changed")
        lease.revalidate()
        _check_fixed_drive(api, target, volume)
        current = open_read_file(str(target), backend=api)
        current_id = file_identity(current.handle, backend=api)
        current_proof = windows_acl.read_stable_evidence(current.handle, kind="file",
                           required_access=windows_acl.FILE_GENERIC_READ, backend=acl_backend)
        if (current_id != first_id or api.file_marker(current.handle) != before
                or current_proof.observation != proof.observation):
            raise WindowsReadError("file_changed")
        windows_acl.verify_unchanged(opened.handle, proof, backend=acl_backend)
        if api.file_marker(opened.handle) != before:
            raise WindowsReadError("file_changed")
        lease.revalidate()
        result = GuardedFileRead(before, data, acl_protection(proof), first_id)
    except BaseException as exc:
        error = exc
    release_unknown = False
    for item in (current, opened, lease):
        if item is not None:
            try:
                item.close()
            except BaseException:
                release_unknown = True
    if (monitor.release_unknown or release_unknown or getattr(error, "owned_handle", None) is not None
            or getattr(error, "code", None) in (
                "resource_release_failed", "handle_close_failed", "close_failed", "release_unknown")):
        raise WindowsReadError("release_unknown") from None
    if error is not None:
        if isinstance(error, WindowsReadError):
            raise error
        if (isinstance(error, windows_paths.PathEvidenceError) and error.code in
                ("path_changed", "observation_changed", "lease_invalid")) or (
                isinstance(error, windows_acl.ACLVerificationError) and error.code == "evidence_changed"):
            raise WindowsReadError("file_changed") from None
        if isinstance(error, Exception):
            raise WindowsReadError("read_unavailable") from None
        raise error
    return result


class WindowsTemporaryWriteError(RuntimeError):
    """A CREATE_NEW file remains; data or closure may be indeterminate."""
    def __init__(self, code: str, *, content_may_exist: bool):
        super().__init__(code)
        self.code = code
        self.created = True
        self.content_may_exist = content_may_exist


@dataclass(frozen=True)
class PrivateTemporaryWrite:
    identity: FileIdentity
    marker: tuple[int, int, int, int, int]
    acl: Any = field(repr=False)


def write_private_new_file(owned: WindowsFileHandle, data: bytes, *, acl_backend: Any = None) -> PrivateTemporaryWrite:
    """Write/flush an already CREATE_NEW file only after same-handle ACL checks.

    The caller must separately prove parent/path/cross-role safety and owns the
    still-open handle on success. This is NOT an atomic record replacement.
    On failure a created file is never silently removed; closing is attempted
    once and ``content_may_exist`` records whether writing was attempted.
    """
    from core import windows_acl
    if not isinstance(owned, WindowsFileHandle) or not isinstance(data, bytes):
        raise ValueError("new file handle and bytes required")
    # The HANDLE must not be closed or transferred while WriteFile is in use.
    with owned._state_lock:
        if owned._handle is None or owned.created is not True:
            raise ValueError("new file handle and bytes required")
        api = owned._backend
        attempted = False
        try:
            handle = owned.handle
            identity = file_identity(handle, backend=api)
            proof = windows_acl.read_stable_evidence(handle, kind="file", policy="private",
                        required_access=windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE,
                        backend=acl_backend)
            if proof.observation.identity != (identity.volume_serial, identity.file_index,
                                              identity.link_count, identity.attributes):
                raise WindowsTemporaryWriteError("identity_changed", content_may_exist=False)
            before = api.file_marker(handle)
            if (not isinstance(before, tuple) or len(before) != 5 or before[:2] !=
                    (identity.volume_serial, identity.file_index) or before[4] != 0):
                raise WindowsTemporaryWriteError("file_unverified", content_may_exist=False)
            attempted = True
            api.write_all_and_flush(handle, data)
            after = api.file_marker(handle)
            if (not isinstance(after, tuple) or len(after) != 5 or after[:2] != before[:2]
                    or after[4] != len(data) or file_identity(handle, backend=api) != identity):
                raise WindowsTemporaryWriteError("file_changed", content_may_exist=True)
            windows_acl.verify_unchanged(handle, proof, backend=acl_backend)
            return PrivateTemporaryWrite(identity, after, proof)
        except BaseException as exc:
            release = getattr(exc, "code", None) in ("resource_release_failed", "handle_close_failed")
            try:
                owned.close()
            except BaseException:
                # CloseHandle outcome is unknown: invalidate this wrapper and
                # do not permit an accidental second close of a reused HANDLE.
                owned._handle = None
                release = True
            if release:
                raise WindowsTemporaryWriteError("release_unknown", content_may_exist=attempted) from None
            if isinstance(exc, WindowsTemporaryWriteError):
                raise
            if isinstance(exc, Exception):
                raise WindowsTemporaryWriteError("write_failed", content_may_exist=attempted) from None
            raise

class WindowsPrivateCreationError(RuntimeError):
    """Failure with no ACL repair; created=None means creation outcome unknown."""
    def __init__(self, code: str, *, created=None, release_unknown=False):
        super().__init__(code)
        self.code = code
        self.created = created
        self.release_unknown = release_unknown


@dataclass(frozen=True, repr=False)
class PrivateCreationResult:
    created: bool
    identity: tuple = field(repr=False)
    protection: tuple = field(repr=False)


def _ensure_private_entry(path, kind, *, required_access, backend=None,
                          acl_backend=None, paths_backend=None, trusted_boundary=None):
    from core import windows_acl, windows_paths
    from core.discovery import _WindowsPathMonitor, _windows_path_backend
    if kind not in ('file', 'directory') or type(required_access) is not int or required_access <= 0:
        raise WindowsPrivateCreationError('invalid_arguments', created=False)
    initial = final = owned = None
    created = False
    creation_inflight = False
    error = None
    result = None
    release_unknown = False
    try:
        api = _api(backend)
        monitor = _WindowsPathMonitor(_windows_path_backend() if paths_backend is None else paths_backend)
        initial = windows_paths.inspect(path, backend=monitor, kind=kind, private=True,
                                        single_link=(kind == 'file'), trusted_boundary=trusted_boundary)
        ev = initial.evidence
        if ev.exists:
            if kind == 'file':
                raise WindowsPrivateCreationError('already_exists', created=False)
            target = initial
        else:
            if not ev.direct_parent_observed:
                raise WindowsPrivateCreationError('parent_missing', created=False)
            initial.revalidate()
            parent = initial._handles[-1]
            access = windows_paths._DIR_CHECK_ACCESS | (0x4 if kind == 'directory' else 0x2)
            proof = windows_acl.read_stable_evidence(parent.raw, kind='directory',
                required_access=access, policy='ancestor', backend=acl_backend)
            user = proof.observation.user
            creation_inflight = True
            if kind == 'directory':
                api.create_private_directory(path, user_sid=user)
            else:
                handle = api.open_private_candidate(path, user_sid=user)
                try:
                    owned = WindowsFileHandle(handle, api, created=True)
                except BaseException:
                    try:
                        api.close(handle)
                    except BaseException:
                        release_unknown = True
                    created = True
                    raise
            created = True
            creation_inflight = False
            final = windows_paths.inspect(path, backend=monitor, kind=kind, private=True,
                                          single_link=(kind == 'file'), trusted_boundary=trusted_boundary)
            if not final.evidence.exists or len(final.evidence.components) != len(ev.components) + 1:
                raise WindowsPrivateCreationError('created_path_unverified', created=True)
            if any(a.name != b.name or a.observation != b.observation
                   for a, b in zip(ev.components, final.evidence.components[:-1])):
                raise WindowsPrivateCreationError('parent_changed', created=True)
            target = final
        raw = target._handles[-1].raw
        proof = windows_acl.read_stable_evidence(raw, kind=kind, policy='private',
                                                 required_access=required_access, backend=acl_backend)
        obs = target.evidence.components[-1].observation
        ident = proof.observation.identity
        if (ident[0] != obs.volume or ident[1] != int.from_bytes(obs.file_id, 'big') or
                ident[2] != obs.links):
            raise WindowsPrivateCreationError('identity_unverified', created=created)
        if created and (proof.observation.owner != user or not proof.observation.control & 0x1000):
            raise WindowsPrivateCreationError('protection_unverified', created=True)
        if owned is not None:
            own_ident = file_identity(owned.handle, backend=api)
            marker = api.file_marker(owned.handle)
            if ((own_ident.volume_serial, own_ident.file_index, own_ident.link_count, own_ident.attributes) != ident
                    or marker[:2] != ident[:2] or marker[4] != 0):
                raise WindowsPrivateCreationError('file_unverified', created=True)
            own_proof = windows_acl.read_stable_evidence(owned.handle, kind='file', policy='private',
                                                          required_access=required_access, backend=acl_backend)
            if acl_protection(own_proof) != acl_protection(proof):
                raise WindowsPrivateCreationError('protection_unverified', created=True)
            windows_acl.verify_unchanged(owned.handle, own_proof, backend=acl_backend)
        target.revalidate()
        windows_acl.verify_unchanged(raw, proof, backend=acl_backend)
        result = PrivateCreationResult(created, ident, acl_protection(proof))
    except BaseException as exc:
        error = exc
        reported_created = getattr(exc, 'created', None)
        reported_release = getattr(exc, 'release_unknown', None)
        if (kind == 'file' and creation_inflight and not created
                and reported_created is not False and reported_release is not False):
            # A create call that did not transfer a HANDLE may still have
            # allocated one. Do not guess its identity or attempt a close.
            release_unknown = True
        if creation_inflight and type(reported_created) is bool:
            # Preserve an already observed creation; otherwise use the
            # lower-level result only when it is explicitly known.
            created = created or reported_created
            creation_inflight = False
        if getattr(exc, 'release_unknown', False) or getattr(exc, 'code', None) in (
                'release_unknown', 'close_failed', 'resource_release_failed', 'handle_close_failed'):
            release_unknown = True
    for item in (owned, final, initial):
        if item is not None:
            try:
                item.close()
            except BaseException:
                release_unknown = True
    if 'monitor' in locals() and monitor.release_unknown:
        release_unknown = True
    if error is not None or release_unknown:
        code = 'release_unknown' if release_unknown else getattr(error, 'code', 'creation_unverified')
        state = None if creation_inflight else created
        raise WindowsPrivateCreationError(code, created=state,
                                          release_unknown=release_unknown) from None
    return result


def ensure_private_directory(path: str, *, required_access: int = 0x200A1,
                             backend=None, acl_backend=None, paths_backend=None,
                             trusted_boundary: str | None = None) -> PrivateCreationResult:
    """Verify an existing private directory or create exactly one missing leaf.

    The selected boundary only scopes ancestor ACL checks, not authorization;
    the actual creation parent and its required access are checked separately.
    Never changes an existing ACL. Missing ancestors, races and unverified
    creation/protection fail closed; this is not a same-user race guarantee.
    """
    return _ensure_private_entry(path, 'directory', required_access=required_access,
                                 backend=backend, acl_backend=acl_backend, paths_backend=paths_backend,
                                 trusted_boundary=trusted_boundary)


def create_private_empty_file(path: str, *, required_access: int = 0x0012019f,
                              backend=None, acl_backend=None, paths_backend=None,
                              trusted_boundary: str | None = None) -> PrivateCreationResult:
    """CREATE_NEW one empty private file; existing paths are never overwritten."""
    return _ensure_private_entry(path, 'file', required_access=required_access,
                                 backend=backend, acl_backend=acl_backend, paths_backend=paths_backend,
                                 trusted_boundary=trusted_boundary)
