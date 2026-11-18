"""Offline fake TCP tables, handles, and sockets; no OS processes or network."""
import ctypes
import ipaddress
import math
import socket
import struct
import unittest
from unittest.mock import Mock, patch

from core import windows_process as wp


def port(value): return struct.pack("!H", value) + b"\0\0"
def v4(address): return ipaddress.IPv4Address(address).packed
def v6(address): return ipaddress.IPv6Address(address).packed


def row4(local="127.0.0.1", lport=19431, remote="127.0.0.1", rport=50123, *, state=5, pid=4321):
    return struct.pack("<I", state) + v4(local) + port(lport) + v4(remote) + port(rport) + struct.pack("<I", pid)


def row6(local="::1", lport=19431, remote="::1", rport=50123, *, state=5, pid=4321, lscope=0, rscope=0):
    return (v6(local) + struct.pack("!I", lscope) + port(lport) + v6(remote) +
            struct.pack("!I", rscope) + port(rport) + struct.pack("<II", state, pid))


def table(*rows): return struct.pack("<I", len(rows)) + b"".join(rows)


class FakeSocket:
    family = socket.AF_INET
    type = socket.SOCK_STREAM
    def __init__(self):
        self.fd = 91
        self.local = ("127.0.0.1", 50123)
        self.peer = ("127.0.0.1", 19431)
        self.sent = []
        self.closed = False
    def close(self): self.closed = True; self.fd = -1
    def fileno(self): return self.fd
    def getsockname(self): return self.local
    def getpeername(self): return self.peer
    def getsockopt(self, level, name): return 0
    def sendall(self, data):
        self.sent.append(data)
        raise AssertionError("process evidence must never send")


class FakeBackend:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else wp.parse_owner_pid_table(socket.AF_INET, table(row4()))
        self.row_sequence = []
        self.opened = []
        self.closed = []
        self.queries = []
        self.pid = 4321
        self.ticks = 12345
        self.exe = r"C:\fixture\python.exe"
        self.alive = True
        self.fail_open = False
        self.fail_close = False
        self.on_rows = None
    def tcp_rows(self, family):
        self.queries.append(("table", family))
        if self.on_rows: self.on_rows()
        return self.row_sequence.pop(0) if self.row_sequence else self.rows
    def open_process(self, pid):
        self.opened.append(pid)
        if self.fail_open: raise OSError("fake sensitive detail")
        return 100
    def process_id(self, handle): self.queries.append(("pid", handle)); return self.pid
    def creation_ticks(self, handle): self.queries.append(("ticks", handle)); return self.ticks
    def image(self, handle): self.queries.append(("image", handle)); return self.exe
    def assert_alive(self, handle):
        self.queries.append(("alive", handle))
        if not self.alive: raise wp.ProcessEvidenceError("process_exited")
    def close(self, handle):
        self.closed.append(handle)
        if self.fail_close: raise OSError("fake close")


