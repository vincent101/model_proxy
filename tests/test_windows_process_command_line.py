"""Fake PowerShell/CIM only: no real subprocess, network, or config."""
import base64
import contextlib
import io
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.cli import windows_command_line as cmd

PID = 4321
DIRECTORY = r'C:\Windows\System32'


def payload(text, pid=PID):
    return b'MPW1\n' + str(pid).encode() + b'\n' + base64.b64encode(text.encode('utf-16-le')) + b'\n'


class InPipe:
    def __init__(self): self.writes = []; self.closed = False
    def write(self, data): self.writes.append(data)
    def close(self): self.closed = True


class FakeProcess:
    def __init__(self, output, code=0):
        self.stdin = InPipe()
        self.stdout = io.BytesIO(output)
        self.code = code
        self.wait_calls = 0
        self.stop_calls = []
    def wait(self, timeout=None): self.wait_calls += 1; return self.code
    def terminate(self): self.stop_calls.append('terminate')
    def kill(self): self.stop_calls.append('kill')


class ParserTests(unittest.TestCase):
    def test_exact_unicode_and_backslash_round_trip(self):
        text = r'"C:\程序 文件\python.exe" -I -S "C:\测试 路径\entry.py"'
        self.assertEqual(cmd.parse_observation(payload(text), PID), text)

    def test_malformed_output_and_pid_ambiguities(self):
        valid = payload('abc')
        bad = [b'', valid + b'\n', b'noise\n' + valid, valid.replace(b'4321', b'04321'),
               valid.replace(b'4321', b'4322'), valid.replace(b'MPW1', b'MPW2'),
               b'MPW1\n4321\n@@@=\n', b'MPW1\n4321\nYQ==\n',
               b'MPW1\n4321\n' + base64.b64encode(b'\x00\xd8') + b'\n',
               b'MPW1\n4321\n' + base64.b64encode(b'a\x00\x00\x00') + b'\n',
               b'MPW1\n4321\n\n', b'MPW1\r\n4321\r\nYQBiAA==\r\n',
               b'a' * (cmd._MAX_OUTPUT + 1), None]
        for value in bad:
            with self.subTest(value=repr(value)[:80]), self.assertRaises(cmd.CommandLineObservationError):
                cmd.parse_observation(value, PID)
        for pid in (True, 0, 1, -1, 2**32, '4321'):
            with self.subTest(pid=pid), self.assertRaises(cmd.CommandLineObservationError):
                cmd.parse_observation(valid, pid)


