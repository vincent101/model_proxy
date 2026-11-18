"""Darwin zombie evidence; only owned children inside a copied fake project."""
import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core import platform_process, server
from core.discovery import PreflightError, process_executable, process_start_identity


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin process state required')
class RealOwnedZombie(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ISO, prefix='zombie-fake-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'; self.home.mkdir(mode=0o700)
        self.child = None

    def cleanup_child(self):
        if self.child is None:
            return
        # wait()/poll() are intentionally never called before zombie assertions.
        if self.child.stdin is not None:
            self.child.stdin.close()
            self.child.stdin = None
        try:
            self.child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.child.kill()  # Only the direct child created by this fixture.
            self.child.wait(timeout=3)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            if stream is not None:
                stream.close()

    def start_owned(self):
        env = {'HOME': str(self.home), 'TMPDIR': str(self.root), 'PATH': '/usr/bin:/bin',
               'PYTHONDONTWRITEBYTECODE': '1'}
        self.child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c',
                                       "import sys;print('FAKE_READY',flush=True);sys.stdin.read()"],
                                      cwd=self.root, env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_child)
        self.assertEqual(self.child.stdout.readline(), b'FAKE_READY\n')
        self.start = process_start_identity(self.child.pid)
        self.image = process_executable(self.child.pid)
        self.assertIsNotNone(self.start); self.assertIsNotNone(self.image)
        platform_process.check_identity(self.child.pid, self.start, self.image)
        return self.child

    def test_current_sdk_kinfo_abi_matches_decoder(self):
        code = r'''#include <stdio.h>
#include <stddef.h>
#include <sys/types.h>
#include <sys/sysctl.h>
#include <sys/proc.h>
int main(void) {
 printf("%zu %zu %zu %zu %zu %zu %d %d %d %d\n",
        sizeof(struct kinfo_proc), offsetof(struct kinfo_proc,kp_proc.p_stat),
        offsetof(struct kinfo_proc,kp_proc.p_pid),
        offsetof(struct kinfo_proc,kp_proc.p_starttime.tv_sec),
        offsetof(struct kinfo_proc,kp_proc.p_starttime.tv_usec),
        sizeof(((struct kinfo_proc*)0)->kp_proc.p_starttime.tv_usec),
        CTL_KERN,KERN_PROC,KERN_PROC_PID,SZOMB);
 return 0;
}
'''
        source, binary = self.root / 'abi.c', self.root / 'abi'
        source.write_text(code)
        env = {'HOME': str(self.home), 'TMPDIR': str(self.root), 'PATH': '/usr/bin:/bin'}
        sdk = subprocess.run(['/usr/bin/xcrun', '--show-sdk-path'], env=env, check=True,
                             capture_output=True, text=True).stdout.strip()
        subprocess.run(['/usr/bin/clang', '-isysroot', sdk, str(source), '-o', str(binary)],
                       env=env, check=True, capture_output=True)
        output = subprocess.run([str(binary)], env=env, check=True,
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(output, '648 36 40 0 8 4 1 14 1 5')

    def make_unreaped_zombie(self):
        # Popen owns this child; signal and eventual wait target this handle only.
        self.child.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if result is not None:
                self.assertEqual(result.si_pid, self.child.pid)
                os.kill(self.child.pid, 0)
                return
            time.sleep(0.01)
        self.fail('owned fake child did not reach unreaped exit')

    def test_live_then_unreaped_zombie_requires_matching_identity(self):
        self.start_owned()
        self.assertEqual(platform_process.darwin_recorded_state(self.child.pid, self.start), 'ALIVE')
        self.assertFalse(platform_process.process_gone(self.child.pid, self.start, self.image))
        self.make_unreaped_zombie()
        self.assertEqual(platform_process.darwin_recorded_state(self.child.pid, self.start), 'ZOMBIE')
        self.assertTrue(platform_process.process_gone(self.child.pid, self.start, self.image))
        wrong = dict(self.start, seconds=self.start['seconds'] + 1)
        with self.assertRaises(platform_process.ProcessEvidenceError):
            platform_process.process_gone(self.child.pid, wrong, self.image)
        original_kill = os.kill
        calls = []
        def guard(pid, sig):
            self.assertEqual(pid, self.child.pid)
            calls.append(sig)
            if sig != 0:
                raise AssertionError('no second signal to zombie')
            return original_kill(pid, sig)
        with mock.patch.object(platform_process.os, 'kill', side_effect=guard):
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process.signal_verified(self.child.pid, self.start, self.image)
        self.assertNotIn(signal.SIGTERM, calls)
        # WNOWAIT confirms the test has not accidentally reaped the child.
        self.assertIsNotNone(os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG))

    def test_server_stale_check_accepts_only_exact_zombie_not_unknown(self):
        self.start_owned()
        paths = {'fake': 'paths'}; locks = {'fake': 'locks'}
        active = {'pid': self.child.pid, 'process_start': self.start,
                  'runtime_paths': paths, 'locks': locks}
        with self.assertRaises(PreflightError) as running:
            server._ensure_stale_instance({'active': active}, paths, {}, locks)
        self.assertEqual(running.exception.code, 'process_still_active')
        self.make_unreaped_zombie()
        server._ensure_stale_instance({'active': active}, paths, {}, locks)
        changed = dict(active, process_start=dict(self.start, seconds=self.start['seconds'] + 1))
        with self.assertRaises(PreflightError) as unknown:
            server._ensure_stale_instance({'active': changed}, paths, {}, locks)
        self.assertEqual(unknown.exception.code, 'process_unverified')
        self.assertIsNotNone(os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG))

    def test_live_process_missing_image_is_never_declared_exited(self):
        self.start_owned()
        with mock.patch.object(platform_process, 'process_executable', return_value=None):
            self.assertFalse(platform_process.process_gone(self.child.pid, self.start, self.image))
            with mock.patch.object(platform_process.os, 'kill') as kill:
                with self.assertRaises(platform_process.ProcessEvidenceError):
                    platform_process.signal_verified(self.child.pid, self.start, self.image)
                kill.assert_not_called()

    def test_live_pid_reuse_is_not_classified_as_zombie(self):
        self.start_owned()
        wrong = dict(self.start, seconds=self.start['seconds'] + 1)
        with self.assertRaises(platform_process.ProcessEvidenceError):
            platform_process.darwin_recorded_state(self.child.pid, wrong)
        with self.assertRaises(platform_process.ProcessEvidenceError):
            platform_process.process_gone(self.child.pid, wrong, self.image)
        # Existing server policy treats a stable different start as a stale
        # *old generation*, not proof that this new process is dead. Locks
        # and ports remain separate startup gates.
        paths, locks = {'fake': 'paths'}, {'fake': 'locks'}
        active = {'pid': self.child.pid, 'process_start': wrong,
                  'runtime_paths': paths, 'locks': locks}
        server._ensure_stale_instance({'active': active}, paths, {}, locks)
        os.kill(self.child.pid, 0)


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin classification contract')
class InjectedStateFailures(unittest.TestCase):
    def setUp(self):
        self.pid = 23456  # All observations/signals for this synthetic PID are mocked.
        self.start = {'kind': 'darwin_proc_bsdinfo', 'seconds': 12345, 'microseconds': 12}
        self.old = (self.pid, 12345, 12, 5)

    def classify(self, observations):
        with mock.patch.object(platform_process, '_darwin_check_kinfo_abi'), \
             mock.patch.object(platform_process, '_darwin_kinfo_one', side_effect=observations), \
             mock.patch.object(platform_process.os, 'kill') as probe:
            result = platform_process.darwin_recorded_state(self.pid, self.start)
            self.assertTrue(all(call.args == (self.pid, 0) for call in probe.call_args_list))
            return result

    def test_only_two_matching_zombie_observations_authorize_exit(self):
        self.assertEqual(self.classify([self.old, self.old]), 'ZOMBIE')
        for pair in ((self.old, (self.pid, 12345, 12, 2)),
                     (self.old, (self.pid, 12346, 12, 5)),
                     ((self.pid, 12346, 12, 5), (self.pid, 12346, 12, 5))):
            with self.subTest(pair=pair), self.assertRaises(platform_process.ProcessEvidenceError):
                self.classify(pair)

    def test_unavailable_abi_state_or_permission_never_proves_exit(self):
        with mock.patch.object(platform_process, '_darwin_check_kinfo_abi',
                               side_effect=platform_process.ProcessEvidenceError('process_state_unavailable')), \
             mock.patch.object(platform_process.os, 'kill'):
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process.process_gone(self.pid, self.start, '/fake/python3')
        with mock.patch.object(platform_process, '_darwin_check_kinfo_abi'), \
             mock.patch.object(platform_process.os, 'kill', side_effect=PermissionError()):
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process.darwin_recorded_state(self.pid, self.start)

    def test_unknown_status_and_partial_sysctl_never_prove_exit(self):
        with self.assertRaises(platform_process.ProcessEvidenceError):
            self.classify([(self.pid, 12345, 12, 99)] * 2)
        class Truncated:
            def __call__(self, _mib, _count, _buffer, size, _new, _newlen):
                ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 647
                return 0
        fake = types.SimpleNamespace(sysctl=Truncated())
        with mock.patch.object(platform_process.ctypes, 'CDLL', return_value=fake):
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process._darwin_kinfo_one(self.pid)

    def test_invalid_pid_or_record_is_rejected_before_any_os_probe(self):
        for pid, start in ((-1, self.start), (2 ** 40, self.start),
                           (self.pid, dict(self.start, microseconds=1_000_000)),
                           (self.pid, dict(self.start, seconds=2 ** 70))):
            with self.subTest(pid=pid, start=start), \
                 mock.patch.object(platform_process.os, 'kill') as probe:
                with self.assertRaises(platform_process.ProcessEvidenceError):
                    platform_process.process_gone(pid, start, '/fake/python3')
                probe.assert_not_called()

    def test_observed_zombie_never_receives_sigterm(self):
        with mock.patch.object(platform_process, 'check_identity'), \
             mock.patch.object(platform_process, 'darwin_recorded_state', return_value='ZOMBIE'), \
             mock.patch.object(platform_process.os, 'kill') as kill:
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process.signal_verified(self.pid, self.start, '/fake/python3')
            kill.assert_not_called()

    def test_unobservable_state_never_receives_sigterm(self):
        with mock.patch.object(platform_process, 'check_identity'), \
             mock.patch.object(platform_process, 'darwin_recorded_state',
                               side_effect=platform_process.ProcessEvidenceError('process_state_unavailable')), \
             mock.patch.object(platform_process.os, 'kill') as kill:
            with self.assertRaises(platform_process.ProcessEvidenceError):
                platform_process.signal_verified(self.pid, self.start, '/fake/python3')
            kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
