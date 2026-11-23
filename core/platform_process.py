"""OS process and connected-socket evidence; never establishes installation trust.

Darwin's 64-bit proc_info socket layout below is the public SDK ABI. A size
mismatch, inaccessible FD, changed process, or unsupported OS is UNKNOWN.
The caller must independently authenticate the installation and held locks.
POSIX kill remains subject to the unavoidable identity-check/kill race.
"""
from __future__ import annotations

import ctypes
import errno
import ipaddress
import os
import select
import signal
import socket
import struct
import sys
import time

from core.discovery import process_executable, process_start_identity


class ProcessEvidenceError(RuntimeError):
    pass


def _darwin_interface_addresses():
    """Read local interface addresses without DNS or network traffic."""
    if sys.platform != 'darwin':
        raise ProcessEvidenceError('local_address_unavailable')
    class IfAddrs(ctypes.Structure):
        pass
    IfAddrs._fields_ = [
        ('next', ctypes.POINTER(IfAddrs)), ('name', ctypes.c_char_p),
        ('flags', ctypes.c_uint), ('addr', ctypes.c_void_p),
        ('netmask', ctypes.c_void_p), ('destination', ctypes.c_void_p),
        ('data', ctypes.c_void_p),
    ]
    try:
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(IfAddrs))]
        libc.getifaddrs.restype = ctypes.c_int
        libc.freeifaddrs.argtypes = [ctypes.POINTER(IfAddrs)]
        libc.freeifaddrs.restype = None
        head = ctypes.POINTER(IfAddrs)()
        if libc.getifaddrs(ctypes.byref(head)) != 0 or not head:
            raise ProcessEvidenceError('local_address_unavailable')
        try:
            addresses, seen = set(), set()
            current = head
            while current:
                if len(seen) >= 4096:
                    raise ProcessEvidenceError('local_address_unavailable')
                pointer = ctypes.addressof(current.contents)
                if pointer in seen:
                    raise ProcessEvidenceError('local_address_unavailable')
                seen.add(pointer)
                item = current.contents
                if item.addr and item.flags & 1:  # IFF_UP
                    length, family = ctypes.string_at(item.addr, 2)
                    if family == socket.AF_INET:
                        if length < 8:
                            raise ProcessEvidenceError('local_address_unavailable')
                        addresses.add(ipaddress.ip_address(ctypes.string_at(item.addr + 4, 4)))
                    elif family == socket.AF_INET6:
                        if length < 28:
                            raise ProcessEvidenceError('local_address_unavailable')
                        addresses.add(ipaddress.ip_address(ctypes.string_at(item.addr + 8, 16)))
                current = item.next
            return addresses
        finally:
            libc.freeifaddrs(head)
    except ProcessEvidenceError:
        raise
    except (OSError, AttributeError, ValueError, TypeError):
        raise ProcessEvidenceError('local_address_unavailable') from None


def is_local_numeric_address(host: str) -> bool:
    """Accept only numeric local unicast IPs; scoped IPv6 is not supported."""
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_unspecified or ip.is_multicast
                or isinstance(ip, ipaddress.IPv6Address)
                and (ip.scope_id is not None or ip.ipv4_mapped is not None or ip.is_link_local)):
            return False
        if ip.is_loopback:
            return True
        return ip in _darwin_interface_addresses()
    except (ValueError, TypeError):
        return False