class AdapterTests(unittest.TestCase):
    def read(self, process, *, timeout=1):
        calls = []
        def factory(*args, **kwargs):
            calls.append((args, kwargs)); return process
        reader = cmd.PowerShellWmiReader(timeout=timeout, _process_factory=factory,
                                        _directory_provider=lambda: DIRECTORY)
        return reader, calls

    def wait_gate(self):
        # Failure paths may leave the per-interpreter gate held.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cmd._GATE.acquire(False):
                cmd._GATE.release(); return
            time.sleep(0.002)
        self.fail('fake helper did not reap')

    def tearDown(self): self.wait_gate()

    def test_fixed_pinned_invocation_no_shell_pid_only_on_stdin(self):
        text = r'C:\中文 空格\hello'
        proc = FakeProcess(payload(text))
        reader, calls = self.read(proc)
        self.assertEqual(reader.read_command_line(PID), text)
        self.wait_gate()
        self.assertEqual(proc.stdin.writes, [b'4321\n'])
        args, kw = calls[0]
        self.assertEqual(args[0][0], r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
        self.assertEqual(args[0][1:5], ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand'])
        self.assertNotIn('4321', args[0])
        self.assertEqual(base64.b64decode(args[0][5]).decode('utf-16-le'), cmd._SCRIPT)
        self.assertFalse(kw['shell'])
        self.assertTrue(kw['close_fds'])
        self.assertEqual(kw['stdin'], subprocess.PIPE)
        self.assertEqual(kw['stdout'], subprocess.PIPE)
        self.assertEqual(kw['stderr'], subprocess.DEVNULL)
        self.assertEqual(set(kw['env']), {'SystemRoot', 'PSModulePath'})
        self.assertEqual(kw['env']['SystemRoot'], r'C:\Windows')
        self.assertNotIn('ComputerName', cmd._SCRIPT)
        self.assertIn(r"Microsoft.PowerShell.Core\Import-Module", cmd._SCRIPT)
        self.assertIn(r"\Modules\CimCmdlets\CimCmdlets.psd1", cmd._SCRIPT)
        self.assertIn('-OperationTimeoutSec 2', cmd._SCRIPT)
        self.assertIn("'^[1-9][0-9]{0,9}$'", cmd._SCRIPT)
        self.assertIn('[Console]::OutputEncoding = [Text.ASCIIEncoding]::new()', cmd._SCRIPT)
        self.assertIn('UnicodeEncoding]::new($false, $false, $true)', cmd._SCRIPT)
        self.assertNotIn('TerminateProcess', cmd._SCRIPT)

    def test_immediate_consecutive_successes_do_not_report_busy(self):
        processes = []
        def factory(*args, **kwargs):
            process = FakeProcess(payload('observation'))
            processes.append(process)
            return process
        reader = cmd.PowerShellWmiReader(timeout=1, _process_factory=factory,
                                         _directory_provider=lambda: DIRECTORY)
        for _ in range(20):
            self.assertEqual(reader.read_command_line(PID), 'observation')
        self.assertEqual(len(processes), 20)
        self.assertTrue(all(p.stdin.closed and p.stdout.closed for p in processes))

    def test_success_waits_for_pipe_cleanup_and_gate_release(self):
        entered, release = threading.Event(), threading.Event()
        proc = FakeProcess(b'')
        class SlowClose(io.BytesIO):
            def close(self):
                entered.set()
                release.wait(2)
                super().close()
        proc.stdout = SlowClose(payload('observation'))
        reader, _ = self.read(proc)
        outcomes = []
        def call():
            try:
                outcomes.append(reader.read_command_line(PID))
            except Exception as exc:
                outcomes.append(exc)
        thread = threading.Thread(target=call)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(outcomes, [])
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ['observation'])
        next_reader, _ = self.read(FakeProcess(payload('second')))
        self.assertEqual(next_reader.read_command_line(PID), 'second')

    def test_success_cannot_return_while_gate_release_is_blocked(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        class DelayedGate:
            def __init__(self):
                self.lock = threading.Lock()
            def acquire(self, blocking=True):
                return self.lock.acquire(blocking)
            def release(self):
                entered.set()
                release.wait(2)
                self.lock.release()
        gate = DelayedGate()
        reader, _ = self.read(FakeProcess(payload('first')))
        outcomes = []
        def call():
            try:
                outcomes.append(reader.read_command_line(PID))
            except Exception as exc:
                outcomes.append(exc)
            finally:
                done.set()
        with patch.object(cmd, '_GATE', gate):
            thread = threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(done.wait(.05))
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertEqual(caught.exception.code, 'observer_busy')
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertEqual(outcomes, ['first'])
            second, _ = self.read(FakeProcess(payload('second')))
            self.assertEqual(second.read_command_line(PID), 'second')

    def test_close_failure_never_publishes_success_or_releases_gate(self):
        class BadClose(io.BytesIO):
            fail = True
            def close(self):
                if self.fail:
                    raise OSError('private close detail')
                super().close()
        proc = FakeProcess(b'')
        proc.stdout = BadClose(payload('helper_ownership_unverified'))
        reader, _ = self.read(proc)
        gate = threading.Lock()
        with patch.object(cmd, '_GATE', gate):
            try:
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertEqual(caught.exception.code, 'helper_cleanup_unconfirmed')
                self.assertNotIn('private', str(caught.exception))
                self.assertTrue(gate.locked())
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertEqual(caught.exception.code, 'observer_busy')
            finally:
                proc.stdout.fail = False
                proc.stdout.close()

    def test_timeout_during_final_close_never_returns_stale_success(self):
        entered, release = threading.Event(), threading.Event()
        proc = FakeProcess(b'')
        class SlowClose(io.BytesIO):
            def close(self):
                entered.set()
                release.wait(2)
                super().close()
        proc.stdout = SlowClose(payload('stale-observation'))
        reader, _ = self.read(proc, timeout=.03)
        try:
            with patch.object(cmd, '_CLEANUP_REPLY_WAIT', .08):
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
            self.assertTrue(entered.is_set())
            self.assertEqual(caught.exception.code, 'helper_cleanup_unconfirmed')
            self.assertEqual(proc.stop_calls, [])
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
        finally:
            release.set()
            self.wait_gate()
        next_reader, _ = self.read(FakeProcess(payload('fresh')))
        self.assertEqual(next_reader.read_command_line(PID), 'fresh')

    def test_numeric_pid_only_and_configured_timeout_bounds(self):
        reader, calls = self.read(FakeProcess(payload('x')))
        for pid in (True, False, None, 0, 1, -1, 2**32, '4321', '1;evil'):
            with self.subTest(pid=pid), self.assertRaises(cmd.CommandLineObservationError):
                reader.read_command_line(pid)
        self.assertEqual(calls, [])
        for value in (0, -1, float('inf'), float('nan'), 31, True, '5'):
            with self.subTest(value=value), self.assertRaises(cmd.CommandLineObservationError):
                cmd.PowerShellWmiReader(timeout=value)

    def test_query_error_invalid_output_and_oversize_fail_without_raw_text(self):
        for proc in (FakeProcess(payload('secret'), code=1), FakeProcess(b'secret'),
                     FakeProcess(b'x' * (cmd._MAX_OUTPUT + 2))):
            with self.subTest(proc=proc):
                reader, _ = self.read(proc)
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertNotIn('secret', str(caught.exception))
                self.wait_gate()

    def test_permission_and_platform_error_are_sanitized(self):
        def forbidden(*args, **kwargs): raise OSError('private detail')
        reader = cmd.PowerShellWmiReader(_process_factory=forbidden,
                                         _directory_provider=lambda: DIRECTORY)
        gate = threading.Lock()
        with patch.object(cmd, '_GATE', gate):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'helper_start_unconfirmed')
            self.assertNotIn('private', str(caught.exception))
            self.assertTrue(gate.locked())
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
        with patch.object(cmd.os, 'name', 'posix'):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                cmd.PowerShellWmiReader().read_command_line(PID)
            self.assertEqual(caught.exception.code, 'platform_unavailable')

    def test_system_directory_binding_is_fixed_and_rejects_bad_results(self):
        kernel = Mock()
        fn = kernel.GetSystemDirectoryW
        def fill(buffer, size):
            buffer.value = DIRECTORY
            return len(DIRECTORY)
        fn.side_effect = fill
        with patch.object(cmd.os, 'name', 'nt'), \
             patch.object(cmd.ctypes, 'WinDLL', return_value=kernel, create=True) as loader:
            self.assertEqual(cmd._system_directory(), DIRECTORY)
            loader.assert_called_once_with('kernel32', use_last_error=True)
            self.assertEqual(len(fn.argtypes), 2)
            self.assertIs(fn.restype, cmd.ctypes.c_uint32)
            fn.side_effect = lambda buffer, size: 0
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                cmd._system_directory()
            self.assertEqual(caught.exception.code, 'system_directory_unavailable')
            def invalid(buffer, size):
                buffer.value = r'\\server\share'
                return len(buffer.value)
            fn.side_effect = invalid
            with self.assertRaises(cmd.CommandLineObservationError):
                cmd._system_directory()

    def test_timeout_never_terminates_and_globally_limits_hung_helper(self):
        started, release = threading.Event(), threading.Event()
        class BlockingStream:
            def read(self, limit):
                started.set()
                release.wait(2)
                return payload('eventual')
            def close(self): pass
        proc = FakeProcess(b'')
        proc.stdout = BlockingStream()
        reader, calls = self.read(proc, timeout=0.03)
        try:
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'helper_ownership_unverified')
            self.assertTrue(started.is_set())
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
            other_reader, other_calls = self.read(FakeProcess(payload('not launched')), timeout=0.03)
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                other_reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
            self.assertEqual(other_calls, [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(proc.wait_calls, 0)
            self.assertEqual(proc.stop_calls, [])
        finally:
            release.set()
            self.wait_gate()
        self.assertGreaterEqual(proc.wait_calls, 1)
        self.assertEqual(proc.stop_calls, [])

    def test_unknown_reaping_stays_fail_closed(self):
        class UnknownWait(FakeProcess):
            def wait(self, timeout=None):
                self.wait_calls += 1
                raise OSError('private wait detail')
        proc = UnknownWait(payload('x'))
        reader, calls = self.read(proc)
        gate = threading.Lock()
        with patch.object(cmd, '_GATE', gate):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertNotIn('private', str(caught.exception))
            deadline = time.monotonic() + 1
            while proc.wait_calls < 2 and time.monotonic() < deadline:
                time.sleep(.002)
            self.assertGreaterEqual(proc.wait_calls, 2)
            self.assertTrue(gate.locked())
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
            self.assertEqual(len(calls), 1)
            self.assertFalse(proc.stop_calls)


class HelperCleanupTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, *, child_pid=9876, ticks_change=False, already_exited=False,
                ineffective_kill=False, invalid_handle=False,
                spawn_started=None, spawn_release=None, exit_during_kill=False,
                identity_started=None, identity_release=None, oversized_output=False,
                directory_provider=None, kill_started=None, kill_release=None):
        created = []
        class FakeOwned:
            def __init__(self, args, **kwargs):
                self.pid, self._handle = child_pid, (0 if invalid_handle else 777777)
                self.stop_calls = []
                self.exit_event, self.read_release = threading.Event(), threading.Event()
                self.stdin = InPipe()
                if already_exited:
                    self.exit_event.set()
                proc = self
                class Out:
                    def read(self, limit):
                        if oversized_output:
                            return b'x' * limit
                        proc.read_release.wait(3)
                        return b''
                    def close(self): pass
                self.stdout = Out()
                created.append(self)
                if spawn_started is not None:
                    spawn_started.set()
                if spawn_release is not None:
                    spawn_release.wait(3)
            def poll(self):
                return 0 if self.exit_event.is_set() else None
            def kill(self):
                self.stop_calls.append('kill')
                if exit_during_kill:
                    self.exit_event.set()
                    self.read_release.set()
                    raise OSError('fake exit race; secret')
                if not ineffective_kill:
                    self.exit_event.set()
                    self.read_release.set()
                if kill_started is not None:
                    kill_started.set()
                if kill_release is not None:
                    kill_release.wait(3)
            def wait(self, timeout=None):
                if not self.exit_event.wait(timeout if timeout is not None else 3):
                    raise subprocess.TimeoutExpired('fake', timeout)
                return 0
        class Backend:
            calls = 0
            def process_id(self, handle):
                if identity_started is not None:
                    identity_started.set()
                if identity_release is not None:
                    identity_release.wait(3)
                return child_pid
            def creation_ticks(self, handle):
                self.calls += 1
                return 7 if not ticks_change or self.calls == 1 else 8
        backend = Backend()
        with patch.object(cmd, '_DIRECT_POPEN', FakeOwned), \
             patch.object(cmd, '_is_windows', return_value=True), \
             patch.object(cmd, '_system_directory', directory_provider or (lambda: DIRECTORY)), \
             patch.object(cmd.windows_process, 'NativeBackend', lambda: backend), \
             patch.object(cmd, '_CLEANUP_TIMEOUT', .08), \
             patch.object(cmd, '_CLEANUP_REPLY_WAIT', .18):
            reader = cmd.PowerShellWmiReader(timeout=.03)
            try:
                yield reader, created, backend, FakeOwned
            finally:
                for proc in created:
                    proc.exit_event.set()
                    proc.read_release.set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if cmd._GATE.acquire(False):
                        cmd._GATE.release()
                        break
                    time.sleep(.002)
                else:
                    self.fail('fake owned helper was not reaped')

    def test_timeout_kills_only_owned_handle_and_waits(self):
        with self.fixture() as (reader, created, backend, cls):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_timeout')
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].stop_calls, ['kill'])
            self.assertTrue(created[0].exit_event.is_set())
            self.assertGreaterEqual(backend.calls, 3)

    def test_gate_stays_held_until_concurrent_cleanup_finishes(self):
        started, release = threading.Event(), threading.Event()
        with self.fixture(kill_started=started, kill_release=release) as (reader, created, _, cls):
            outcomes = []
            def call():
                try:
                    reader.read_command_line(PID)
                except cmd.CommandLineObservationError as exc:
                    outcomes.append(exc.code)
            thread = threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(created[0].exit_event.is_set())
                self.assertTrue(cmd._GATE.locked())
                self.assertEqual(outcomes, [])
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ['observer_timeout'])
            self.assertEqual(created[0].stop_calls, ['kill'])

    def test_already_exited_helper_is_waited_not_killed(self):
        with self.fixture(already_exited=True) as (reader, created, _, cls):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'helper_cleanup_unconfirmed')
            self.assertEqual(created[0].stop_calls, [])
            self.assertTrue(created[0].exit_event.is_set())

    def test_exit_racing_kill_is_accepted_only_after_wait(self):
        with self.fixture(exit_during_kill=True) as (reader, created, _, cls):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_timeout')
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(created[0].stop_calls, ['kill'])
            self.assertTrue(created[0].exit_event.is_set())

    def test_oversized_output_also_reaps_owned_helper(self):
        with self.fixture(oversized_output=True) as (reader, created, _, cls):
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'command_line_response_invalid')
            self.assertEqual(created[0].stop_calls, ['kill'])
            self.assertTrue(created[0].exit_event.is_set())

    def test_unknown_ownership_never_stops_child(self):
        for kwargs in ({'child_pid': PID}, {'ticks_change': True}, {'invalid_handle': True}):
            with self.subTest(kwargs=kwargs), self.fixture(**kwargs) as (reader, created, _, cls):
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertEqual(caught.exception.code, 'helper_ownership_unverified')
                self.assertEqual(created[0].stop_calls, [])
                if not kwargs.get('ticks_change'):
                    self.assertEqual(created[0].stdin.writes, [])

    def test_injected_provider_never_qualifies_for_termination(self):
        with self.fixture() as (_, created, backend, cls):
            reader = cmd.PowerShellWmiReader(timeout=.03,
                        _directory_provider=lambda: DIRECTORY)
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'helper_ownership_unverified')
            self.assertEqual(created[0].stop_calls, [])
            self.assertEqual(created[0].stdin.writes, [])

    def test_cleanup_unconfirmed_is_bounded_and_not_retried(self):
        with self.fixture(ineffective_kill=True) as (reader, created, _, cls):
            started = time.monotonic()
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(caught.exception.code, 'helper_cleanup_unconfirmed')
            self.assertEqual(created[0].stop_calls, ['kill'])
            with self.assertRaises(cmd.CommandLineObservationError) as caught:
                reader.read_command_line(PID)
            self.assertEqual(caught.exception.code, 'observer_busy')
            self.assertEqual(created[0].stop_calls, ['kill'])

    def test_repeated_cleanup_does_not_kill_twice(self):
        with self.fixture() as (reader, created, backend, cls):
            process = cls([], shell=False)
            guard = cmd._capture_owned_helper(process, PID, True, lambda: backend)
            self.assertIsNotNone(guard)
            self.assertEqual(guard.cleanup(), 'reaped')
            self.assertEqual(guard.cleanup(), 'reaped')
            self.assertEqual(process.stop_calls, ['kill'])

    def test_concurrent_cleanup_requests_kill_once(self):
        with self.fixture() as (_, created, backend, cls):
            process = cls([], shell=False)
            guard = cmd._capture_owned_helper(process, PID, True, lambda: backend)
            lifecycle = cmd._HelperLifecycle()
            self.assertTrue(lifecycle.begin_spawn())
            self.assertTrue(lifecycle.attach(guard))
            threads = [threading.Thread(target=lifecycle.request_cleanup) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
            self.assertEqual(lifecycle.wait_cleanup(), 'reaped')
            self.assertEqual(process.stop_calls, ['kill'])

    def test_changed_handle_prevents_cleanup_and_repeated_stop(self):
        with self.fixture() as (reader, created, backend, cls):
            process = cls([], shell=False)
            guard = cmd._capture_owned_helper(process, PID, True, lambda: backend)
            self.assertIsNotNone(guard)
            process._handle = 888888
            self.assertEqual(guard.cleanup(), 'helper_ownership_unverified')
            self.assertEqual(guard.cleanup(), 'helper_ownership_unverified')
            self.assertEqual(process.stop_calls, [])

    def test_cancel_before_spawn_prevents_any_helper(self):
        release = threading.Event()
        with self.fixture(directory_provider=lambda: (release.wait(2), DIRECTORY)[1]) as (reader, created, _, cls):
            try:
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertEqual(caught.exception.code, 'observer_timeout')
                self.assertEqual(created, [])
            finally:
                release.set()

    def test_slow_spawn_is_reported_then_late_child_is_cleaned(self):
        started, release = threading.Event(), threading.Event()
        with self.fixture(spawn_started=started, spawn_release=release) as (reader, created, _, cls):
            try:
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertTrue(started.is_set())
                self.assertEqual(caught.exception.code, 'helper_cleanup_unconfirmed')
                self.assertEqual(created[0].stop_calls, [])
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertEqual(caught.exception.code, 'observer_busy')
                release.set()
                self.assertTrue(created[0].exit_event.wait(2))
                self.assertEqual(created[0].stop_calls, ['kill'])
            finally:
                release.set()

    def test_slow_ownership_capture_reports_unknown_then_cleans(self):
        started, release = threading.Event(), threading.Event()
        with self.fixture(identity_started=started, identity_release=release) as (reader, created, _, cls):
            try:
                with self.assertRaises(cmd.CommandLineObservationError) as caught:
                    reader.read_command_line(PID)
                self.assertTrue(started.is_set())
                self.assertEqual(caught.exception.code, 'helper_cleanup_unconfirmed')
                self.assertEqual(created[0].stop_calls, [])
                release.set()
                self.assertTrue(created[0].exit_event.wait(2))
                self.assertEqual(created[0].stop_calls, ['kill'])
            finally:
                release.set()


if __name__ == '__main__': unittest.main()
