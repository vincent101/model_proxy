"""Process probe; only touches Python children spawned here.

Run from an isolated source copy with Windows Python:
    python -I -S -B -c "import sys,unittest; sys.path.insert(0,'.'); unittest.main(module='tests.test_windows_native_process_probe', verbosity=2)"

No server, configuration, credentials, or sockets are involved. A Wine run
exercises Wine's Win32 implementation, not native Windows acceptance.
Children normally exit on stdin EOF; a bounded timeout permits termination
only through the Popen object that created the child.
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, call

from core import windows_process as wp


_CHILD = (
    "import json, os, sys\n"
    "print(json.dumps({'pid': os.getpid(), 'executable': sys.executable}), flush=True)\n"
    "sys.stdin.buffer.read()\n"
)


def _environment_label(os_name=None, loader=None):
    """Detect Wine's ntdll export; absence is not proof of native Windows."""
    if os_name is None:
        os_name = os.name
    if os_name != "nt":
        return "UNVERIFIED (not Windows Python)"
    try:
        if loader is None:
            loader = ctypes.WinDLL
        getattr(loader("ntdll"), "wine_get_version")
    except (AttributeError, OSError, TypeError):
        return "UNVERIFIED (Wine not detected; native Windows not established)"
    return "Wine (ntdll wine_get_version export detected)"


def setUpModule():
    print("Process probe environment: " + _environment_label(), file=sys.stderr, flush=True)