def darwin_launch_evidence(pid: int, start: dict, executable: str) -> tuple[tuple[str, ...], dict[str, str]]:
    """Read the kernel's initial argv and selected environment of a process.

    This establishes observed launch arguments, not a cryptographic attestation
    of Python code or its mutable environment. Same-user impersonation and
    check/use races remain possible. Never expose raw process environment.
    """
    if sys.platform != 'darwin':
        raise ProcessEvidenceError('process_launch_unsupported')
    check_identity(pid, start, executable)
    try:
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [(f'field_{i}', ctypes.c_uint32) for i in range(12)] + [
                ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
                (f'tail_{i}', ctypes.c_uint32) for i in range(6)] + [
                ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]

        libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        call = libproc.proc_pidinfo
        call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                         ctypes.c_void_p, ctypes.c_int]
        call.restype = ctypes.c_int
        info = ProcBsdInfo()
        if (call(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
                or info.field_3 != pid or info.field_5 != os.geteuid()
                or info.field_7 != os.getuid()):
            raise ProcessEvidenceError('process_owner_unverified')

        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        call = libc.sysctl
        call.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        call.restype = ctypes.c_int
        limit_mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
        limit = ctypes.c_int()
        limit_size = ctypes.c_size_t(ctypes.sizeof(limit))
        if (call(limit_mib, 2, ctypes.byref(limit), ctypes.byref(limit_size), None, 0) != 0
                or limit_size.value != ctypes.sizeof(limit)
                or not 4096 <= limit.value <= 8 * 1024 * 1024):
            raise ProcessEvidenceError('process_launch_unavailable')
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        size = ctypes.c_size_t(limit.value)
        buf = ctypes.create_string_buffer(size.value)
        if call(mib, 3, buf, ctypes.byref(size), None, 0) != 0 or not 8 <= size.value < len(buf):
            raise ProcessEvidenceError('process_launch_unavailable')
        raw = buf.raw[:size.value]
        argc = struct.unpack_from('=i', raw)[0]
        if not 1 <= argc <= 128:
            raise ProcessEvidenceError('process_launch_unavailable')
        end = raw.find(b'\0', 4)
        if end <= 4:
            raise ProcessEvidenceError('process_launch_unavailable')
        offset = end + 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        args = []
        for _ in range(argc):
            end = raw.find(b'\0', offset)
            if end < offset:
                raise ProcessEvidenceError('process_launch_unavailable')
            args.append(os.fsdecode(raw[offset:end]))
            offset = end + 1
        selected = {}
        keys = {'MODEL_PROXY_CONFIG', 'MODEL_PROXY_START_NONCE', 'MODEL_PROXY_PORT'}
        while offset < len(raw):
            end = raw.find(b'\0', offset)
            if end < 0:
                raise ProcessEvidenceError('process_launch_unavailable')
            item = raw[offset:end]
            offset = end + 1
            if not item:
                break
            key, separator, value = item.partition(b'=')
            if separator:
                name = os.fsdecode(key)
                if name in keys:
                    if name in selected:
                        raise ProcessEvidenceError('process_launch_unavailable')
                    selected[name] = os.fsdecode(value)
        else:
            raise ProcessEvidenceError('process_launch_unavailable')
        check_identity(pid, start, executable)
        return tuple(args), selected
    except ProcessEvidenceError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, struct.error):
        raise ProcessEvidenceError('process_launch_unavailable') from None


def check_identity(pid: int, start: dict, executable: str) -> None:
    if (type(pid) is not int or pid <= 1 or pid == os.getpid()
            or not isinstance(start, dict) or not start
            or not isinstance(executable, str) or not os.path.isabs(executable)):
        raise ProcessEvidenceError('invalid_process_identity')
    first = process_start_identity(pid)
    image = process_executable(pid)
    second = process_start_identity(pid)
    if first is None or second is None or image is None:
        raise ProcessEvidenceError('process_identity_unavailable')
    if first != start or second != start or image != executable:
        raise ProcessEvidenceError('process_identity_changed')


# The 64-bit local Darwin SDK's struct kinfo_proc (sys/sysctl.h). Unlike
# PROC_PIDTBSDINFO, KERN_PROC_PID remains observable for an unreaped zombie on
# the tested macOS. Reject other layouts rather than guessing their offsets.
_KINFO_PROC_SIZE = 648
_KINFO_START_SEC = 0
_KINFO_START_USEC = 8
_KINFO_STATUS = 36
_KINFO_PID = 40
_DARWIN_ACTIVE_STATES = frozenset((1, 2, 3, 4))
_DARWIN_ZOMBIE = 5


class _DarwinBsdInfo(ctypes.Structure):
    _fields_ = [(f'field_{i}', ctypes.c_uint32) for i in range(12)] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
        (f'tail_{i}', ctypes.c_uint32) for i in range(6)] + [
        ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]


