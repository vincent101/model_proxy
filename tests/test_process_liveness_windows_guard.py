"""Windows liveness must fail before any signal; POSIX semantics are unchanged."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core import platform_process


class ProcessGoneWindowsGuard(unittest.TestCase):
    def test_either_windows_indicator_rejects_every_pid_before_any_probe(self):
        # Include inconsistent indicators: neither one may override the guard.
        indicators = (('win32', 'nt'), ('win32', 'posix'),
                      ('linux', 'nt'), ('darwin', 'nt'))
        pids = (0, 1, 42, -1, 2 ** 40, None, True, 'sensitive-pid')
        for platform, name in indicators:
            for pid in pids:
                with self.subTest(platform=platform, name=name, pid=pid):
                    with mock.patch.object(platform_process.sys, 'platform', platform), \
                         mock.patch.object(platform_process.os, 'name', name), \
                         mock.patch.object(platform_process.os, 'kill') as kill, \
                         mock.patch.object(platform_process, 'check_identity') as identity, \
                         mock.patch.object(platform_process, '_validate_darwin_record') as validate, \
                         mock.patch.object(platform_process, 'darwin_recorded_state') as state:
                        with self.assertRaises(platform_process.ProcessEvidenceError) as caught:
                            platform_process.process_gone(pid, {'secret': 'FAKE_SECRET'},
                                                          '/fake/secret-executable')
                        self.assertEqual(str(caught.exception), 'process_liveness_unsupported')
                        kill.assert_not_called()
                        identity.assert_not_called()
                        validate.assert_not_called()
                        state.assert_not_called()


class ProcessGonePosixParity(unittest.TestCase):
    def run_probe(self, effects, identity_effect=None):
        with mock.patch.object(platform_process.sys, 'platform', 'linux'), \
             mock.patch.object(platform_process.os, 'name', 'posix'), \
             mock.patch.object(platform_process.os, 'kill', side_effect=effects) as kill, \
             mock.patch.object(platform_process, 'check_identity', side_effect=identity_effect) as identity:
            try:
                outcome = ('return', platform_process.process_gone(42, {'fake': 1}, '/fake/python'))
            except platform_process.ProcessEvidenceError as exc:
                outcome = ('error', str(exc))
            return outcome, kill.call_args_list, identity.call_args_list

    def test_absent_and_permission_denied(self):
        for effect, expected in ((ProcessLookupError(), ('return', True)),
                                 (PermissionError(), ('error', 'process_liveness_unavailable'))):
            with self.subTest(expected=expected):
                result, calls, identity = self.run_probe([effect])
                self.assertEqual(result, expected)
                self.assertEqual(calls, [mock.call(42, 0)])
                self.assertEqual(identity, [])

    def test_live_and_changed_identity(self):
        for effect, expected in ((None, ('return', False)),
                                 (platform_process.ProcessEvidenceError('process_identity_changed'),
                                  ('error', 'process_identity_changed'))):
            with self.subTest(expected=expected):
                result, calls, identity = self.run_probe([None], effect)
                self.assertEqual(result, expected)
                self.assertEqual(calls, [mock.call(42, 0)])
                self.assertEqual(identity, [mock.call(42, {'fake': 1}, '/fake/python')])

    def test_unavailable_identity_requires_second_liveness_probe(self):
        unknown = platform_process.ProcessEvidenceError('process_identity_unavailable')
        for second, expected in ((None, ('return', False)),
                                 (ProcessLookupError(), ('return', True)),
                                 (PermissionError(), ('error', 'process_liveness_unavailable'))):
            with self.subTest(expected=expected):
                result, calls, identity = self.run_probe([None, second], unknown)
                self.assertEqual(result, expected)
                self.assertEqual(calls, [mock.call(42, 0), mock.call(42, 0)])
                self.assertEqual(identity, [mock.call(42, {'fake': 1}, '/fake/python')])


if __name__ == '__main__':
    unittest.main()
