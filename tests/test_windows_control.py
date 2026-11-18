"""Windows control gates and peer observations; all sockets/processes are fake."""
import copy
import io
import math
import socket
import unittest
from unittest.mock import Mock, patch

from core import windows_process as wp
from core.cli.windows_control import WindowsControl, WindowsControlError, validate_record_claim
from core.runtime_paths import PATH_KEYS
from tests.test_windows_process import FakeBackend, FakeSocket, row4, row6, table


ROOT = r"C:\fixture\project"
EXE = r"C:\fixture\python.exe"


def records():
    common = {"schema": 2, "project_root": ROOT, "instance_nonce": "a" * 32,
              "install_id": "b" * 32, "listen": {"host": "127.0.0.1", "port": 19431},
              "runtime_paths": {key: r"C:\fixture\runtime" + "\\" + key for key in PATH_KEYS}}
    active = dict(common, record="active", state="READY", pid=4321,
                  process_start={"kind": "windows_filetime", "ticks_100ns": 12345},
                  executable=EXE, locks={"anchor": {"device": 17, "inode": 1},
                                         "legacy": {"device": 17, "inode": 2}})
    last = dict(common, record="last_good_paths")
    return copy.deepcopy(active), copy.deepcopy(last)


class WindowsControlTests(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.authority = Mock()
        self.control = WindowsControl(ROOT, authority=self.authority, out=self.out, err=self.err)
        self.sock = FakeSocket()
        self.backend = FakeBackend()
        self.active, self.last = records()
        for item in (patch.object(wp.socket, "socket", FakeSocket),
                     patch.object(wp, "socket_open_without_data")):
            item.start()
            self.addCleanup(item.stop)

    def test_public_commands_fail_closed_even_with_truthy_authority(self):
        self.authority.verify_active.return_value = True
        with patch.object(wp, "verify_socket_owner", side_effect=AssertionError("probe forbidden")) as probe:
            self.assertEqual([self.control.on(), self.control.off(), self.control.restart(),
                              self.control.status(), self.control.reload()], [1] * 5)
        self.authority.locked.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(self.sock.sent, [])
        self.assertIn("no signal or credential sent", self.err.getvalue())

    def test_record_and_peer_can_be_observed_without_credentials(self):
        observation = self.control.observe_peer(self.sock, self.active, self.last,
                                                backend=self.backend, timeout=0)
        self.assertEqual(observation.claim.pid, 4321)
        self.assertFalse(self.sock.closed)
        self.assertEqual(self.backend.opened, [4321])
        self.assertEqual(self.backend.closed, [])
        observation.revalidate(self.active, self.last)
        observation.close()
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])
        self.assertFalse(self.sock.closed)  # success retains caller ownership
        self.sock.close()

    def test_ipv6_loopback_observation_with_fake_socket(self):
        class FakeIPv6(FakeSocket):
            family = socket.AF_INET6
            def __init__(self):
                super().__init__()
                self.local = ("::1", 50123, 0, 0)
                self.peer = ("::1", 19431, 0, 0)
        sock = FakeIPv6()
        backend = FakeBackend(wp.parse_owner_pid_table(socket.AF_INET6, table(row6())))
        self.active["listen"]["host"] = self.last["listen"]["host"] = "::1"
        with patch.object(wp.socket, "socket", FakeIPv6):
            observation = self.control.observe_peer(sock, self.active, self.last, backend=backend, timeout=0)
            self.assertEqual(observation.claim.endpoint[0], socket.AF_INET6)
            observation.close()
        self.assertEqual(backend.closed, [100])
        self.assertEqual(sock.sent, [])
        sock.close()

    def test_ipv6_scoped_socket_fails_before_process_query(self):
        class FakeIPv6(FakeSocket):
            family = socket.AF_INET6
            def __init__(self):
                super().__init__()
                self.local = ("::1", 50123, 0, 1)
                self.peer = ("::1", 19431, 0, 0)
        sock = FakeIPv6()
        self.active["listen"]["host"] = self.last["listen"]["host"] = "::1"
        with patch.object(wp.socket, "socket", FakeIPv6):
            with self.assertRaises(wp.ProcessEvidenceError):
                self.control.observe_peer(sock, self.active, self.last, backend=self.backend, timeout=0)
        self.assertTrue(sock.closed)
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(sock.sent, [])

    def test_mismatched_records_fail_before_native_queries(self):
        self.last["install_id"] = "c" * 32
        with self.assertRaises(WindowsControlError) as caught:
            self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        self.assertEqual(caught.exception.code, "record_invalid")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.queries, [])
        self.assertEqual(self.sock.sent, [])

    def test_duplicate_or_wrong_owner_closes_socket_without_process_open(self):
        for raw in (table(row4(), row4()), table(row4(pid=9999))):
            self.sock = FakeSocket()
            self.backend = FakeBackend(wp.parse_owner_pid_table(socket.AF_INET, raw))
            with self.subTest(raw=raw), self.assertRaises(wp.ProcessEvidenceError):
                self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
            self.assertTrue(self.sock.closed)
            self.assertEqual(self.backend.opened, [])
            self.assertEqual(self.sock.sent, [])

    def test_record_endpoint_mismatch_closes_socket_and_process_handle(self):
        self.active["listen"]["port"] = self.last["listen"]["port"] = 19432
        with self.assertRaises(WindowsControlError) as caught:
            self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        self.assertEqual(caught.exception.code, "record_endpoint_changed")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])

    def test_closed_observation_cannot_be_reentered_or_revalidated(self):
        observation = self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        observation.close()
        with self.assertRaises(WindowsControlError):
            observation.__enter__()
        with self.assertRaises(WindowsControlError):
            observation.revalidate(self.active, self.last)
        self.assertEqual(self.backend.closed, [100])
        self.assertFalse(self.sock.closed)
        self.sock.close()

    def test_record_change_after_initial_table_refuses_and_closes(self):
        changed = []
        def mutate():
            if not changed:
                changed.append(True)
                self.active["instance_nonce"] = self.last["instance_nonce"] = "c" * 32
        self.backend.on_rows = mutate
        with self.assertRaises(WindowsControlError) as caught:
            self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])

    def test_record_change_during_final_peer_check_is_detected(self):
        calls = []
        def mutate():
            calls.append(1)
            if len(calls) == 3:
                self.active["instance_nonce"] = self.last["instance_nonce"] = "c" * 32
        self.backend.on_rows = mutate
        with self.assertRaises(WindowsControlError) as caught:
            self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])

    def test_revalidation_detects_record_change_after_last_peer_check(self):
        observation = self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        counts = []
        def mutate():
            counts.append(1)
            if len(counts) == 2:
                self.active["instance_nonce"] = self.last["instance_nonce"] = "c" * 32
        self.backend.on_rows = mutate
        with self.assertRaises(WindowsControlError) as caught:
            observation.revalidate(self.active, self.last)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])

    def test_revalidation_detects_record_change_during_peer_check(self):
        observation = self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        def mutate():
            self.active["instance_nonce"] = self.last["instance_nonce"] = "c" * 32
        self.backend.on_rows = mutate
        with self.assertRaises(WindowsControlError) as caught:
            observation.revalidate(self.active, self.last)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])
        self.assertEqual(self.sock.sent, [])

    def test_revalidation_record_drift_invalidates_and_closes_all_evidence(self):
        observation = self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        self.active["instance_nonce"] = self.last["instance_nonce"] = "c" * 32
        with self.assertRaises(WindowsControlError) as caught:
            observation.revalidate(self.active, self.last)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])
        with self.assertRaises(WindowsControlError):
            observation.revalidate(self.active, self.last)
        observation.close()
        self.assertEqual(self.backend.closed, [100])

    def test_release_failure_is_unknown_and_not_retried(self):
        self.active["listen"]["port"] = self.last["listen"]["port"] = 19432
        self.backend.fail_close = True
        with self.assertRaises(WindowsControlError) as caught:
            self.control.observe_peer(self.sock, self.active, self.last, backend=self.backend, timeout=0)
        self.assertEqual(caught.exception.code, "resource_release_unknown")
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.backend.closed, [100])

    def test_input_schema_paths_and_nonloopback_fail_closed(self):
        cases = [
            ("pid", True), ("pid", 0), ("executable", r"C:relative.exe"),
            ("executable", r"C:\fixture\NUL"), ("state", "STARTING"),
        ]
        for key, value in cases:
            active, last = records()
            active[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(WindowsControlError):
                validate_record_claim(ROOT, active, last)
        active, last = records()
        active["listen"]["host"] = last["listen"]["host"] = "203.0.113.4"
        with self.assertRaises(WindowsControlError): validate_record_claim(ROOT, active, last)
        active, last = records()
        active["runtime_paths"]["log"] = last["runtime_paths"]["log"] = r"C:\fixture\file:stream"
        with self.assertRaises(WindowsControlError): validate_record_claim(ROOT, active, last)

    def test_duplicate_role_and_lock_ids_are_not_valid_claims(self):
        active, last = records()
        active["runtime_paths"]["log"] = active["runtime_paths"]["totals"]
        last["runtime_paths"] = copy.deepcopy(active["runtime_paths"])
        with self.assertRaises(WindowsControlError): validate_record_claim(ROOT, active, last)
        active, last = records()
        active["locks"]["legacy"] = dict(active["locks"]["anchor"])
        with self.assertRaises(WindowsControlError): validate_record_claim(ROOT, active, last)

    def test_constructor_rejects_noncanonical_or_nonfinite_configuration(self):
        for path in ("relative", r"C:\fixture\..\project", r"\\server\share\project"):
            with self.subTest(path=path), self.assertRaises(WindowsControlError):
                WindowsControl(path, out=self.out, err=self.err)
        for timeout in (0, -1, math.inf, math.nan):
            with self.subTest(timeout=timeout), self.assertRaises(WindowsControlError):
                WindowsControl(ROOT, request_timeout=timeout, out=self.out, err=self.err)


if __name__ == "__main__":
    unittest.main()