def _darwin_kinfo_one(pid: int):
    """Read one exact kernel observation; never expose the rest of the record."""
    if (sys.platform != 'darwin' or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(ctypes.c_long) != 8 or sys.byteorder != 'little'):
        raise ProcessEvidenceError('process_state_unavailable')
    try:
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        call = libc.sysctl
        call.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        call.restype = ctypes.c_int
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buffer = ctypes.create_string_buffer(_KINFO_PROC_SIZE)
        size = ctypes.c_size_t(_KINFO_PROC_SIZE)
        ctypes.set_errno(0)
        if call(mib, 4, buffer, ctypes.byref(size), None, 0) != 0 or size.value != _KINFO_PROC_SIZE:
            raise ProcessEvidenceError('process_state_unavailable')
        raw = buffer.raw
        status = raw[_KINFO_STATUS]
        observed_pid = struct.unpack_from('<i', raw, _KINFO_PID)[0]
        seconds = struct.unpack_from('<q', raw, _KINFO_START_SEC)[0]
        microseconds = struct.unpack_from('<i', raw, _KINFO_START_USEC)[0]
        if (observed_pid != pid or seconds <= 0 or not 0 <= microseconds < 1_000_000
                or status not in _DARWIN_ACTIVE_STATES | {_DARWIN_ZOMBIE}):
            raise ProcessEvidenceError('process_state_unavailable')
        return observed_pid, seconds, microseconds, status
    except ProcessEvidenceError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, struct.error):
        raise ProcessEvidenceError('process_state_unavailable') from None


