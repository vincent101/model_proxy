"""Fake-handle tests for the approved Windows stop primitive."""
import unittest

from core import windows_process as wp
from tests.test_windows_process import FakeBackend


class StopBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.stops = []

    def terminate_and_wait(self, handle, timeout):
        self.stops.append((handle, timeout))


class StopTests(unittest.TestCase):
    def test_one_verified_handle_is_used_for_stop_and_wait(self):
        backend = StopBackend()
        start = {"kind": "windows_filetime", "ticks_100ns": 12345}
        with wp.open_verified_process(4321, start, backend.exe, backend=backend, terminate=True) as proof:
            proof.terminate_checked(lambda identity: identity.pid == 4321, timeout=2)
        self.assertEqual(backend.opened, [4321])
        self.assertEqual(backend.stops, [(100, 2)])
        self.assertEqual(backend.closed, [100])

    def test_bad_entry_or_changed_creation_time_never_stops(self):
        for mode in ("bad_entry", "reused"):
            backend = StopBackend()
            start = {"kind": "windows_filetime", "ticks_100ns": 12345}
            with wp.open_verified_process(4321, start, backend.exe, backend=backend, terminate=True) as proof:
                def entry(_identity):
                    if mode == "reused":
                        backend.ticks += 1
                        return True
                    return False
                with self.assertRaises(wp.ProcessEvidenceError):
                    proof.terminate_checked(entry, timeout=2)
            self.assertEqual(backend.stops, [])

    def test_pid_reuse_is_not_reported_as_original_process(self):
        backend = StopBackend()
        backend.ticks = 12346
        self.assertEqual(wp.recorded_process_state(
            4321, {"kind": "windows_filetime", "ticks_100ns": 12345},
            backend.exe, backend=backend), "reused")
        self.assertEqual(backend.closed, [100])


if __name__ == "__main__":
    unittest.main()
