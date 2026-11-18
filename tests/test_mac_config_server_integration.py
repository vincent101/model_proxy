"""Mac server integration against fake files; run only from an isolated copy."""
import json
import io
from contextlib import redirect_stderr
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(os.environ.get('MP_TEST_ROOT', '/invalid')).resolve()
PROJECT = Path(os.environ.get('MP_TEST_PROJECT', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != PROJECT or PROJECT.parent != ROOT
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ROOT)):
    raise RuntimeError('isolated project and fake HOME required')

from core import server
from core.config_snapshot import ConfigSnapshotError, read_mac_business_config
from core.discovery import PreflightError
from core.mac_config_path import MacConfigError


def config(generation='A', port=29611):
    return {'generation': generation, 'admin_token': 'FAKE_ADMIN',
            'supplies': [], 'routes': [], 'strategies': [],
            'listen': {'host': '127.0.0.1', 'port': port}}


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS required')
class ServerMacSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.real = self.root / 'real'
        self.real.mkdir(mode=0o700)
        self.inner = self.real / 'inner'
        self.inner.mkdir(mode=0o700)
        self.target = self.real / 'business.json'
        self.write(config())
        self.link = self.root / 'entry.json'
        self.link.symlink_to(self.target)
        self.paths = self.root / 'runtime.json'
        self.paths.write_text('{}'); self.paths.chmod(0o600)

    def write(self, value):
        self.target.write_text(json.dumps(value), encoding='utf-8')
        self.target.chmod(0o600)

    def guard(self, path=None):
        path = str(path or self.link)
        store = server.ConfigStore(path)
        initial = server._mac_private_config_snapshot(path)
        listen = server.resolve_listen(path)
        legacy = SimpleNamespace(snapshot=object())
        with patch.object(server, '_RUNTIME_PATHS_FILE', self.paths), \
             patch.object(server, '_capture_migration_ledger', return_value='fake-ledger'):
            guard = server._capture_migration_guard(path, store, listen, legacy, initial)
        return path, store, listen, legacy, guard

    def test_symlink_dotdot_selects_real_target_not_lexical_decoy(self):
        pivot = self.root / 'pivot'
        pivot.symlink_to(self.inner, target_is_directory=True)
        (self.root / 'business.json').write_text(json.dumps(config('DECOY')))
        (self.root / 'business.json').chmod(0o600)
        raw = str(pivot) + '/../business.json'
        store = server.ConfigStore(raw)
        self.assertEqual(store._config['generation'], 'A')
        self.assertEqual(store._business_snapshot.evidence.source_path, raw)
        self.assertEqual(store._business_snapshot.evidence.target_path, str(self.target))
        self.assertEqual(Path(raw).parent / 'session_overrides.json',
                         pivot / '..' / 'session_overrides.json')

    def test_trailing_slash_and_dot_are_not_silently_erased(self):
        for suffix in ('/', '/.'):
            with self.subTest(suffix=suffix), self.assertRaises(ConfigSnapshotError):
                server.ConfigStore(str(self.target) + suffix)

    def test_chain_replacement_same_target_is_reloaded(self):
        calls = []
        store = server.ConfigStore(self.link, on_reload=lambda: calls.append('reload'))
        marker = store._probe_marker
        self.link.unlink(); self.link.symlink_to('real/business.json')
        self.assertTrue(store.maybe_reload())
        self.assertNotEqual(store._probe_marker, marker)
        self.assertEqual(calls, ['reload'])
        self.assertEqual(store._config['generation'], 'A')

    def test_unsafe_or_bad_json_preserves_and_retry_after_repair(self):
        store = server.ConfigStore(self.link)
        old, marker, evidence = store._config, store._probe_marker, store._business_snapshot
        self.target.chmod(0o644)
        self.assertFalse(store.maybe_reload())
        self.assertIs(store._config, old); self.assertEqual(store._probe_marker, marker)
        self.assertIs(store._business_snapshot, evidence)
        self.target.chmod(0o600)
        self.target.write_text('{"broken":'); self.target.chmod(0o600)
        self.assertFalse(store.maybe_reload())
        self.assertIs(store._config, old); self.assertEqual(store._probe_marker, marker)
        self.write(config('B'))
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config['generation'], 'B')

    def test_failed_verification_after_parse_retains_previous_snapshot(self):
        store = server.ConfigStore(self.link)
        old, marker = store._config, store._probe_marker
        self.write(config('B'))
        with patch('core.mac_config_path.verify_config', side_effect=MacConfigError('FAKE_SECRET')):
            outcome = store.reload_detailed()
        self.assertFalse(outcome.loaded)
        self.assertNotIn('FAKE_SECRET', str(outcome))
        self.assertIs(store._config, old); self.assertEqual(store._probe_marker, marker)
        self.assertTrue(store.reload())

    def test_manual_failed_reload_still_clears_cooldown_and_unauth_cannot_reload(self):
        store = server.ConfigStore(self.link)
        handler = server.ModelProxyHandler.__new__(server.ModelProxyHandler)
        output = []
        handler._send_json = lambda code, body: output.append((code, body))
        calls = []
        cd = SimpleNamespace(snapshot=lambda: {'fake': 1}, clear_all=lambda: calls.append('clear'))
        handler.server = SimpleNamespace(config_store=store, cooldown_store=cd)
        handler.headers = {'X-Proxy-Admin-Token': 'WRONG'}
        handler.path = '/model_proxy/reload'
        self.target.write_text('{"broken":'); self.target.chmod(0o600)
        handler._dispatch_control('POST')
        self.assertEqual(output[-1][0], 401); self.assertEqual(calls, [])
        handler.headers = {'X-Proxy-Admin-Token': 'FAKE_ADMIN'}
        handler._dispatch_control('POST')
        self.assertEqual(output[-1][1]['error'], 'config_parse_error')
        self.assertEqual(calls, ['clear'])
        self.assertNotIn(str(self.root), repr(output))

    def test_guard_binds_initial_chain_and_store(self):
        path, store, listen, legacy, guard = self.guard()
        self.link.unlink(); self.link.symlink_to('real/business.json')
        with patch.object(server, '_RUNTIME_PATHS_FILE', self.paths), \
             patch.object(server, '_capture_migration_ledger', return_value='fake-ledger'):
            with self.assertRaises(PreflightError):
                server._capture_migration_guard(path, store, listen, legacy, guard[4])

    def test_verification_before_ready_detects_change_during_listen_read(self):
        path, store, listen, legacy, guard = self.guard()
        ids = {key: SimpleNamespace(canonical=key, requested=key, final_was_link=False)
               for key in (*server.PATH_KEYS, 'state_dir', 'anchor.lock')}
        legacy.snapshot = SimpleNamespace(as_dict=lambda: ids)
        def drift(*_):
            self.link.unlink(); self.link.symlink_to('real/business.json')
            return listen
        with patch.object(server, '_RUNTIME_PATHS_FILE', self.paths), \
             patch.object(server, '_runtime_paths', {}), \
             patch.object(server, 'resolve_listen', side_effect=drift), \
             patch.object(server, 'resolve_runtime_paths', return_value={}), \
             patch.object(server, 'verify_held_instance_locks'), \
             patch.object(server, 'inspect_runtime_identities', return_value=legacy.snapshot), \
             patch.object(server, '_check_config_targets'), \
             patch.object(server, 'read_instance_set', return_value=None), \
             patch.object(server, '_capture_migration_ledger', return_value='fake-ledger'):
            with self.assertRaises(PreflightError):
                server._verify_migration_guard(guard, path, store, listen, None, legacy, None)

    def test_relative_config_is_bound_to_startup_cwd(self):
        old_cwd = os.getcwd()
        try:
            os.chdir(self.root)
            store = server.ConfigStore('entry.json')
            os.chdir(ROOT)
            self.assertTrue(store._path.endswith('/entry.json'))
            self.write(config('B'))
            self.assertTrue(store.maybe_reload())
            self.assertEqual(store._config['generation'], 'B')
        finally:
            os.chdir(old_cwd)

    def test_guard_failure_prevents_binding(self):
        path, store, listen, legacy, guard = self.guard()
        ids = {key: SimpleNamespace(canonical=str(self.root / key)) for key in server.PATH_KEYS}
        ids['state_dir'] = SimpleNamespace(canonical=str(self.root))
        legacy.snapshot = SimpleNamespace(as_dict=lambda: ids)
        with patch.object(server, 'ConfigStore', return_value=store), \
             patch.object(server, 'resolve_listen', return_value=listen), \
             patch.object(server, 'resolve_runtime_paths', return_value={}), \
             patch.object(server, '_runtime_paths', {}), \
             patch.object(server, 'inspect_runtime_identities', return_value=legacy.snapshot), \
             patch.object(server, '_check_config_targets'), \
             patch.object(server, 'read_instance_set', return_value=None), \
             patch.object(server, '_lock_identities', return_value={}), \
             patch.object(server, '_capture_migration_guard', return_value=guard), \
             patch.object(server, '_verify_migration_guard', side_effect=PreflightError('changed', 'changed')), \
             patch.object(server, 'ProxyHTTPServer') as binder:
            with self.assertRaises(PreflightError):
                server._run_server(path, listen, None, legacy, initial_private=guard[4])
            binder.assert_not_called()

    def test_post_ready_change_never_starts_serving_and_sidecar_uses_source_parent(self):
        path, store, listen, legacy, guard = self.guard()
        record = {'state': 'STARTING'}
        expected = {'active': record, 'install': 'fake', 'last_good_paths': 'fake'}
        ready_record = dict(record, state='READY')
        ready_records = dict(expected, active=ready_record)
        ready = {'serving_started': False}
        attempted = {'active.json': [record]}
        fake_server = SimpleNamespace(serve_forever=lambda: self.fail('must not serve'))
        checks = []
        def verify(*args, **kwargs):
            checks.append(args[6])
            if args[6] == ready_records:
                raise PreflightError('startup_config_changed', 'changed')
        with patch.object(server, '_runtime_paths', {'log': self.root/'fake.log', 'totals': self.root/'totals'}), \
             patch.object(server, 'usage_totals', None), \
             patch.object(server, '_VERSION_FILE', self.root/'missing-version'), \
             patch.object(server, '_verify_migration_guard', side_effect=verify), \
             patch.object(server, 'init_logging'), \
             patch.object(server.UsageTotalsStore, 'from_verified_snapshot', return_value=object()), \
             patch.object(server, 'SessionOverridesSidecar', return_value=object()) as sidecar, \
             patch.object(server, 'write_instance_record'), \
             patch.object(server, 'read_instance_set', return_value=ready_records):
            with self.assertRaises(PreflightError):
                server._run_bound_server(fake_server, path, listen, store, self.root, record,
                                         ready, attempted, None, legacy, guard, expected, None)
        self.assertFalse(ready['serving_started'])
        self.assertEqual(checks[-1], ready_records)
        sidecar.assert_called_once_with(self.link.parent / 'session_overrides.json')
        self.assertNotEqual(self.link.parent, self.target.parent)

    def test_server_and_snapshot_use_backend_default_and_accept_none_evidence(self):
        # Interface test only: the fake backend models max_bytes=None. Native
        # unlimited reads and verification require the real backend update.
        marker = SimpleNamespace(mtime_ns=1_000_000_000)
        evidence = SimpleNamespace(source_path=str(self.link), target_path=str(self.target),
                                   data=json.dumps(config()).encode('utf-8'),
                                   marker=marker, max_bytes=None)
        with patch('core.mac_config_path.capture_config', return_value=evidence) as capture, \
             patch('core.mac_config_path.verify_config', return_value=evidence) as verify:
            store = server.ConfigStore(self.link)
            self.assertEqual(store._config['generation'], 'A')
            self.assertIsNone(store._business_snapshot.evidence.max_bytes)
            self.assertFalse(store.maybe_reload())
            initial = server._mac_private_config_snapshot(str(self.link))
            server._check_mac_private_business(initial, store._business_snapshot)
            server._verify_business_snapshot(store._business_snapshot)
        self.assertEqual(capture.call_count, 3)
        for call in capture.call_args_list:
            self.assertEqual(call.kwargs, {})
            self.assertEqual(os.fspath(call.args[0]), str(self.link))
        self.assertGreaterEqual(verify.call_count, 3)
        for call in verify.call_args_list:
            self.assertIs(call.args[0], evidence)

    def test_large_ordinary_config_initial_guard_and_atomic_reload(self):
        # This used to be legal and must not acquire a new fixed size limit.
        large = config('LARGE_A')
        large['padding'] = 'x' * (2 * 1024 * 1024 + 1)
        self.write(large)
        self.assertGreater(self.target.stat().st_size, 2 * 1024 * 1024)
        path, store, listen, legacy, guard = self.guard(self.target)
        self.assertEqual(store._config['generation'], 'LARGE_A')
        self.assertEqual(len(store._config['padding']), len(large['padding']))
        self.assertEqual(guard[0].evidence.data, self.target.read_bytes())
        self.assertIsNone(guard[0].evidence.max_bytes)
        replacement = self.real / 'replacement.json'
        large['generation'] = 'LARGE_B'
        replacement.write_text(json.dumps(large), encoding='utf-8')
        replacement.chmod(0o600)
        os.replace(replacement, self.target)
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config['generation'], 'LARGE_B')
        self.assertEqual(len(store._config['padding']), len(large['padding']))
        large['generation'] = 'LARGE_C'
        self.write(large)
        self.assertTrue(store.reload())
        self.assertEqual(store._config['generation'], 'LARGE_C')
        self.assertIsNone(store._business_snapshot.evidence.max_bytes)
        with patch.object(server, '_RUNTIME_PATHS_FILE', self.paths), \
             patch.object(server, '_capture_migration_ledger', return_value='fake-ledger'):
            with self.assertRaises(PreflightError):
                server._capture_migration_guard(path, store, listen, legacy, guard[4])

    def test_explicit_backend_limit_is_still_enforced(self):
        from core.mac_config_path import capture_config
        data = self.target.read_bytes()
        limited = capture_config(self.target, max_bytes=len(data))
        self.assertEqual(limited.data, data)
        self.assertEqual(limited.max_bytes, len(data))
        with self.assertRaises(MacConfigError) as caught:
            capture_config(self.target, max_bytes=len(data) - 1)
        self.assertEqual(caught.exception.code, 'config_too_large')

    def test_acl_unknown_is_not_mislabeled_as_read_or_race(self):
        from core.secure_files import SecureSnapshotError
        store = server.ConfigStore(self.link)
        old, marker = store._config, store._probe_marker
        self.write(config('B'))
        handler = server.ModelProxyHandler.__new__(server.ModelProxyHandler)
        sent, cleared = [], []
        handler._send_json = lambda status, body: sent.append((status, body))
        cooldown = SimpleNamespace(snapshot=lambda: {'fake': 1}, clear_all=lambda: cleared.append(True))
        with patch('core.mac_config_path._mac_fd_protection',
                   side_effect=SecureSnapshotError('platform_acl_unverified')):
            handler._handle_reload(store, cooldown)
        self.assertEqual(sent[0][1]['error'], 'filesystem_unverified')
        self.assertFalse(sent[0][1]['loaded'])
        self.assertTrue(sent[0][1]['cooldown_cleared'])
        self.assertEqual(cleared, [True])
        self.assertIs(store._config, old)
        self.assertEqual(store._probe_marker, marker)
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config['generation'], 'B')
        self.assertEqual(cleared, [True])

    def test_observable_link_swap_during_manual_reload_is_unstable_and_clears(self):
        from core import mac_config_path
        store = server.ConfigStore(self.link)
        old, marker = store._config, store._probe_marker
        self.write(config('B'))
        handler = server.ModelProxyHandler.__new__(server.ModelProxyHandler)
        sent, cleared = [], []
        handler._send_json = lambda status, body: sent.append((status, body))
        cooldown = SimpleNamespace(snapshot=lambda: {'fake': 1}, clear_all=lambda: cleared.append(True))
        original = mac_config_path.verify_config
        def replace_chain_then_verify(snapshot):
            self.link.unlink()
            self.link.symlink_to('real/business.json')
            return original(snapshot)
        with patch.object(mac_config_path, 'verify_config', side_effect=replace_chain_then_verify):
            handler._handle_reload(store, cooldown)
        self.assertEqual(sent[0][1]['error'], 'config_unstable')
        self.assertFalse(sent[0][1]['loaded'])
        self.assertTrue(sent[0][1]['cooldown_cleared'])
        self.assertEqual(cleared, [True])
        self.assertIs(store._config, old)
        self.assertEqual(store._probe_marker, marker)
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config['generation'], 'B')
        self.assertEqual(cleared, [True])

    def test_failure_codes_are_fixed_redacted_and_cover_all_mac_adapters(self):
        from core.config_snapshot import (mac_business_marker, verify_mac_business_config,
                                          ConfigSnapshot)
        actual = read_mac_business_config(self.link)
        for lower, expected in (
            ('config_read_error', 'config_read_error'),
            ('file_not_found', 'config_read_error'),
            ('permission_denied', 'config_read_error'),
            ('file_permissions_unsafe', 'config_read_error'),
            ('config_unstable', 'config_unstable'),
            ('target_changed', 'config_unstable'),
            ('link_changed', 'config_unstable'),
            ('filesystem_unverified', 'filesystem_unverified'),
            ('link_acl_unavailable', 'filesystem_unverified'),
            ('FAKE_SECRET_UNKNOWN', 'filesystem_unverified'),
        ):
            with self.subTest(lower=lower):
                with patch('core.mac_config_path.capture_config', side_effect=MacConfigError(lower)):
                    for fn in (read_mac_business_config, mac_business_marker):
                        with self.assertRaises(ConfigSnapshotError) as caught:
                            fn(self.link)
                        self.assertEqual(caught.exception.code, expected)
                        self.assertNotIn('FAKE_SECRET', str(caught.exception))
                with patch('core.mac_config_path.verify_config', side_effect=MacConfigError(lower)):
                    with self.assertRaises(ConfigSnapshotError) as caught:
                        verify_mac_business_config(actual)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn('FAKE_SECRET', str(caught.exception))

    def test_startup_read_error_is_redacted_and_never_binds(self):
        stderr = io.StringIO()
        with patch.object(server, '_DEFAULT_CONFIG_PATH', str(self.link)), \
             patch('core.mac_config_path.capture_config',
                   side_effect=MacConfigError('permission_denied')), \
             patch.object(server, 'ProxyHTTPServer') as bind, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn('config_read_error', stderr.getvalue())
        self.assertNotIn(str(self.root), stderr.getvalue())
        self.assertNotIn('permission_denied', stderr.getvalue())
        bind.assert_not_called()

    def test_missing_and_bad_mode_are_read_errors_and_retry_preserves_memory(self):
        store = server.ConfigStore(self.link)
        old, marker = store._config, store._probe_marker
        self.link.unlink()
        outcome = store.reload_detailed()
        self.assertEqual(outcome.error, 'config_read_error')
        self.assertFalse(outcome.loaded)
        self.assertIs(store._config, old)
        self.assertEqual(store._probe_marker, marker)
        self.link.symlink_to(self.target)
        for mode in (0o000, 0o644):
            with self.subTest(mode=oct(mode)):
                self.target.chmod(mode)
                outcome = store.reload_detailed()
                self.assertEqual(outcome.error, 'config_read_error')
                self.assertIs(store._config, old)
                self.assertEqual(store._probe_marker, marker)
        self.target.chmod(0o600)
        self.write(config('B'))
        self.assertTrue(store.reload())
        self.assertEqual(store._config['generation'], 'B')

    def test_duplicate_business_keys_keep_existing_last_value(self):
        self.target.write_text('{"generation":"A","generation":"B"}')
        self.target.chmod(0o600)
        self.assertEqual(read_mac_business_config(self.link).config['generation'], 'B')


if __name__ == '__main__':
    unittest.main(verbosity=2)