def _darwin_check_kinfo_abi():
    """Cross-check our ABI against a live self observation from libproc."""
    if (ctypes.sizeof(_DarwinBsdInfo) != 136
            or _DarwinBsdInfo.field_1.offset != 4 or _DarwinBsdInfo.field_3.offset != 12
            or _DarwinBsdInfo.start_sec.offset != 120 or _DarwinBsdInfo.start_usec.offset != 128):
        raise ProcessEvidenceError('process_state_unavailable')
    try:
        pid = os.getpid()
        libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        call = libproc.proc_pidinfo
        call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                         ctypes.c_void_p, ctypes.c_int]
        call.restype = ctypes.c_int
        info = _DarwinBsdInfo()
        if (call(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != 136
                or info.field_3 != pid or info.field_1 not in _DARWIN_ACTIVE_STATES
                or info.field_5 != os.geteuid()):
            raise ProcessEvidenceError('process_state_unavailable')
        current = _darwin_kinfo_one(pid)
        if (current[:3] != (pid, info.start_sec, info.start_usec)
                or current[3] not in _DARWIN_ACTIVE_STATES):
            raise ProcessEvidenceError('process_state_unavailable')
    except ProcessEvidenceError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        raise ProcessEvidenceError('process_state_unavailable') from None


def _validate_darwin_record(pid: int, start: dict) -> None:
    if (sys.platform != 'darwin' or type(pid) is not int or pid <= 1 or pid > 0x7fffffff
            or pid == os.getpid()
            or not isinstance(start, dict) or set(start) != {'kind', 'seconds', 'microseconds'}
            or start.get('kind') != 'darwin_proc_bsdinfo'
            or type(start.get('seconds')) is not int or not 0 < start['seconds'] <= 0x7fffffffffffffff
            or type(start.get('microseconds')) is not int
            or not 0 <= start['microseconds'] < 1_000_000):
        raise ProcessEvidenceError('process_state_unavailable')


def darwin_recorded_state(pid: int, start: dict) -> str:
    """Return ALIVE or ZOMBIE only for a twice-observed exact PID/start.

    This is not an image or installation proof. A same-user race remains
    between these observations and any later signal, lock or socket action.
    """
    _validate_darwin_record(pid, start)
    _darwin_check_kinfo_abi()
    try:
        os.kill(pid, 0)
        first = _darwin_kinfo_one(pid)
        second = _darwin_kinfo_one(pid)
        os.kill(pid, 0)
    except ProcessEvidenceError:
        raise
    except OSError:
        raise ProcessEvidenceError('process_state_unavailable') from None
    expected = (pid, start['seconds'], start['microseconds'])
    if first[:3] != expected or second[:3] != expected:
        raise ProcessEvidenceError('process_identity_changed')
    if first[3] == second[3] == _DARWIN_ZOMBIE:
        return 'ZOMBIE'
    if first[3] in _DARWIN_ACTIVE_STATES and second[3] in _DARWIN_ACTIVE_STATES:
        return 'ALIVE'
    raise ProcessEvidenceError('process_state_unavailable')


def signal_verified(pid: int, start: dict, executable: str) -> None:
    """Send exactly one SIGTERM, never SIGKILL; identity/kill is not atomic."""
    if sys.platform != 'darwin':
        raise ProcessEvidenceError('stop_unsupported')
    check_identity(pid, start, executable)
    if darwin_recorded_state(pid, start) != 'ALIVE':
        raise ProcessEvidenceError('process_already_exited')
    check_identity(pid, start, executable)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        raise ProcessEvidenceError('signal_failed') from None


def process_gone(pid: int, start: dict, executable: str) -> bool:
    """ESRCH or an exact stable Darwin zombie establishes exit, not PID reuse."""
    # Windows os.kill(pid, 0) is not a POSIX, side-effect-free probe.
    # Reject either Windows platform indicator before any process operation.
    if sys.platform == 'win32' or os.name == 'nt':
        raise ProcessEvidenceError('process_liveness_unsupported')
    if sys.platform == 'darwin':
        _validate_darwin_record(pid, start)
        if not isinstance(executable, str) or not os.path.isabs(executable):
            raise ProcessEvidenceError('invalid_process_identity')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        raise ProcessEvidenceError('process_liveness_unavailable') from None
    if sys.platform == 'darwin':
        try:
            state = darwin_recorded_state(pid, start)
        except ProcessEvidenceError as exc:
            if str(exc) == 'process_state_unavailable':
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                except OSError:
                    raise ProcessEvidenceError('process_liveness_unavailable') from None
            raise
        if state == 'ZOMBIE':
            return True
    try:
        check_identity(pid, start, executable)
    except ProcessEvidenceError as exc:
        if str(exc) != 'process_identity_unavailable':
            raise
        # A dying process may have no queryable image; this alone is not proof
        # of exit. On Darwin, re-observe a possible live-to-zombie transition.
        if sys.platform == 'darwin':
            try:
                if darwin_recorded_state(pid, start) == 'ZOMBIE':
                    return True
            except ProcessEvidenceError as state_error:
                if str(state_error) != 'process_state_unavailable':
                    raise
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            raise ProcessEvidenceError('process_liveness_unavailable') from None
    return False


# struct socket_fdinfo and fields from the 64-bit macOS SDK sys/proc_info.h.
# Require precisely this ABI size, rather than guessing at another OS layout.
_SOCKET_FDINFO_SIZE = 792
_KIND, _PROTO = 256, 264
_STATE, _FPORT, _LPORT, _VFLAG, _FADDR, _LADDR = 344, 264, 268, 288, 296, 312


def _socket_tuple(sock: socket.socket):
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or sock.type & 0xf != socket.SOCK_STREAM:
        raise ProcessEvidenceError('socket_family_unsupported')
    try:
        local, peer = sock.getsockname(), sock.getpeername()
        local_ip, peer_ip = ipaddress.ip_address(local[0]), ipaddress.ip_address(peer[0])
        if not is_local_numeric_address(local[0]) or not is_local_numeric_address(peer[0]):
            raise ProcessEvidenceError('non_local_socket')
        if sock.family == socket.AF_INET6 and (local[3] or peer[3]):
            raise ProcessEvidenceError('scoped_ipv6_unsupported')
        return (sock.family, local_ip.packed, local[1], peer_ip.packed, peer[1])
    except (OSError, ValueError, IndexError, TypeError):
        raise ProcessEvidenceError('socket_endpoint_unavailable') from None


def _socket_open_without_data(sock: socket.socket) -> None:
    try:
        if select.select([sock], [], [], 0)[0]:
            # A protocol peer must not send data before our first request.
            # MSG_PEEK prevents consuming an untrusted response.
            sock.recv(1, socket.MSG_PEEK)
            raise ProcessEvidenceError('socket_closed_or_unsolicited_data')
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            raise ProcessEvidenceError('socket_error')
    except OSError:
        raise ProcessEvidenceError('socket_unavailable') from None


def _libproc():
    if sys.platform != 'darwin' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ProcessEvidenceError('connected_peer_unsupported')
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidfdinfo.restype = ctypes.c_int
        return lib
    except (OSError, AttributeError):
        raise ProcessEvidenceError('libproc_unavailable') from None


def _darwin_has_reverse_connection(pid: int, target) -> bool:
    """Check accepted TCP FD in target PID, not listening/client socket."""
    lib = _libproc()
    size = lib.proc_pidinfo(pid, 1, 0, None, 0)  # PROC_PIDLISTFDS
    if size <= 0 or size > 8 * 65536:
        raise ProcessEvidenceError('fd_list_unavailable')
    # Leave room for FDs created between the two calls; full buffer is unknown.
    capacity = size + 8 * 128
    buf = ctypes.create_string_buffer(capacity)
    count = lib.proc_pidinfo(pid, 1, 0, buf, capacity)
    if count <= 0 or count >= capacity or count % 8:
        raise ProcessEvidenceError('fd_list_unavailable')
    family, client_ip, client_port, server_ip, server_port = target
    found = 0
    for fd, kind in struct.iter_unpack('=iI', buf.raw[:count]):
        if kind != 2:  # PROX_FDTYPE_SOCKET
            continue
        info = ctypes.create_string_buffer(_SOCKET_FDINFO_SIZE)
        got = lib.proc_pidfdinfo(pid, fd, 3, info, len(info))  # PROC_PIDFDSOCKETINFO
        if got != _SOCKET_FDINFO_SIZE:
            raise ProcessEvidenceError('socket_fd_unavailable')
        data = info.raw
        if struct.unpack_from('=i', data, _KIND)[0] != 2:  # SOCKINFO_TCP
            continue
        flag = data[_VFLAG]
        if family == socket.AF_INET:
            if not flag & 1:
                continue
            foreign = data[_FADDR + 12:_FADDR + 16]
            local = data[_LADDR + 12:_LADDR + 16]
        else:
            if not flag & 2:
                continue
            foreign = data[_FADDR:_FADDR + 16]
            local = data[_LADDR:_LADDR + 16]
        fport = socket.ntohs(struct.unpack_from('=I', data, _FPORT)[0] & 0xffff)
        lport = socket.ntohs(struct.unpack_from('=I', data, _LPORT)[0] & 0xffff)
        state = struct.unpack_from('=i', data, _STATE)[0]
        if (state == 4 and foreign == client_ip and fport == client_port
                and local == server_ip and lport == server_port):
            found += 1
    if found > 1:
        raise ProcessEvidenceError('ambiguous_socket_owner')
    return found == 1


_TSI_S_LISTEN = 1  # sys/proc_info.h TSI_S_LISTEN for struct tcp_sockinfo.


def _darwin_listening_tcp_sockets(pid: int):
    """Enumerate this exact PID's LISTEN TCP sockets as (family, local, port).

    Only read-only FD metadata is used. Any unreadable FD or unexpected layout
    is UNKNOWN; this cannot prove exclusive ownership or exclude races.
    """
    lib = _libproc()
    size = lib.proc_pidinfo(pid, 1, 0, None, 0)  # PROC_PIDLISTFDS
    if size <= 0 or size > 8 * 65536:
        raise ProcessEvidenceError('fd_list_unavailable')
    capacity = size + 8 * 128
    buf = ctypes.create_string_buffer(capacity)
    count = lib.proc_pidinfo(pid, 1, 0, buf, capacity)
    if count <= 0 or count >= capacity or count % 8:
        raise ProcessEvidenceError('fd_list_unavailable')
    found = []
    for fd, kind in struct.iter_unpack('=iI', buf.raw[:count]):
        if kind != 2:  # PROX_FDTYPE_SOCKET
            continue
        info = ctypes.create_string_buffer(_SOCKET_FDINFO_SIZE)
        got = lib.proc_pidfdinfo(pid, fd, 3, info, len(info))  # PROC_PIDFDSOCKETINFO
        if got != _SOCKET_FDINFO_SIZE:
            raise ProcessEvidenceError('socket_fd_unavailable')
        data = info.raw
        if struct.unpack_from('=i', data, _KIND)[0] != 2:  # SOCKINFO_TCP
            continue
        if struct.unpack_from('=i', data, _STATE)[0] != _TSI_S_LISTEN:
            continue
        flag = data[_VFLAG]
        if flag & 1:  # INI_IPV4
            family, local = socket.AF_INET, data[_LADDR + 12:_LADDR + 16]
        elif flag & 2:  # INI_IPV6
            family, local = socket.AF_INET6, data[_LADDR:_LADDR + 16]
        else:
            raise ProcessEvidenceError('socket_fd_unavailable')
        port = socket.ntohs(struct.unpack_from('=I', data, _LPORT)[0] & 0xffff)
        found.append((family, local, port))
    return found


def verify_listening_owner(pid: int, start: dict, executable: str, family: int,
                           address: str, port: int) -> bool:
    """Whether this exact PID has a listening TCP socket covering the endpoint.

    ``address`` is the expected local address; an unspecified listener covers
    any address of its own family. A cross-family wildcard is ambiguous (the
    kernel may report only one side of a dual-stack socket, and V6ONLY is not
    observable), so it raises instead of claiming or denying ownership.
    ``False`` means the exact PID was enumerated and no matching listener
    exists. An unreadable FD table, changed process identity or unsupported
    platform raises; callers must not treat that as absence. Same-user
    impersonation and observation races remain.
    """
    if sys.platform != 'darwin':
        raise ProcessEvidenceError('listening_owner_unsupported')
    if (family not in (socket.AF_INET, socket.AF_INET6)
            or type(port) is not int or not 1 <= port <= 65535):
        raise ProcessEvidenceError('socket_family_unsupported')
    try:
        expected = ipaddress.ip_address(address)
    except (ValueError, TypeError):
        raise ProcessEvidenceError('local_address_unavailable') from None
    check_identity(pid, start, executable)
    sockets = _darwin_listening_tcp_sockets(pid)
    check_identity(pid, start, executable)
    for socket_family, packed, local_port in sockets:
        if local_port != port:
            continue
        try:
            local = ipaddress.ip_address(packed)
        except ValueError:
            raise ProcessEvidenceError('socket_fd_unavailable') from None
        if local.is_unspecified:
            if socket_family == family:
                return True
            # Cross-family wildcards are ambiguous: an IPv4 wildcard may be
            # the reported view of an IPv6 dual-stack listener, and an IPv6
            # wildcard may or may not have been opened with V6ONLY. Never
            # claim ownership in either direction.
            raise ProcessEvidenceError('listening_owner_ambiguous')
        if socket_family == family and local == expected:
            return True
    return False


def verify_connected_peer(sock: socket.socket, pid: int, start: dict, executable: str,
                          *, timeout: float = 2.0, interval: float = 0.05) -> None:
    """Verify the reverse tuple in PID while keeping the *same* socket open.

    No application bytes are sent. A not-yet-accepted connection is retried
    only on this socket; identity and connection errors are never retried.
    Finding the reverse socket FD in this PID is not proof of exclusive FD
    ownership: a same-user process may inherit or transfer a descriptor.
    """
    if timeout < 0 or interval <= 0:
        raise ValueError('invalid timeout')
    target = _socket_tuple(sock)
    deadline = time.monotonic() + timeout
    while True:
        check_identity(pid, start, executable)
        _socket_open_without_data(sock)
        found = _darwin_has_reverse_connection(pid, target)
        check_identity(pid, start, executable)
        if _socket_tuple(sock) != target:
            raise ProcessEvidenceError('socket_endpoint_changed')
        _socket_open_without_data(sock)
        if found:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessEvidenceError('connected_peer_not_found')
        time.sleep(min(interval, remaining))
