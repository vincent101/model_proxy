"""Isolated, no-network/no-signal regressions for Mac lifecycle recovery."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISOLATION / 'project'
        or not (ISOLATION / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISOLATION)):
    raise RuntimeError('isolated source snapshot and fake HOME required')
sys.path.insert(0, str(ISOLATION / 'project'))

from core.cli import authority, control
from core import platform_process
from core.discovery import (PreflightError, inspect_runtime_identities,
                            verify_record_directory, write_instance_record)
from core.platform_lock import acquire_lock, verify_local_access
from core.runtime_paths import PATH_KEYS


class LifecycleRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ISOLATION, prefix='repair-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'fake-project'
        (self.root / 'config').mkdir(parents=True)
        (self.root / 'runtime').mkdir()
        (self.root / 'core/cli').mkdir(parents=True)
        (self.root / 'core/cli/model_proxy.py').write_text('# fake service entry\n')
        self.config = self.root / 'config/model_proxy_config.json'
        self.paths = {key: str(self.root / 'runtime' / key) for key in PATH_KEYS}
        self.active = {'runtime_paths': self.paths, 'pid': 54321}
        self.good = {'runtime_paths': self.paths}
        self.records = {'install': {'ever_started': True}, 'active': self.active,
                        'last_good_paths': self.good}
        self.instance = control.Instance(54321, {'fake': 1}, '/fake/python3', 'a'*32,
                                         '127.0.0.1', 29491, self.active, self.good)
        self.obs = mock.Mock(_root=self.root, _config_path=self.config, _paths=self.paths)
        self.obs.record_set.return_value = self.records
        self.obs.records.return_value = (self.active, self.good)
        self.lease = authority.MacAuthorityLease(self.obs)
        self.cli = control.MacControl(self.root, config_path=self.config,
                                      out=io.StringIO(), err=io.StringIO())

    def test_stop_recovers_from_both_damaged_inputs_without_reading_them(self):
        # Includes missing business config and malformed runtime config.
        (self.root / 'config/runtime_paths.json').write_text('{broken')
        env = {'MODEL_PROXY_CONFIG': str(self.config)}
        with self.lease.stopping(), \
             mock.patch.object(self.lease, '_launch', return_value=env) as launch, \
             mock.patch.object(self.lease, '_configuration', side_effect=AssertionError('must not read')), \
             mock.patch.object(self.lease, '_assert_stop_locks') as locks, \
             mock.patch.object(platform_process, 'check_identity') as identity:
            self.assertTrue(self.lease.verify_active(self.instance))
        self.assertEqual(launch.call_count, 2)
        locks.assert_called_once_with(self.instance, require_identity_evidence=True)
        self.assertEqual(identity.call_count, 2)

    def test_stop_does_not_relax_launch_or_process_identity(self):
        for fail in ('launch', 'identity', 'record'):
            with self.subTest(fail=fail), self.lease.stopping(), \
                 mock.patch.object(self.lease, '_launch', return_value={}) as launch, \
                 mock.patch.object(platform_process, 'check_identity') as identity:
                self.obs.record_set.return_value = self.records
                if fail == 'launch': launch.side_effect = control.ControlError('installation_identity_unknown')
                if fail == 'identity': identity.side_effect = platform_process.ProcessEvidenceError('process_identity_changed')
                if fail == 'record': self.obs.record_set.return_value = dict(self.records, active={})
                with self.assertRaises((control.ControlError, platform_process.ProcessEvidenceError)):
                    self.lease.verify_active(self.instance)

    def test_launch_recovery_accepts_missing_config_but_not_relative_entry_or_config(self):
        entry = self.root / 'core/cli/model_proxy.py'
        snap = SimpleNamespace(exists=True)
        args = ('/fake/python3', str(entry))
        with self.lease.stopping(), mock.patch.object(authority, '_capture', return_value=snap), \
             mock.patch.object(platform_process, 'darwin_launch_evidence') as evidence:
            evidence.return_value = (args, {'MODEL_PROXY_CONFIG': str(self.config)})
            self.assertEqual(self.lease._launch(54321, {'fake': 1}, '/fake/python3', 'a'*32),
                             {'MODEL_PROXY_CONFIG': str(self.config)})
            # A different absolute launch path is not compared or read for
            # plain off: the protected installation record selects the target.
            evidence.return_value = (args, {'MODEL_PROXY_CONFIG': str(self.root / 'other.json')})
            self.assertEqual(self.lease._launch(54321, {'fake': 1}, '/fake/python3', 'a'*32),
                             {'MODEL_PROXY_CONFIG': str(self.root / 'other.json')})
            for argv, config in ((('/fake/python3', 'core/cli/model_proxy.py'), str(self.config)),
                                 (args, 'config/model_proxy_config.json')):
                evidence.return_value = (argv, {'MODEL_PROXY_CONFIG': config})
                with self.assertRaises(control.ControlError):
                    self.lease._launch(54321, {'fake': 1}, '/fake/python3', 'a'*32)

    def test_recovery_locator_rechecks_protected_records_under_lock(self):
        @contextlib.contextmanager
        def locked(root, paths, config, *, recovery):
            self.assertTrue(recovery)
            self.assertEqual(paths, self.paths)
            yield self.obs
        provider = authority.MacInstallationAuthority()
        with mock.patch.object(authority, 'read_instance_set', return_value=self.records), \
             mock.patch.object(authority, 'locked_observation', side_effect=locked), \
             mock.patch.object(authority, '_valid_record', return_value=self.instance):
            with provider.locked_for_stop(self.root, self.config) as lease:
                self.assertIsInstance(lease, authority.MacAuthorityLease)
            self.obs.record_set.return_value = dict(self.records, active={})
            with self.assertRaises(control.ControlError):
                with provider.locked_for_stop(self.root, self.config):
                    self.fail('changed record must not authorize')

    def test_recovery_locator_rejects_missing_or_invalid_protected_records(self):
        provider = authority.MacInstallationAuthority()
        for outcome in (None, PreflightError('records_inconsistent', 'fake')):
            with self.subTest(outcome=outcome), \
                 mock.patch.object(authority, 'read_instance_set', side_effect=outcome if isinstance(outcome, Exception) else None,
                                   return_value=outcome), \
                 mock.patch.object(authority, 'locked_observation') as locked:
                with self.assertRaises((control.ControlError, PreflightError)):
                    with provider.locked_for_stop(self.root, self.config): pass
                locked.assert_not_called()

    def test_repeated_off_needs_gone_released_and_free_port(self):
        lease = mock.Mock(unsafe=True)
        lease.records.return_value = (self.active, self.good)
        lease.assert_stopped.return_value = True
        lease.stopping = None
        with mock.patch.object(control, '_valid_record', return_value=self.instance), \
             mock.patch.object(platform_process, 'process_gone', return_value=True), \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(self.cli, '_port_free') as port:
            for _ in range(2): self.cli._stop(lease, allow_stale=True)
            self.assertEqual(lease.assert_stopped.call_count, 2)
            self.assertEqual(port.call_count, 2)
            signal.assert_not_called()
            lease.assert_stopped.return_value = False
            with self.assertRaises(control.ControlError): self.cli._stop(lease, allow_stale=True)
            lease.assert_stopped.return_value = True
            port.side_effect = control.ControlError('port_unavailable')
            with self.assertRaises(control.ControlError): self.cli._stop(lease, allow_stale=True)
            signal.assert_not_called()

    def test_unknown_liveness_never_signals_or_claims_stopped(self):
        lease = mock.Mock(unsafe=True)
        lease.records.return_value = (self.active, self.good)
        lease.stopping = None
        with mock.patch.object(control, '_valid_record', return_value=self.instance), \
             mock.patch.object(platform_process, 'process_gone', side_effect=platform_process.ProcessEvidenceError('process_liveness_unavailable')), \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(self.cli, '_port_free') as port:
            with self.assertRaises(platform_process.ProcessEvidenceError):
                self.cli._stop(lease, allow_stale=True)
            signal.assert_not_called(); port.assert_not_called(); lease.assert_stopped.assert_not_called()

    def test_off_selects_stale_handling_and_recovery_lock_only_for_off(self):
        auth = mock.Mock()
        @contextlib.contextmanager
        def locked(*args): yield self.lease
        auth.locked.side_effect = locked
        auth.locked_for_stop.side_effect = locked
        self.cli.authority = auth
        with mock.patch.object(self.cli, '_stop') as stop:
            self.assertEqual(self.cli.off(), 0)
            stop.assert_called_once_with(self.lease, allow_stale=True,
                                         require_identity_evidence=False)
        auth.locked_for_stop.assert_called_once()
        auth.locked.assert_not_called()

    def test_never_started_restart_requires_explicit_proof_and_pins_records(self):
        record = {'install': {'ever_started': False}, 'active': None, 'last_good_paths': None}
        self.obs.record_set.return_value = record
        self.assertTrue(self.lease.assert_restart_supported())
        self.assertTrue(self.lease.assert_never_started())
        self.obs.record_set.return_value = dict(record, install={'ever_started': True})
        self.assertFalse(self.lease.assert_never_started())
        self.obs.record_set.return_value = None
        with self.assertRaises(control.ControlError): self.lease.assert_restart_supported()
        self.assertFalse(self.lease.assert_never_started())

    def test_restart_skips_stop_only_for_literal_true(self):
        @contextlib.contextmanager
        def locked(*args): yield self.lease
        self.cli.authority = SimpleNamespace(locked=locked)
        for proof in (True, False, None, 1):
            with self.subTest(proof=proof), \
                 mock.patch.object(self.lease, 'assert_restart_supported', return_value=True), \
                 mock.patch.object(self.lease, 'assert_never_started', return_value=proof), \
                 mock.patch.object(self.cli, '_stop') as stop, \
                 mock.patch.object(self.cli, '_start') as start:
                self.assertEqual(self.cli.restart(), 0)
                if proof is True: stop.assert_not_called()
                else: stop.assert_called_once_with(self.lease, allow_stale=True)
                start.assert_called_once_with(self.lease, allow_initialize=False)

    def test_restart_preflight_error_never_stops_or_starts(self):
        @contextlib.contextmanager
        def locked(*args): yield self.lease
        self.cli.authority = SimpleNamespace(locked=locked)
        with mock.patch.object(self.lease, 'assert_restart_supported', side_effect=control.ControlError('installation_identity_unknown')), \
             mock.patch.object(self.cli, '_stop') as stop, mock.patch.object(self.cli, '_start') as start:
            self.assertEqual(self.cli.restart(), 1)
            stop.assert_not_called(); start.assert_not_called()


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin ACL and locks required')
class ProtectedMetadataTests(unittest.TestCase):
    """Real isolated metadata/locks; process and network actions are mocked."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ISOLATION, prefix='protected-repair-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        self.root.mkdir(mode=0o700)
        (self.root / '.runtime').mkdir(mode=0o700)
        (self.root / 'config').mkdir(mode=0o700)
        (self.root / 'runtime').mkdir(mode=0o700)
        (self.root / 'core/cli').mkdir(parents=True, mode=0o700)
        (self.root / 'core/cli/model_proxy.py').write_text('# fake entry\n')
        self.paths = {key: str(self.root / 'runtime' / key) for key in PATH_KEYS}
        self.config = self.root / 'config/model_proxy_config.json'
        self.config.write_text(json.dumps({'admin_token': 'fake-only',
                                           'listen': {'host': '127.0.0.1', 'port': 29491}}))
        self.config.chmod(0o600)
        self.runtime = self.root / 'config/runtime_paths.json'
        self.runtime.write_text(json.dumps(self.paths)); self.runtime.chmod(0o600)
        self.state = self.root / '.runtime'
        self.cli = control.MacControl(self.root, authority=authority.MacInstallationAuthority(),
                                      config_path=self.config, out=io.StringIO(), err=io.StringIO())

    def write_records(self, started=True):
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, 'anchor.lock', create=True, access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, 'lock', create=True, access_verifier=verify_local_access,
                              anchor=anchor) as legacy:
                dev, ino = verify_record_directory(self.state)
                nonce = 'a' * 32
                install = {'schema': 2, 'record': 'install', 'project_root': str(self.root),
                           'install_id': 'b' * 32, 'root_identity': {'device': dev, 'inode': ino},
                           'initialization_source': 'fake isolated unit fixture',
                           'ever_started': started, 'last_instance_nonce': nonce if started else None}
                if started:
                    common = {'schema': 2, 'project_root': str(self.root), 'install_id': install['install_id'],
                              'instance_nonce': nonce, 'listen': {'host': '127.0.0.1', 'port': 29491},
                              'runtime_paths': self.paths}
                    locks = {}
                    for name, held in (('anchor', anchor), ('legacy', legacy)):
                        st = os.fstat(held.fileno()); locks[name] = {'device': st.st_dev, 'inode': st.st_ino}
                    active = dict(common, record='active', state='READY', pid=54321,
                                  process_start={'kind': 'darwin_proc_bsdinfo', 'seconds': 1, 'microseconds': 0},
                                  executable='/fake/python3', locks=locks)
                    good = dict(common, record='last_good_paths')
                    for name, value in (('last_good_paths.json', good), ('active.json', active)):
                        write_instance_record(self.state, name, value, anchor=anchor, legacy=legacy)
                write_instance_record(self.state, 'install.json', install, anchor=anchor, legacy=legacy)

    def test_real_protected_records_allow_recovery_when_both_configs_damaged(self):
        self.write_records()
        self.runtime.write_text('{bad')
        self.config.unlink()
        with mock.patch.object(authority.MacAuthorityLease, '_launch', return_value={}) as launch, \
             mock.patch.object(authority.MacAuthorityLease, '_assert_stop_locks') as locks, \
             mock.patch.object(platform_process, 'check_identity') as identity, \
             mock.patch.object(platform_process, 'process_gone', side_effect=[False, True, True, True]), \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(control.MacControl, '_port_free') as port:
            self.assertEqual(self.cli.off(), 0, self.cli.err.getvalue())
            signal.assert_called_once()
            self.assertGreaterEqual(launch.call_count, 2)
            self.assertGreaterEqual(identity.call_count, 2)
            self.assertGreaterEqual(locks.call_count, 1)
            port.assert_called_once()

    def test_corrupt_runtime_never_enables_non_off_operations(self):
        self.write_records()
        self.runtime.write_text('{bad')
        with mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(authority.MacAuthorityLease, 'admin_token') as token, \
             mock.patch.object(control.subprocess, 'Popen') as spawn:
            for operation in (self.cli.on, self.cli.restart, self.cli.status, self.cli.reload):
                self.assertEqual(operation(), 1)
            signal.assert_not_called(); token.assert_not_called(); spawn.assert_not_called()

    def test_real_protected_records_detect_tamper_before_any_signal(self):
        self.write_records()
        (self.state / 'last_good_paths.json').write_text('{}')
        with mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(platform_process, 'process_gone') as gone:
            self.assertEqual(self.cli.off(), 1)
            signal.assert_not_called(); gone.assert_not_called()

    def test_real_never_started_set_runs_start_preflight(self):
        self.write_records(started=False)
        with self.cli.authority.locked(self.root, self.config) as lease:
            self.assertTrue(lease.assert_restart_supported())
            self.assertTrue(lease.assert_never_started())
            self.assertTrue(lease.assert_startable())

    def test_real_stale_metadata_requires_released_locks_and_free_port(self):
        self.write_records()
        with mock.patch.object(platform_process, 'process_gone', return_value=True), \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(control.MacControl, '_port_free') as port:
            self.assertEqual(self.cli.off(), 0, self.cli.err.getvalue())
            self.assertEqual(self.cli.off(), 0, self.cli.err.getvalue())
            self.assertEqual(port.call_count, 2)
            port.side_effect = control.ControlError('port_unavailable')
            self.assertEqual(self.cli.off(), 1)
            signal.assert_not_called()

    def test_stale_record_with_held_anchor_fails_closed(self):
        self.write_records()
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, 'anchor.lock', access_verifier=verify_local_access):
            with mock.patch.object(platform_process, 'process_gone', return_value=True), \
                 mock.patch.object(platform_process, 'signal_verified') as signal, \
                 mock.patch.object(control.MacControl, '_port_free') as port:
                self.assertEqual(self.cli.off(), 1)
                signal.assert_not_called(); port.assert_not_called()

    def test_real_never_started_restart_reaches_guarded_spawn(self):
        self.write_records(started=False)
        child = mock.Mock(pid=12345)
        child.poll.return_value = 2  # No real child exists.
        with mock.patch.object(control.subprocess, 'Popen', return_value=child) as spawn, \
             mock.patch.object(platform_process, 'signal_verified') as signal, \
             mock.patch.object(control.MacControl, '_port_free'):
            self.assertEqual(self.cli.restart(), 1)
            self.assertIn('startup_failed', self.cli.err.getvalue())
            spawn.assert_called_once()
            signal.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