class RawTableTests(unittest.TestCase):
    def test_ipv4_network_ports_and_little_endian_pid(self):
        rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(lport=0x1234, rport=0x5678, pid=0x1020304)))
        self.assertEqual((rows[0].local_ip, rows[0].local_port, rows[0].remote_port, rows[0].pid),
                         (v4("127.0.0.1"), 0x1234, 0x5678, 0x1020304))

    def test_ipv6_scope_network_order_and_unrelated_scoped_entry(self):
        raw = table(row6(local="fe80::1", remote="fe80::2", lscope=3, rscope=4), row6())
        rows = wp.parse_owner_pid_table(socket.AF_INET6, raw)
        self.assertEqual((rows[0].local_scope, rows[0].remote_scope), (3, 4))
        endpoint = wp.ConnectionTuple(socket.AF_INET6, v6("::1"), 50123, v6("::1"), 19431)
        self.assertEqual(wp.find_connection_owner(endpoint, rows), 4321)
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            wp.find_connection_owner(endpoint, wp.parse_owner_pid_table(socket.AF_INET6, table(row6(lscope=1))))
        self.assertEqual(caught.exception.code, "socket_scope_unsupported")

    def test_empty_table_and_trailing_capacity_are_accepted(self):
        self.assertEqual(wp.parse_owner_pid_table(socket.AF_INET, b"\0" * 4), ())
        self.assertEqual(len(wp.parse_owner_pid_table(socket.AF_INET, table(row4()) + b"\0" * 32)), 1)

    def test_non_established_zero_ports_are_not_misread_as_connections(self):
        raw = table(row4(lport=0, rport=0, state=2, pid=0), row4())
        rows = wp.parse_owner_pid_table(socket.AF_INET, raw)
        endpoint = wp.ConnectionTuple(socket.AF_INET, v4("127.0.0.1"), 50123, v4("127.0.0.1"), 19431)
        self.assertEqual(wp.find_connection_owner(endpoint, rows), 4321)

    def test_truncated_or_invalid_established_row_fails_closed(self):
        for raw in (b"", b"\x01\0\0\0", table(row4())[:-1], table(row4(lport=0)),
                    table(row4(pid=0)), table(row4(state=99))):
            with self.subTest(raw=raw), self.assertRaises(wp.ProcessEvidenceError):
                wp.parse_owner_pid_table(socket.AF_INET, raw)

    def test_reverse_tuple_with_established_and_nonestablished_row_is_ambiguous(self):
        endpoint = wp.ConnectionTuple(socket.AF_INET, v4("127.0.0.1"), 50123, v4("127.0.0.1"), 19431)
        rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(), row4(state=11, pid=0)))
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            wp.find_connection_owner(endpoint, rows)
        self.assertEqual(caught.exception.code, "connection_owner_ambiguous")

    def test_duplicate_reverse_rows_are_ambiguous_even_same_pid(self):
        endpoint = wp.ConnectionTuple(socket.AF_INET, v4("127.0.0.1"), 50123, v4("127.0.0.1"), 19431)
        rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(), row4()))
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            wp.find_connection_owner(endpoint, rows)
        self.assertEqual(caught.exception.code, "connection_owner_ambiguous")


class SocketProbeTests(unittest.TestCase):
    def test_readable_or_error_socket_fails_without_receiving_or_sending(self):
        sock = FakeSocket()
        with patch.object(wp.select, "select", return_value=([], [], [])):
            wp.socket_open_without_data(sock)
        with patch.object(wp.select, "select", return_value=([sock], [], [])):
            with self.assertRaises(wp.ProcessEvidenceError) as caught:
                wp.socket_open_without_data(sock)
        self.assertEqual(caught.exception.code, "socket_closed_or_unsolicited_data")
        sock.getsockopt = lambda *args: 5
        with patch.object(wp.select, "select", return_value=([], [], [])):
            with self.assertRaises(wp.ProcessEvidenceError) as caught:
                wp.socket_open_without_data(sock)
        self.assertEqual(caught.exception.code, "socket_error")
        self.assertEqual(sock.sent, [])


class FakeIphlp:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.grow_once = False
        self.fail = None
    def GetExtendedTcpTable(self, buffer, size, ordered, family, table_class, reserved):
        size_ptr = ctypes.cast(size, ctypes.POINTER(wp.DWORD))
        self.calls.append((buffer is None, family, table_class, reserved, size_ptr[0]))
        if self.fail is not None: return self.fail
        if buffer is None:
            size_ptr[0] = 4 if self.grow_once else len(self.raw)
            return wp.ERROR_INSUFFICIENT_BUFFER
        if size_ptr[0] < len(self.raw):
            size_ptr[0] = len(self.raw)
            return wp.ERROR_INSUFFICIENT_BUFFER
        ctypes.memmove(buffer, self.raw, len(self.raw))
        size_ptr[0] = len(self.raw)
        return 0


