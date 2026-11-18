"""Legacy observation adapter on a real loopback socket, simulated OS evidence.

The listener is this test process, and receives no application data. The
backend models Windows process/table queries on non-Windows hosts; this test
does not establish native Windows security or launch authority.
"""
import contextlib
import copy
import io
import os
import socket
import sys
import threading
import unittest
from unittest.mock import patch

from core import discovery, windows_process as wp
from core.cli import dispatch, windows_control
from tests.test_windows_control import ROOT, records
from tests.test_windows_process import FakeBackend, row4, table


class ConnectedStatusTests(unittest.TestCase):
    def run_status(self, *, change_record=False, wrong_pid=False):
        active, last = records()
        # The process backend below is simulated; no unrelated PID is opened.
        pid = 4321 if os.getpid() != 4321 else 4322
        active["pid"] = pid
        backend = FakeBackend()
        backend.pid = pid
        initial = {"install": {"fixture": True}, "active": active, "last_good_paths": last}
        received, failures = [], []
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)
            port = listener.getsockname()[1]
            active["listen"]["port"] = last["listen"]["port"] = port

            def serve():
                try:
                    with listener.accept()[0] as accepted:
                        accepted.settimeout(3)
                        received.append(accepted.recv(4096))
                except Exception as exc:
                    failures.append(exc)

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            count = 0

            def read_records(_):
                nonlocal count
                count += 1
                result = copy.deepcopy(initial)
                if change_record and count > 1:
                    result["install"]["fixture"] = False
                return result

            verify = wp.verify_socket_owner

            def observe(sock, expected_pid, start, executable, **options):
                local = sock.getsockname()
                peer = sock.getpeername()
                backend.rows = wp.parse_owner_pid_table(
                    socket.AF_INET, table(row4(lport=peer[1], rport=local[1],
                                              pid=pid + 1 if wrong_pid else pid)))
                options["backend"] = backend
                return verify(sock, expected_pid, start, executable, **options)

            control = windows_control.WindowsControl(ROOT)
            with patch.object(wp, "verify_socket_owner", side_effect=observe):
                observed = control._observe_recorded_connection(initial, read_records)
            worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(received, [b""])
        return observed, backend, count

    def test_legacy_observer_checks_real_connection_without_sending_bytes(self):
        observed, backend, count = self.run_status()
        self.assertTrue(observed)
        self.assertEqual(len(backend.opened), 1)
        self.assertEqual(backend.closed, [100])
        self.assertEqual(count, 2)

    def test_changed_installation_record_invalidates_observation(self):
        observed, backend, _ = self.run_status(change_record=True)
        self.assertFalse(observed)
        self.assertEqual(backend.closed, [100])

    def test_wrong_connection_owner_is_not_observed(self):
        observed, backend, _ = self.run_status(wrong_pid=True)
        self.assertFalse(observed)
        self.assertEqual(backend.opened, [])

    def test_non_loopback_record_is_rejected_before_opening_socket(self):
        active, last = records()
        active["listen"]["host"] = last["listen"]["host"] = "192.0.2.1"
        snapshot = {"install": {"fixture": True}, "active": active, "last_good_paths": last}
        control = windows_control.WindowsControl(ROOT)
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")) as create:
            self.assertFalse(control._observe_recorded_connection(snapshot, lambda _: snapshot))
        create.assert_not_called()

    def test_dispatch_passes_config_override_to_windows_control(self):
        with patch.object(sys, "platform", "win32"), \
             patch.dict(os.environ, {"MODEL_PROXY_CONFIG": r"relative\fake.json"}), \
             patch.object(dispatch.ntpath, "abspath", return_value=r"C:\cwd\relative\fake.json") as resolve, \
             patch.object(windows_control, "WindowsControl") as control:
            control.return_value.reload.return_value = 1
            self.assertEqual(dispatch.main(["reload"], root=ROOT), 1)
        resolve.assert_called_once_with(r"relative\fake.json")
        control.assert_called_once_with(ROOT, config_path=r"C:\cwd\relative\fake.json")


if __name__ == "__main__":
    unittest.main()
