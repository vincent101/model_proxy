"""Windows TCP and same-process-handle evidence, not authority.

Native behavior is untested. The table attributes a local connection's bind
context to a PID; it does not prove exclusive ownership or prevent handle
transfer, same-user impersonation, PID reuse or check/use races. Callers must
independently prove installation, service entry, effective config and records.
An explicitly opened termination handle can stop only after its caller
independently verifies the entry while that same process handle remains held.
"""
from __future__ import annotations

import ctypes
import errno
import ipaddress
import math
import ntpath
import os
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

DWORD = ctypes.c_uint32
ULONG = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
WIN_AF_INET, WIN_AF_INET6 = 2, 23
ERROR_SUCCESS, ERROR_INSUFFICIENT_BUFFER = 0, 122
TCP_TABLE_OWNER_PID_CONNECTIONS = 4
MIB_TCP_STATE_ESTAB = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
MAX_TABLE_BYTES = 16 * 1024 * 1024
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class ProcessEvidenceError(RuntimeError):
    """Fixed sanitized code; never includes raw paths, socket rows or OS text."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ConnectionTuple:
    family: int
    local_ip: bytes = field(repr=False)
    local_port: int
    peer_ip: bytes = field(repr=False)
    peer_port: int

    def __post_init__(self):
        size = 4 if self.family == socket.AF_INET else 16 if self.family == socket.AF_INET6 else 0
        if (not size or not isinstance(self.local_ip, bytes) or len(self.local_ip) != size
                or not isinstance(self.peer_ip, bytes) or len(self.peer_ip) != size
                or type(self.local_port) is not int or not 1 <= self.local_port <= 65535
                or type(self.peer_port) is not int or not 1 <= self.peer_port <= 65535):
            raise ProcessEvidenceError("socket_endpoint_unavailable")


@dataclass(frozen=True)
class TableRow:
    family: int
    local_ip: bytes = field(repr=False)
    local_port: int
    remote_ip: bytes = field(repr=False)
    remote_port: int
    state: int
    pid: int
    local_scope: int = 0
    remote_scope: int = 0


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_ticks: int
    executable: str = field(repr=False)

    @property
    def start(self) -> dict:
        return {"kind": "windows_filetime", "ticks_100ns": self.creation_ticks}


def parse_owner_pid_table(family: int, data: bytes) -> tuple[TableRow, ...]:
    """Parse bounded MIB_TCP{,6}TABLE_OWNER_PID without assuming trailing size.

    Only the first two bytes of each DWORD port are used (network order).
    IPv6 scope IDs are decoded in network order and checked for matching rows.
    """
    if family not in (socket.AF_INET, socket.AF_INET6):
        raise ProcessEvidenceError("tcp_table_family_unsupported")
    if not isinstance(data, bytes) or len(data) < 4 or len(data) > MAX_TABLE_BYTES:
        raise ProcessEvidenceError("tcp_table_invalid")
    count = struct.unpack_from("<I", data)[0]
    width = 24 if family == socket.AF_INET else 56
    if count > (len(data) - 4) // width:
        raise ProcessEvidenceError("tcp_table_invalid")
    rows = []
    for index in range(count):
        raw = data[4 + index * width:4 + (index + 1) * width]
        if family == socket.AF_INET:
            state = struct.unpack_from("<I", raw, 0)[0]
            local_ip, local_port = raw[4:8], struct.unpack_from("!H", raw, 8)[0]
            remote_ip, remote_port = raw[12:16], struct.unpack_from("!H", raw, 16)[0]
            pid = struct.unpack_from("<I", raw, 20)[0]
            local_scope = remote_scope = 0
        else:
            local_ip, local_scope = raw[0:16], struct.unpack_from("!I", raw, 16)[0]
            local_port = struct.unpack_from("!H", raw, 20)[0]
            remote_ip, remote_scope = raw[24:40], struct.unpack_from("!I", raw, 40)[0]
            remote_port = struct.unpack_from("!H", raw, 44)[0]
            state, pid = struct.unpack_from("<II", raw, 48)
        if not 1 <= state <= 12:
            raise ProcessEvidenceError("tcp_table_invalid")
        if state == MIB_TCP_STATE_ESTAB and (not local_port or not remote_port or not pid):
            raise ProcessEvidenceError("tcp_table_invalid")
        rows.append(TableRow(family, local_ip, local_port, remote_ip, remote_port,
                             state, pid, local_scope, remote_scope))
    return tuple(rows)


def socket_endpoint(sock: socket.socket) -> ConnectionTuple:
    if not isinstance(sock, socket.socket):
        raise ProcessEvidenceError("socket_endpoint_unavailable")
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or sock.type & 0xF != socket.SOCK_STREAM:
        raise ProcessEvidenceError("socket_family_unsupported")
    try:
        local, peer = sock.getsockname(), sock.getpeername()
        local_ip, peer_ip = ipaddress.ip_address(local[0]), ipaddress.ip_address(peer[0])
        # A specific LAN bind can be controlled only when both ends use the
        # very same local address.  Different non-loopback addresses could be
        # routed to another host and are not evidence of a local accepted peer.
        if not (local_ip.is_loopback and peer_ip.is_loopback):
            if (local_ip != peer_ip or local_ip.is_unspecified or local_ip.is_multicast
                    or local_ip.is_loopback or peer_ip.is_loopback):
                raise ProcessEvidenceError("non_loopback_unsupported")
        if sock.family == socket.AF_INET6:
            if local[3] or peer[3] or local_ip.ipv4_mapped or peer_ip.ipv4_mapped:
                raise ProcessEvidenceError("socket_scope_unsupported")
        if local_ip.version != (4 if sock.family == socket.AF_INET else 6) or peer_ip.version != local_ip.version:
            raise ProcessEvidenceError("socket_endpoint_unavailable")
        if local_ip.packed == peer_ip.packed and local[1] == peer[1]:
            # Forward and reverse tuples would be indistinguishable; a client
            # row cannot then establish a distinct accepted server socket.
            raise ProcessEvidenceError("socket_tuple_ambiguous")
        return ConnectionTuple(sock.family, local_ip.packed, local[1], peer_ip.packed, peer[1])
    except ProcessEvidenceError:
        raise
    except (OSError, ValueError, IndexError, TypeError):
        raise ProcessEvidenceError("socket_endpoint_unavailable") from None


def socket_open_without_data(sock: socket.socket) -> None:
    try:
        readable, _, exceptional = select.select([sock], [], [sock], 0)
        if readable or exceptional:
            # Do not consume or request application data prior to authentication.
            raise ProcessEvidenceError("socket_closed_or_unsolicited_data")
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            raise ProcessEvidenceError("socket_error")
    except ProcessEvidenceError:
        raise
    except (OSError, ValueError, TypeError):
        raise ProcessEvidenceError("socket_unavailable") from None


def _socket_handle(sock) -> int:
    try:
        value = sock.fileno()
        if type(value) is not int or value < 0:
            raise ProcessEvidenceError("socket_unavailable")
        return value
    except ProcessEvidenceError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise ProcessEvidenceError("socket_unavailable") from None


def find_connection_owner(endpoint: ConnectionTuple, rows: tuple[TableRow, ...]) -> int:
    if not isinstance(rows, tuple) or len(rows) > (MAX_TABLE_BYTES - 4) // 24:
        raise ProcessEvidenceError("tcp_table_invalid")
    matches = []
    size = 4 if endpoint.family == socket.AF_INET else 16
    for row in rows:
        if (not isinstance(row, TableRow) or row.family != endpoint.family
                or not isinstance(row.local_ip, bytes) or len(row.local_ip) != size
                or not isinstance(row.remote_ip, bytes) or len(row.remote_ip) != size
                or type(row.local_port) is not int or not 0 <= row.local_port <= 65535
                or type(row.remote_port) is not int or not 0 <= row.remote_port <= 65535
                or type(row.state) is not int or not 1 <= row.state <= 12
                or type(row.pid) is not int or not 0 <= row.pid <= 0xFFFFFFFF
                or type(row.local_scope) is not int or not 0 <= row.local_scope <= 0xFFFFFFFF
                or type(row.remote_scope) is not int or not 0 <= row.remote_scope <= 0xFFFFFFFF):
            raise ProcessEvidenceError("tcp_table_invalid")
        if (row.local_ip == endpoint.peer_ip and row.local_port == endpoint.peer_port
                and row.remote_ip == endpoint.local_ip and row.remote_port == endpoint.local_port):
            if row.local_scope or row.remote_scope:
                raise ProcessEvidenceError("socket_scope_unsupported")
            matches.append(row)
    if not matches:
        raise ProcessEvidenceError("connection_owner_not_found")
    if len(matches) != 1:
        raise ProcessEvidenceError("connection_owner_ambiguous")
    if matches[0].state != MIB_TCP_STATE_ESTAB:
        raise ProcessEvidenceError("connection_owner_not_found")
    if matches[0].pid <= 1:
        raise ProcessEvidenceError("tcp_table_invalid")
    return matches[0].pid


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class NativeBackend:
    """Lazy typed Win32 bindings. Never loaded at import or on non-Windows."""
    def __init__(self, *, terminate: bool = False):
        if os.name != "nt":
            raise ProcessEvidenceError("platform_unavailable")
        self._terminate_access = terminate
        try:
            self._iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self._iphlp.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(DWORD), BOOL,
                                                        ULONG, DWORD, ULONG]
            self._iphlp.GetExtendedTcpTable.restype = DWORD
            self._kernel.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
            self._kernel.OpenProcess.restype = HANDLE
            self._kernel.GetProcessId.argtypes = [HANDLE]
            self._kernel.GetProcessId.restype = DWORD
            self._kernel.GetProcessTimes.argtypes = [HANDLE] + [ctypes.POINTER(_FILETIME)] * 4
            self._kernel.GetProcessTimes.restype = BOOL
            self._kernel.QueryFullProcessImageNameW.argtypes = [HANDLE, DWORD, ctypes.c_wchar_p,
                                                                ctypes.POINTER(DWORD)]
            self._kernel.QueryFullProcessImageNameW.restype = BOOL
            self._kernel.WaitForSingleObject.argtypes = [HANDLE, DWORD]
            self._kernel.WaitForSingleObject.restype = DWORD
            self._kernel.TerminateProcess.argtypes = [HANDLE, DWORD]
            self._kernel.TerminateProcess.restype = BOOL
            self._kernel.CloseHandle.argtypes = [HANDLE]
            self._kernel.CloseHandle.restype = BOOL
        except (OSError, AttributeError, TypeError):
            raise ProcessEvidenceError("platform_unavailable") from None

    def tcp_rows(self, family: int) -> tuple[TableRow, ...]:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ProcessEvidenceError("tcp_table_family_unsupported")
        win_family = WIN_AF_INET if family == socket.AF_INET else WIN_AF_INET6
        size = DWORD(0)
        code = self._iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, win_family,
                                               TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if code != ERROR_INSUFFICIENT_BUFFER or not 4 <= size.value <= MAX_TABLE_BYTES:
            raise ProcessEvidenceError("tcp_table_unavailable")
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            actual = DWORD(size.value)
            code = self._iphlp.GetExtendedTcpTable(buffer, ctypes.byref(actual), False, win_family,
                                                   TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
            if code == ERROR_SUCCESS and 4 <= actual.value <= len(buffer):
                return parse_owner_pid_table(family, buffer.raw[:actual.value])
            if (code != ERROR_INSUFFICIENT_BUFFER or actual.value <= size.value
                    or actual.value > MAX_TABLE_BYTES):
                break
            size = DWORD(actual.value)
        raise ProcessEvidenceError("tcp_table_unavailable")

    def open_process(self, pid: int) -> int:
        rights = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if getattr(self, "_terminate_access", False):
            rights |= PROCESS_TERMINATE
        value = self._kernel.OpenProcess(rights, False, pid)
        if not value or value == INVALID_HANDLE_VALUE:
            if getattr(ctypes, "get_last_error", lambda: 0)() == 87:  # ERROR_INVALID_PARAMETER
                raise ProcessEvidenceError("process_missing")
            raise ProcessEvidenceError("process_open_denied")
        return value

    def process_id(self, handle: int) -> int:
        value = int(self._kernel.GetProcessId(handle))
        if not value:
            raise ProcessEvidenceError("process_identity_unavailable")
        return value

    def creation_ticks(self, handle: int) -> int:
        created, exited, kernel, user = (_FILETIME() for _ in range(4))
        if not self._kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
            raise ProcessEvidenceError("process_times_unavailable")
        value = (int(created.high) << 32) | int(created.low)
        if not value:
            raise ProcessEvidenceError("process_times_unavailable")
        return value

    def image(self, handle: int) -> str:
        output = ctypes.create_unicode_buffer(32768)
        size = DWORD(len(output))
        if not self._kernel.QueryFullProcessImageNameW(handle, 0, output, ctypes.byref(size)):
            raise ProcessEvidenceError("process_image_unavailable")
        if not 0 < size.value < len(output) or not output.value or len(output.value) > size.value:
            raise ProcessEvidenceError("process_image_unavailable")
        return output.value

    def assert_alive(self, handle: int) -> None:
        result = self._kernel.WaitForSingleObject(handle, 0)
        if result == WAIT_OBJECT_0:
            raise ProcessEvidenceError("process_exited")
        if result != WAIT_TIMEOUT:
            raise ProcessEvidenceError("process_liveness_unavailable")

    def terminate_and_wait(self, handle: int, timeout: float) -> None:
        if not self._terminate_access:
            raise ProcessEvidenceError("termination_access_unavailable")
        if not self._kernel.TerminateProcess(handle, 1):
            raise ProcessEvidenceError("termination_failed")
        result = self._kernel.WaitForSingleObject(handle, min(int(timeout * 1000), 0xfffffffe))
        if result == WAIT_TIMEOUT:
            raise ProcessEvidenceError("stop_timeout")
        if result != WAIT_OBJECT_0:
            raise ProcessEvidenceError("stop_wait_unavailable")

    def close(self, handle: int) -> None:
        if not self._kernel.CloseHandle(handle):
            raise ProcessEvidenceError("process_release_unknown")


class _ProcessHandle:
    """Owns one non-inherited OpenProcess handle until explicit close."""
    def __init__(self, backend: Any, pid: int):
        self._backend = backend
        self._lock = threading.RLock()
        self._release_unknown = False
        self._handle = backend.open_process(pid)
        if type(self._handle) is not int or self._handle in (0, -1, INVALID_HANDLE_VALUE):
            raise ProcessEvidenceError("process_open_denied")

    def read(self, expected: ProcessIdentity) -> None:
        with self._lock:
            if self._handle is None:
                raise ProcessEvidenceError("process_handle_closed")
            handle = self._handle
            try:
                if self._backend.assert_alive(handle) is not None:
                    raise ProcessEvidenceError("process_liveness_unavailable")
                pid = self._backend.process_id(handle)
                first = self._backend.creation_ticks(handle)
                image = self._backend.image(handle)
                second = self._backend.creation_ticks(handle)
                if self._backend.assert_alive(handle) is not None:
                    raise ProcessEvidenceError("process_liveness_unavailable")
                if (type(pid) is not int or type(first) is not int or type(second) is not int
                        or not isinstance(image, str) or pid != expected.pid
                        or first != expected.creation_ticks or second != expected.creation_ticks
                        or image != expected.executable):
                    raise ProcessEvidenceError("process_identity_changed")
            except ProcessEvidenceError:
                raise
            except Exception:
                raise ProcessEvidenceError("process_identity_unavailable") from None

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                if self._release_unknown:
                    raise ProcessEvidenceError("process_release_unknown")
                return
            handle, self._handle = self._handle, None
            try:
                self._backend.close(handle)
            except BaseException:
                self._release_unknown = True
                raise ProcessEvidenceError("process_release_unknown") from None


def _safe_image_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\0" in value or value.startswith(("\\\\", "//")):
        return False
    drive, tail = ntpath.splitdrive(value)
    if (len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha()
            or not tail.startswith("\\") or ntpath.normpath(value) != value):
        return False
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT")
        for number in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³")
    }
    return all(part and not part.endswith((" ", ".")) and
               part.split(".", 1)[0].upper() not in reserved and
               not any(ord(c) < 32 or c in '<>:"|?*' for c in part)
               for part in tail.split("\\")[1:])


def _expected(pid: int, start: dict, executable: str) -> ProcessIdentity:
    if (type(pid) is not int or not 1 < pid <= 0xFFFFFFFF or pid == os.getpid()
            or not isinstance(start, dict) or set(start) != {"kind", "ticks_100ns"}
            or start.get("kind") != "windows_filetime" or type(start.get("ticks_100ns")) is not int
            or not 0 < start["ticks_100ns"] <= 0xFFFFFFFFFFFFFFFF
            or not _safe_image_path(executable)):
        raise ProcessEvidenceError("invalid_process_identity")
    return ProcessIdentity(pid, start["ticks_100ns"], executable)


class ProcessObservation:
    """Hold one verified process handle; not installation or socket authority."""
    def __init__(self, identity: ProcessIdentity, process: _ProcessHandle):
        self.identity = identity
        self._process = process
        self._closed = False
        self._failed = False
        self._release_unknown = False
        self._lock = threading.RLock()

    def revalidate(self) -> None:
        with self._lock:
            if self._closed:
                raise ProcessEvidenceError("process_handle_closed")
            if self._failed:
                raise ProcessEvidenceError("process_evidence_invalid")
            try:
                self._process.read(self.identity)
            except BaseException:
                self._failed = True
                raise

    def terminate_checked(self, verify_entry, *, timeout: float) -> None:
        """Recheck identity around independent entry verification and stop this handle.

        The callback must return exactly True after inspecting the target entry;
        it cannot be a mere assertion based on a PID or record. No PID lookup is
        performed between the final handle check and TerminateProcess.
        """
        if (not callable(verify_entry) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise ProcessEvidenceError("termination_request_invalid")
        with self._lock, self._process._lock:
            if self._closed or self._failed:
                raise ProcessEvidenceError("process_evidence_invalid")
            try:
                self._process.read(self.identity)
                if verify_entry(self.identity) is not True:
                    raise ProcessEvidenceError("entry_unverified")
                self._process.read(self.identity)
                self._process._backend.terminate_and_wait(self._process._handle, timeout)
                self._failed = True  # the observation cannot be reused after termination
            except BaseException:
                self._failed = True
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                if self._release_unknown:
                    raise ProcessEvidenceError("process_release_unknown")
                return
            self._closed = True
            try:
                self._process.close()
            except BaseException:
                self._release_unknown = True
                raise ProcessEvidenceError("process_release_unknown") from None

    def __enter__(self):
        with self._lock:
            if self._closed or self._failed:
                raise ProcessEvidenceError("process_evidence_invalid")
            return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def open_verified_process(pid: int, start: dict, executable: str, *, backend: Any = None,
                          terminate: bool = False) -> ProcessObservation:
    """Pin and verify one process handle without making a control decision."""
    expected = _expected(pid, start, executable)
    if type(terminate) is not bool:
        raise ProcessEvidenceError("termination_request_invalid")
    api = (NativeBackend(terminate=True) if terminate else NativeBackend()) if backend is None else backend
    try:
        process = _ProcessHandle(api, pid)
    except ProcessEvidenceError:
        raise
    except Exception:
        raise ProcessEvidenceError("process_open_denied") from None
    result = ProcessObservation(expected, process)
    try:
        result.revalidate()
        return result
    except BaseException:
        result.close()
        raise


def recorded_process_state(pid: int, start: dict, executable: str, *, backend: Any = None) -> str:
    """Return running, gone, or PID-reused; access/identity ambiguity raises."""
    expected = _expected(pid, start, executable)
    api = NativeBackend() if backend is None else backend
    try:
        handle = _ProcessHandle(api, pid)
    except ProcessEvidenceError as exc:
        if exc.code == "process_missing":
            return "gone"
        raise
    try:
        try:
            api.assert_alive(handle._handle)
            observed_pid = api.process_id(handle._handle)
            first = api.creation_ticks(handle._handle)
            image = api.image(handle._handle)
            second = api.creation_ticks(handle._handle)
            api.assert_alive(handle._handle)
        except ProcessEvidenceError as exc:
            if exc.code == "process_exited":
                return "gone"
            raise
        if observed_pid != expected.pid or type(first) is not int or first != second:
            raise ProcessEvidenceError("process_identity_unavailable")
        if first != expected.creation_ticks:
            return "reused"
        if image != expected.executable:
            raise ProcessEvidenceError("process_identity_changed")
        return "running"
    finally:
        handle.close()


class PeerLease:
    """Owns the verified process handle; socket stays caller-owned and open."""
    def __init__(self, sock, handle: int, endpoint: ConnectionTuple, expected: ProcessIdentity,
                 process: _ProcessHandle, backend: Any):
        self._sock, self._socket_handle = sock, handle
        self.endpoint, self.identity = endpoint, expected
        self._process, self._backend = process, backend
        self._closed = False
        self._failed = False
        self._release_unknown = False
        self._lock = threading.RLock()

    def revalidate(self) -> None:
        with self._lock:
            if self._closed:
                raise ProcessEvidenceError("peer_lease_closed")
            if self._failed:
                raise ProcessEvidenceError("peer_lease_invalid")
            try:
                if _socket_handle(self._sock) != self._socket_handle or socket_endpoint(self._sock) != self.endpoint:
                    raise ProcessEvidenceError("socket_endpoint_changed")
                socket_open_without_data(self._sock)
                self._process.read(self.identity)
                if find_connection_owner(self.endpoint, self._backend.tcp_rows(self.endpoint.family)) != self.identity.pid:
                    raise ProcessEvidenceError("connection_owner_changed")
                self._process.read(self.identity)
                if _socket_handle(self._sock) != self._socket_handle or socket_endpoint(self._sock) != self.endpoint:
                    raise ProcessEvidenceError("socket_endpoint_changed")
                socket_open_without_data(self._sock)
            except ProcessEvidenceError:
                self._failed = True
                raise
            except BaseException as exc:
                self._failed = True
                if isinstance(exc, Exception):
                    raise ProcessEvidenceError("connected_peer_unavailable") from None
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                if self._release_unknown:
                    raise ProcessEvidenceError("process_release_unknown")
                return
            self._closed = True
            try:
                self._process.close()
            except BaseException:
                self._release_unknown = True
                raise ProcessEvidenceError("process_release_unknown") from None

    def __enter__(self):
        with self._lock:
            if self._closed:
                raise ProcessEvidenceError("peer_lease_closed")
            if self._failed:
                raise ProcessEvidenceError("peer_lease_invalid")
            return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def verify_socket_owner(sock: socket.socket, pid: int, start: dict, executable: str, *,
                        backend: Any = None, timeout: float = 2.0, interval: float = 0.05,
                        clock=time.monotonic, sleep=time.sleep) -> PeerLease:
    """Return held same-process-handle evidence for this socket, or fail closed.

    No application data is sent, and the original socket is never reconnected.
    No matches may be retried until timeout; ambiguities and wrong PIDs fail now.
    """
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30
            or type(interval) not in (int, float) or not math.isfinite(interval) or not 0 < interval <= 1):
        raise ValueError("invalid timeout")
    expected = _expected(pid, start, executable)
    target, sock_handle = socket_endpoint(sock), _socket_handle(sock)
    api = NativeBackend() if backend is None else backend
    deadline = clock() + timeout
    while True:
        socket_open_without_data(sock)
        try:
            owner = find_connection_owner(target, api.tcp_rows(target.family))
        except ProcessEvidenceError as exc:
            if exc.code != "connection_owner_not_found":
                raise
            owner = None
        except Exception:
            raise ProcessEvidenceError("tcp_table_unavailable") from None
        if _socket_handle(sock) != sock_handle or socket_endpoint(sock) != target:
            raise ProcessEvidenceError("socket_endpoint_changed")
        if owner is not None:
            if owner != pid:
                raise ProcessEvidenceError("connection_owner_changed")
            try:
                process = _ProcessHandle(api, pid)
            except ProcessEvidenceError:
                raise
            except Exception:
                raise ProcessEvidenceError("process_open_denied") from None
            lease = PeerLease(sock, sock_handle, target, expected, process, api)
            try:
                lease.revalidate()
                return lease
            except BaseException:
                lease.close()
                raise
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProcessEvidenceError("connection_owner_not_found")
        sleep(min(interval, remaining))