class FakeKernel:
    def __init__(self):
        self.calls = []
        self.wait = wp.WAIT_TIMEOUT
        self.pid = 4321
        self.ticks = 12345
        self.image = r"C:\fixture\python.exe"
        self.close_ok = True
    def OpenProcess(self, rights, inherit, pid):
        self.calls.append(("open", rights, inherit, pid)); return 100
    def GetProcessId(self, handle): self.calls.append(("pid", handle)); return self.pid
    def GetProcessTimes(self, handle, created, exited, kernel, user):
        self.calls.append(("times", handle))
        item = ctypes.cast(created, ctypes.POINTER(wp._FILETIME)).contents
        item.low, item.high = self.ticks & 0xFFFFFFFF, self.ticks >> 32
        return 1
    def QueryFullProcessImageNameW(self, handle, flags, output, size):
        self.calls.append(("image", handle)); output.value = self.image
        ctypes.cast(size, ctypes.POINTER(wp.DWORD))[0] = len(self.image)
        return 1
    def WaitForSingleObject(self, handle, timeout): self.calls.append(("wait", handle, timeout)); return self.wait
    def CloseHandle(self, handle): self.calls.append(("close", handle)); return self.close_ok


class NativeBindingContractTests(unittest.TestCase):
    def test_table_size_probe_growth_and_windows_ipv6_family(self):
        fake = FakeIphlp(table(row6()))
        fake.grow_once = True
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        backend._iphlp = fake
        rows = backend.tcp_rows(socket.AF_INET6)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(call[1:4] == (23, 4, 0) for call in fake.calls))

    def test_native_empty_tcp_table_is_not_an_api_failure(self):
        fake = FakeIphlp(table())
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        backend._iphlp = fake
        self.assertEqual(backend.tcp_rows(socket.AF_INET), ())

    def test_native_dlls_are_lazy_and_have_expected_signatures(self):
        iphlp, kernel = Mock(), Mock()
        with patch.object(wp.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", side_effect=[iphlp, kernel], create=True) as load:
            wp.NativeBackend()
        self.assertEqual([call.args[0] for call in load.call_args_list], ["iphlpapi", "kernel32"])
        self.assertEqual(len(iphlp.GetExtendedTcpTable.argtypes), 6)
        self.assertEqual(len(kernel.GetProcessTimes.argtypes), 5)
        self.assertEqual(len(kernel.QueryFullProcessImageNameW.argtypes), 4)
        self.assertEqual(len(kernel.WaitForSingleObject.argtypes), 2)
        self.assertNotIn("TerminateProcess", str(iphlp.method_calls) + str(kernel.method_calls))

    def test_table_growth_is_bounded(self):
        calls = []
        def grow(buffer, size, ordered, family, cls, reserved):
            pointer = ctypes.cast(size, ctypes.POINTER(wp.DWORD))
            pointer[0] = max(4, pointer[0] + 4)
            calls.append(1)
            return wp.ERROR_INSUFFICIENT_BUFFER
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        backend._iphlp = type("Fake", (), {"GetExtendedTcpTable": staticmethod(grow)})()
        with self.assertRaises(wp.ProcessEvidenceError): backend.tcp_rows(socket.AF_INET)
        self.assertEqual(len(calls), 4)

    def test_table_api_errors_and_oversized_probe_fail_closed(self):
        fake = FakeIphlp(table(row4()))
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        backend._iphlp = fake
        fake.fail = 5
        with self.assertRaises(wp.ProcessEvidenceError): backend.tcp_rows(socket.AF_INET)
        def huge(buffer, size, ordered, family, cls, reserved):
            ctypes.cast(size, ctypes.POINTER(wp.DWORD))[0] = wp.MAX_TABLE_BYTES + 1
            return wp.ERROR_INSUFFICIENT_BUFFER
        fake.GetExtendedTcpTable = huge
        with self.assertRaises(wp.ProcessEvidenceError): backend.tcp_rows(socket.AF_INET)

    def test_native_load_on_nonwindows_is_refused_without_loading_dll(self):
        with patch.object(wp.os, "name", "posix"), \
             patch.object(ctypes, "WinDLL", create=True) as loader:
            with self.assertRaises(wp.ProcessEvidenceError) as caught:
                wp.NativeBackend()
        self.assertEqual(caught.exception.code, "platform_unavailable")
        loader.assert_not_called()

    def test_native_time_and_image_api_failures_are_sanitized(self):
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        kernel = FakeKernel()
        backend._kernel = kernel
        kernel.GetProcessTimes = lambda *args: 0
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            backend.creation_ticks(100)
        self.assertEqual(caught.exception.code, "process_times_unavailable")
        kernel.QueryFullProcessImageNameW = lambda *args: 0
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            backend.image(100)
        self.assertEqual(caught.exception.code, "process_image_unavailable")

    def test_native_process_wait_and_query_failures_are_closed_unknown(self):
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        kernel = FakeKernel()
        backend._kernel = kernel
        kernel.wait = wp.WAIT_OBJECT_0
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            backend.assert_alive(100)
        self.assertEqual(caught.exception.code, "process_exited")
        kernel.wait = 0xFFFFFFFF
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            backend.assert_alive(100)
        self.assertEqual(caught.exception.code, "process_liveness_unavailable")
        kernel.pid = 0
        with self.assertRaises(wp.ProcessEvidenceError): backend.process_id(100)
        kernel.ticks = 0
        with self.assertRaises(wp.ProcessEvidenceError): backend.creation_ticks(100)
        kernel.image = ""
        with self.assertRaises(wp.ProcessEvidenceError): backend.image(100)

    def test_same_handle_times_image_pid_and_zero_wait(self):
        backend = wp.NativeBackend.__new__(wp.NativeBackend)
        kernel = FakeKernel()
        backend._kernel = kernel
        handle = backend.open_process(4321)
        process = wp._ProcessHandle.__new__(wp._ProcessHandle)
        process._backend, process._handle = backend, handle
        process._release_unknown = False
        import threading
        process._lock = threading.RLock()
        expected = wp.ProcessIdentity(4321, 12345, r"C:\fixture\python.exe")
        process.read(expected)
        process.close()
        self.assertEqual(kernel.calls[0], ("open", wp.PROCESS_QUERY_LIMITED_INFORMATION | wp.SYNCHRONIZE, False, 4321))
        self.assertEqual([x[1] for x in kernel.calls if x[0] in ("pid", "times", "image", "wait")], [100] * 6)
        self.assertEqual(kernel.calls[-1], ("close", 100))


class StandaloneProcessTests(unittest.TestCase):
    def test_same_handle_is_retained_across_observations_and_closed_once(self):
        backend = FakeBackend()
        proof = wp.open_verified_process(4321, {"kind": "windows_filetime", "ticks_100ns": 12345},
                                         backend.exe, backend=backend)
        proof.revalidate()
        self.assertEqual(backend.opened, [4321])
        self.assertEqual(backend.closed, [])
        self.assertTrue(all(handle == 100 for name, handle in backend.queries))
        proof.close()
        proof.close()
        self.assertEqual(backend.closed, [100])

    def test_identity_change_invalidates_without_signals_or_retry(self):
        backend = FakeBackend()
        proof = wp.open_verified_process(4321, {"kind": "windows_filetime", "ticks_100ns": 12345},
                                         backend.exe, backend=backend)
        backend.ticks = 999
        with self.assertRaises(wp.ProcessEvidenceError): proof.revalidate()
        backend.ticks = 12345
        with self.assertRaises(wp.ProcessEvidenceError) as caught: proof.revalidate()
        self.assertEqual(caught.exception.code, "process_evidence_invalid")
        proof.close()
        self.assertEqual(backend.closed, [100])

    def test_false_or_malformed_liveness_evidence_is_not_accepted(self):
        backend = FakeBackend()
        backend.assert_alive = lambda handle: False
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            wp.open_verified_process(4321, {"kind": "windows_filetime", "ticks_100ns": 12345},
                                     backend.exe, backend=backend)
        self.assertEqual(caught.exception.code, "process_liveness_unavailable")
        self.assertEqual(backend.closed, [100])

    def test_boolean_pid_and_timestamps_are_not_integers_for_evidence(self):
        for attr in ("pid", "ticks"):
            backend = FakeBackend()
            setattr(backend, attr, True)
            with self.subTest(attr=attr), self.assertRaises(wp.ProcessEvidenceError):
                wp.open_verified_process(4321, {"kind": "windows_filetime", "ticks_100ns": 12345},
                                         backend.exe, backend=backend)
            self.assertEqual(backend.closed, [100])

    def test_open_permission_failure_never_queries_or_closes_fake_handle(self):
        backend = FakeBackend()
        backend.fail_open = True
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            wp.open_verified_process(4321, {"kind": "windows_filetime", "ticks_100ns": 12345},
                                     backend.exe, backend=backend)
        self.assertEqual(caught.exception.code, "process_open_denied")
        self.assertEqual(backend.queries, [])
        self.assertEqual(backend.closed, [])


class PeerLeaseTests(unittest.TestCase):
    def setUp(self):
        self.sock = FakeSocket()
        self.backend = FakeBackend()
        self.start = {"kind": "windows_filetime", "ticks_100ns": 12345}
        self.patches = [patch.object(wp.socket, "socket", FakeSocket),
                        patch.object(wp, "socket_open_without_data")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def acquire(self, **kwargs):
        return wp.verify_socket_owner(self.sock, 4321, self.start, self.backend.exe,
                                      backend=self.backend, timeout=0, **kwargs)

    def test_matching_reverse_tuple_holds_same_process_handle_until_close(self):
        lease = self.acquire()
        lease.revalidate()
        self.assertEqual(self.backend.opened, [4321])
        self.assertEqual(self.backend.closed, [])
        self.assertTrue(all(handle == 100 for name, handle in self.backend.queries if name != "table"))
        lease.close()
        lease.close()
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])

    def test_pid_mismatch_and_duplicate_rows_never_open_process(self):
        for rows in (wp.parse_owner_pid_table(socket.AF_INET, table(row4(pid=9999))),
                     wp.parse_owner_pid_table(socket.AF_INET, table(row4(), row4()))):
            self.backend.rows = rows
            with self.assertRaises(wp.ProcessEvidenceError): self.acquire()
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(self.sock.sent, [])

    def test_process_creation_image_pid_and_liveness_mismatch_close_handle(self):
        for attr, value in (("pid", 9999), ("ticks", 999), ("exe", r"C:\other.exe"), ("alive", False)):
            backend = FakeBackend()
            setattr(backend, attr, value)
            with self.subTest(attr=attr), self.assertRaises(wp.ProcessEvidenceError):
                wp.verify_socket_owner(self.sock, 4321, self.start, r"C:\fixture\python.exe", backend=backend, timeout=0)
            self.assertEqual(backend.closed, [100])
            self.assertEqual(self.sock.sent, [])

    def test_fd_or_tuple_change_and_row_change_refused(self):
        lease = self.acquire()
        self.sock.fd = 92
        with self.assertRaises(wp.ProcessEvidenceError): lease.revalidate()
        lease.close()
        self.sock.fd = 91
        lease = self.acquire()
        self.sock.peer = ("127.0.0.1", 19432)
        with self.assertRaises(wp.ProcessEvidenceError): lease.revalidate()
        lease.close()
        self.sock.peer = ("127.0.0.1", 19431)
        lease = self.acquire()
        self.backend.rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(pid=9999)))
        with self.assertRaises(wp.ProcessEvidenceError): lease.revalidate()
        lease.close()
        self.assertEqual(self.sock.sent, [])

    def test_failed_revalidation_cannot_be_reused_after_owner_returns(self):
        lease = self.acquire()
        original = self.backend.rows
        self.backend.rows = wp.parse_owner_pid_table(socket.AF_INET, table(row4(pid=9999)))
        with self.assertRaises(wp.ProcessEvidenceError): lease.revalidate()
        self.backend.rows = original
        with self.assertRaises(wp.ProcessEvidenceError) as caught: lease.revalidate()
        self.assertEqual(caught.exception.code, "peer_lease_invalid")
        lease.close()
        self.assertEqual(self.backend.closed, [100])

    def test_process_creation_change_within_same_handle_is_rejected(self):
        times = iter((12345, 12346))
        self.backend.creation_ticks = lambda handle: next(times)
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "process_identity_changed")
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])

    def test_closed_or_unsolicited_socket_never_opens_process(self):
        with patch.object(wp, "socket_open_without_data", side_effect=wp.ProcessEvidenceError("socket_unavailable")):
            with self.assertRaises(wp.ProcessEvidenceError): self.acquire()
        self.assertEqual(self.backend.opened, [])

    def test_pending_accept_retries_same_socket_only(self):
        self.backend.row_sequence = [(), self.backend.rows, self.backend.rows]
        ticks = [0.0]
        def clock(): return ticks[0]
        def sleep(value): ticks[0] += value
        lease = wp.verify_socket_owner(self.sock, 4321, self.start, self.backend.exe, backend=self.backend,
                                       timeout=0.2, interval=0.05, clock=clock, sleep=sleep)
        self.assertEqual(ticks[0], 0.05)
        lease.close()
        self.assertEqual(self.sock.sent, [])

    def test_unavailable_or_close_failure_is_sanitized(self):
        self.backend.fail_open = True
        with self.assertRaises(wp.ProcessEvidenceError) as caught: self.acquire()
        self.assertNotIn("sensitive", str(caught.exception))
        self.backend.fail_open = False
        lease = self.acquire()
        self.backend.fail_close = True
        with self.assertRaises(wp.ProcessEvidenceError) as caught: lease.close()
        self.assertEqual(caught.exception.code, "process_release_unknown")
        with self.assertRaises(wp.ProcessEvidenceError) as again:
            lease.close()
        self.assertEqual(again.exception.code, "process_release_unknown")
        self.assertEqual(self.backend.closed, [100])

    def test_ambiguous_executable_paths_fail_before_process_open(self):
        for value in (r"C:relative.exe", r"C:\fixture\..\python.exe", r"C:\fixture\file:stream", r"C:\fixture\NUL", r"\\server\share\python.exe"):
            with self.subTest(value=value), self.assertRaises(wp.ProcessEvidenceError):
                wp.verify_socket_owner(self.sock, 4321, self.start, value, backend=self.backend, timeout=0)
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(self.sock.sent, [])

    def test_symmetric_forward_reverse_tuple_is_never_accepted(self):
        self.sock.peer = self.sock.local
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            self.acquire()
        self.assertEqual(caught.exception.code, "socket_tuple_ambiguous")
        self.assertEqual(self.backend.queries, [])
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(self.sock.sent, [])

    def test_nonfinite_timing_and_nonloopback_rejected(self):
        for bad in (math.nan, math.inf, -1, 31):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                wp.verify_socket_owner(self.sock, 4321, self.start, self.backend.exe, backend=self.backend, timeout=bad)
        self.sock.peer = ("203.0.113.1", 19431)
        with self.assertRaises(wp.ProcessEvidenceError): self.acquire()
        self.assertEqual(self.sock.sent, [])


if __name__ == "__main__":
    unittest.main()