def _finish_owned_child(child, timeout=20):
    """EOF, bounded wait, then at most one Popen-handle termination and wait.

    A fallback or unconfirmed cleanup is a test failure, even if the second
    wait succeeds. Never resolve a PID or use a process-name/port search.
    """
    close_failed = False
    if child.stdin and not child.stdin.closed:
        try:
            child.stdin.close()
        except OSError:
            close_failed = True
    try:
        exit_code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_failed = False
        try:
            child.terminate()
        except OSError:
            terminate_failed = True
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("owned child cleanup unconfirmed after bounded termination") from None
        if terminate_failed:
            raise AssertionError("owned child termination failed; child subsequently reaped") from None
        raise AssertionError("owned child timed out; fallback termination reaped it") from None
    if close_failed:
        raise AssertionError("owned child stdin close failed; child reaped")
    if exit_code != 0:
        raise AssertionError("owned child did not exit normally")


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def _popen_handle_evidence(process):
    """Independent Win32 oracle, using Popen's original child handle."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p(int(process._handle))
    kernel.GetProcessId.argtypes = [ctypes.c_void_p]
    kernel.GetProcessId.restype = ctypes.c_uint32
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FileTime)] * 4
    kernel.GetProcessTimes.restype = ctypes.c_int32
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int32
    pid = kernel.GetProcessId(handle)
    created, exited, kernel_time, user_time = (_FileTime() for _ in range(4))
    if not pid or not kernel.GetProcessTimes(
        handle, ctypes.byref(created), ctypes.byref(exited),
        ctypes.byref(kernel_time), ctypes.byref(user_time)
    ):
        raise RuntimeError("could not query original child handle")
    image = ctypes.create_unicode_buffer(32768)
    size = ctypes.c_uint32(len(image))
    if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
        raise RuntimeError("could not query original child image")
    return pid, (created.high << 32) | created.low, image.value


@unittest.skipUnless(os.name == "nt", "requires Windows Python and Win32 APIs")
class NativeOwnedProcessProbe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mp-process-probe-")
        self.addCleanup(self.temp.cleanup)
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ}
        env.update({"TEMP": self.temp.name, "TMP": self.temp.name})
        self.child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", _CHILD],
            cwd=self.temp.name, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False,
        )
        self.addCleanup(self._stop_child)
        # An unresponsive child cannot hang the test before cleanup.
        result = []
        self.reader = threading.Thread(target=lambda: result.append(self.child.stdout.readline()), daemon=True)
        self.reader.start()
        self.reader.join(20)
        self.assertFalse(self.reader.is_alive(), "child readiness timed out")
        self.assertTrue(result and result[0], "child exited before readiness")
        ready = json.loads(result[0])
        pid, ticks, image = _popen_handle_evidence(self.child)
        self.assertEqual(pid, self.child.pid)
        self.assertEqual(ready["pid"], self.child.pid)
        self.assertGreater(ticks, 0)
        # The process image must be the executable we launched, not merely
        # whatever the backend returned. samefile permits Windows path aliases.
        self.assertTrue(os.path.samefile(image, sys.executable))
        self.assertTrue(os.path.samefile(image, ready["executable"]))
        self.start = {"kind": "windows_filetime", "ticks_100ns": ticks}
        self.image = image
        self.backend = wp.NativeBackend()

    def _stop_child(self):
        try:
            _finish_owned_child(self.child)
        finally:
            reader = getattr(self, "reader", None)
            if self.child.stdout and (reader is None or not reader.is_alive()):
                self.child.stdout.close()

    def _open(self, *, start=None, image=None, backend=None):
        return wp.open_verified_process(
            self.child.pid, self.start if start is None else start,
            self.image if image is None else image,
            backend=self.backend if backend is None else backend,
        )

    def test_native_identity_matches_original_child_handle_and_revalidates(self):
        handle = self.backend.open_process(self.child.pid)
        try:
            self.backend.assert_alive(handle)
            self.assertEqual(self.backend.process_id(handle), self.child.pid)
            self.assertEqual(self.backend.creation_ticks(handle), self.start["ticks_100ns"])
            self.assertEqual(self.backend.image(handle), self.image)
        finally:
            self.backend.close(handle)
        with self._open() as proof:
            self.assertEqual(proof.identity.pid, self.child.pid)
            self.assertEqual(proof.identity.start, self.start)
            self.assertEqual(proof.identity.executable, self.image)
            proof.revalidate()

    def test_wrong_creation_time_is_rejected(self):
        wrong = dict(self.start, ticks_100ns=self.start["ticks_100ns"] + 1)
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            self._open(start=wrong)
        self.assertEqual(caught.exception.code, "process_identity_changed")

    def test_wrong_executable_is_rejected(self):
        wrong = os.path.join(os.path.dirname(self.image), "mp-probe-not-python.exe")
        self.assertNotEqual(wrong, self.image)
        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            self._open(image=wrong)
        self.assertEqual(caught.exception.code, "process_identity_changed")

    def test_reported_pid_mismatch_rejected_without_opening_another_process(self):
        # Fault injection only changes the reported PID. All real OS queries
        # still use a handle opened for this test's child and nothing else.
        native = self.backend
        child_pid = self.child.pid

        class WrongPidBackend:
            def open_process(self, pid):
                if pid != child_pid:
                    raise AssertionError("unexpected PID")
                return native.open_process(pid)

            def process_id(self, handle):
                if native.process_id(handle) != child_pid:
                    raise AssertionError("unexpected handle")
                return child_pid + 1

            def __getattr__(self, name):
                return getattr(native, name)

        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            self._open(backend=WrongPidBackend())
        self.assertEqual(caught.exception.code, "process_identity_changed")

    def test_self_pid_rejected_before_any_process_open(self):
        class NoOpen:
            def open_process(self, pid):
                raise AssertionError("must not open any process")

        with self.assertRaises(wp.ProcessEvidenceError) as caught:
            wp.open_verified_process(os.getpid(), self.start, self.image, backend=NoOpen())
        self.assertEqual(caught.exception.code, "invalid_process_identity")

    def test_naturally_exited_child_invalidates_held_observation(self):
        with self._open() as proof:
            self.child.stdin.close()
            self.assertEqual(self.child.wait(timeout=20), 0)
            with self.assertRaises(wp.ProcessEvidenceError) as caught:
                proof.revalidate()
            self.assertEqual(caught.exception.code, "process_exited")
            with self.assertRaises(wp.ProcessEvidenceError) as again:
                proof.revalidate()
            self.assertEqual(again.exception.code, "process_evidence_invalid")


class CleanupAndEnvironmentTests(unittest.TestCase):
    def test_environment_detection_is_conservative(self):
        wine = Mock()
        wine.wine_get_version = object()
        self.assertIn("Wine", _environment_label("nt", lambda _: wine))
        self.assertIn("UNVERIFIED", _environment_label("nt", lambda _: object()))
        self.assertIn("UNVERIFIED", _environment_label("posix", lambda _: wine))

        def unavailable(_):
            raise OSError("ntdll unavailable")
        self.assertIn("UNVERIFIED", _environment_label("nt", unavailable))

    def test_normal_cleanup_never_terminates(self):
        child = Mock()
        child.stdin.closed = False
        child.wait.return_value = 0
        _finish_owned_child(child, timeout=2)
        child.stdin.close.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=2)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_cleanup_after_timeout_is_bounded_and_uses_only_popen(self):
        child = Mock()
        child.stdin.closed = False
        child.wait.side_effect = [subprocess.TimeoutExpired("owned", 2), 1]
        with self.assertRaisesRegex(AssertionError, "fallback termination reaped"):
            _finish_owned_child(child, timeout=2)
        child.stdin.close.assert_called_once_with()
        child.terminate.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [call(timeout=2), call(timeout=2)])
        child.kill.assert_not_called()

    def test_second_timeout_is_reported_without_retry_or_unbounded_wait(self):
        child = Mock()
        child.stdin.closed = False
        child.wait.side_effect = subprocess.TimeoutExpired("owned", 2)
        with self.assertRaisesRegex(AssertionError, "cleanup unconfirmed"):
            _finish_owned_child(child, timeout=2)
        child.terminate.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [call(timeout=2), call(timeout=2)])
        child.kill.assert_not_called()

    def test_termination_error_still_performs_second_bounded_wait(self):
        child = Mock()
        child.stdin.closed = False
        child.wait.side_effect = [subprocess.TimeoutExpired("owned", 2), 0]
        child.terminate.side_effect = OSError("termination failed")
        with self.assertRaisesRegex(AssertionError, "termination failed; child subsequently reaped"):
            _finish_owned_child(child, timeout=2)
        self.assertEqual(child.wait.call_args_list, [call(timeout=2), call(timeout=2)])


@unittest.skipUnless(os.name == "nt", "requires Windows Python")
class NativeFallbackProbe(unittest.TestCase):
    def test_timeout_fallback_reaps_only_a_new_owned_child(self):
        with tempfile.TemporaryDirectory(prefix="mp-process-fallback-") as temp:
            env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ}
            env.update({"TEMP": temp, "TMP": temp})
            child = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", "-c",
                 "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"],
                cwd=temp, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
            )
            try:
                with self.assertRaisesRegex(AssertionError, "fallback termination reaped"):
                    _finish_owned_child(child, timeout=1)
                self.assertIsNotNone(child.returncode)
            finally:
                if child.returncode is None:
                    _finish_owned_child(child, timeout=2)


if __name__ == "__main__":
    unittest.main()
