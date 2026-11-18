"""Server sidecar guard integration: fake data only, no real service/config."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from core import server
from core.commands import SidecarWriteError
from core.sidecar_secure_io import SidecarSecureIO


@unittest.skipUnless(sys.platform == 'darwin' and os.name == 'posix', 'native macOS fixture')
class RealMacSidecarIntegration(unittest.TestCase):
    def setUp(self):
        from tests.test_ledger_secure_io import RealMacLedgerTests
        self.fixture = RealMacLedgerTests('test_real_guard_and_real_mac_commit')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config = self.fixture.config
        self.anchor, self.legacy = self.fixture.anchor, self.fixture.legacy
        self.env = patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def build(self, config=None):
        return server._sidecar_runtime_store(config or self.config, self.anchor, self.legacy)

    def test_corrupt_sidecar_falls_back_and_is_not_overwritten(self):
        path = self.config.parent / 'session_overrides.json'
        path.write_bytes(b'{invalid')
        os.chmod(path, 0o600)
        before = path.read_bytes()
        store = self.build()
        self.assertTrue(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_sidecar_ordinary_request_uses_existing_route_fallback(self):
        from tests import test_route_command as route_fixtures
        from core import session_identity, claudian_titles
        sidecar_path = self.config.parent / 'session_overrides.json'
        sidecar_path.write_bytes(b'{invalid')
        os.chmod(sidecar_path, 0o600)
        store = self.build()
        fake_sessions = self.root / 'fake-sessions'
        fake_sessions.mkdir(mode=0o700)
        cfg = route_fixtures.TestForwardInterceptEndToEnd()._write_cfg(str(self.root))
        with patch.object(server, '_mac_business_enabled', return_value=False), \
             patch.object(session_identity, 'DEFAULT_SESSIONS_DIR', fake_sessions), \
             patch.object(claudian_titles, 'DEFAULT_CLAUDIAN_SESSIONS_DIR', fake_sessions):
            handler = route_fixtures._make_forward_handler(cfg, sidecar_path)
            handler.server.sidecar_store = store
            acc, _ = route_fixtures._send(handler, 'ordinary request', 'fake-session')
            self.assertEqual(acc['builtin'], '')
            self.assertEqual(acc['route'], 'claude')
            self.assertNotEqual(acc['status'], 0)  # no fake supplies, not a hard sidecar failure
            command, _ = route_fixtures._send(handler, '$route nation', 'fake-session')
            self.assertEqual(command['builtin'], 'route')
        self.assertEqual(sidecar_path.read_bytes(), b'{invalid')
        self.assertTrue(store._unavailable)

    def test_approved_config_final_symlink_keeps_original_parent(self):
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        actual_config = other / 'business.json'
        actual_config.write_text('{}')
        os.chmod(actual_config, 0o600)
        self.config.unlink()
        self.config.symlink_to(actual_config)
        store = self.build()
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertTrue(self.config.is_symlink())
        self.assertTrue((self.config.parent / 'session_overrides.json').is_file())
        self.assertFalse((other / 'session_overrides.json').exists())
        self.assertEqual(actual_config.read_text(), '{}')

    def test_approved_config_directory_symlink_inside_project(self):
        directory = self.root / 'linked-config'
        directory.symlink_to(self.config.parent, target_is_directory=True)
        config = directory / self.config.name
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(config)}):
            store = self.build(config)
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertTrue(directory.is_symlink())
        path = self.config.parent / 'session_overrides.json'
        self.assertEqual(json.loads(path.read_text())['fake-client']['fake-session']['route_id'], 'route-b')

    def test_sidecar_final_symlink_does_not_authorize_other_project_file(self):
        other = self.root / 'unrelated.json'
        other.write_text('{"sentinel": {}}')
        os.chmod(other, 0o600)
        before = other.read_bytes()
        path = self.config.parent / 'session_overrides.json'
        path.symlink_to(other)
        store = self.build()
        self.assertTrue(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertTrue(path.is_symlink())
        self.assertEqual(other.read_bytes(), before)

    def test_sidecar_hardlink_is_not_read_or_overwritten(self):
        other = self.root / 'unrelated.json'
        other.write_text('{"sentinel": {}}')
        os.chmod(other, 0o600)
        path = self.config.parent / 'session_overrides.json'
        os.link(other, path)
        before = other.read_bytes()
        store = self.build()
        self.assertTrue(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(other.read_bytes(), before)
        self.assertEqual(path.stat().st_ino, other.stat().st_ino)

    def test_closed_real_instance_lock_blocks_sidecar_writes(self):
        store = self.build()
        self.fixture.legacy.close()
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse((self.config.parent / 'session_overrides.json').exists())

    def test_config_symlink_dotdot_uses_original_parent_semantics(self):
        nested = self.root / 'nested'
        nested.mkdir(mode=0o700)
        deeper = nested / 'deeper'
        deeper.mkdir(mode=0o700)
        config = nested / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        link = self.root / 'alias'
        link.symlink_to(deeper, target_is_directory=True)
        source = str(link) + '/../business.json'
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': source}):
            store = self.build(source)
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertTrue(link.is_symlink())
        self.assertTrue((nested / 'session_overrides.json').is_file())
        self.assertFalse((self.root / 'session_overrides.json').exists())

    def test_explicit_external_config_direct_sidecar_same_volume(self):
        external = self.root.parent / 'fake-external-config'
        external.mkdir(mode=0o700)
        config = external / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(config)}):
            store = self.build(config)
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = external / 'session_overrides.json'
        self.assertEqual(json.loads(path.read_text())['fake-client']['fake-session']['route_id'], 'route-b')
        self.assertEqual(self.root.stat().st_dev, external.stat().st_dev)

    def test_external_cross_volume_branch_uses_authorized_target_parent(self):
        from core import secure_files
        external = self.root.parent / 'fake-external-config'
        external.mkdir(mode=0o700)
        config = external / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(config)}):
            store = self.build(config)
        # Branch injection only; no actual second filesystem is claimed.
        with patch.object(secure_files, '_same_volume', return_value=False):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = external / 'session_overrides.json'
        self.assertEqual(json.loads(path.read_text())['fake-client']['fake-session']['route_id'], 'route-b')
        self.assertEqual(sorted(x.name for x in external.iterdir()), ['business.json', 'session_overrides.json'])

    def test_wrong_installation_locks_use_fail_closed_adapter(self):
        from core.sidecar_runtime_guard import UnavailableSidecarIO
        wrong_root = self.root.parent / 'wrong-project'
        wrong_root.mkdir(mode=0o700)
        with patch.object(server, '_PACKAGE_DIR', wrong_root):
            store = self.build()
        self.assertIsInstance(store._secure_io, UnavailableSidecarIO)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse((self.config.parent / 'session_overrides.json').exists())

    def test_explicit_external_config_directory_symlink_binds_derived_target(self):
        external = self.root.parent / 'fake-external-real'
        external.mkdir(mode=0o700)
        config = external / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        alias = self.root.parent / 'fake-external-alias'
        alias.symlink_to(external, target_is_directory=True)
        source = alias / 'business.json'
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(source)}):
            store = self.build(source)
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertTrue(alias.is_symlink())
        path = external / 'session_overrides.json'
        self.assertEqual(json.loads(path.read_text())['fake-client']['fake-session']['route_id'], 'route-b')

    def test_external_config_not_explicitly_selected_cannot_write(self):
        external = self.root.parent / 'fake-external-config'
        external.mkdir(mode=0o700)
        config = external / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(self.config)}):
            store = self.build(config)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse((external / 'session_overrides.json').exists())

    def test_unselected_external_alias_into_project_does_not_gain_authority(self):
        alias = self.root.parent / 'unselected-external-alias'
        alias.symlink_to(self.config.parent, target_is_directory=True)
        source = alias / self.config.name
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(self.config)}):
            store = self.build(source)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse((self.config.parent / 'session_overrides.json').exists())

    def test_external_final_link_does_not_authorize_different_target(self):
        external = self.root.parent / 'fake-external-config'
        external.mkdir(mode=0o700)
        config = external / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        target = external / 'other.json'
        target.write_text('{"sentinel": {}}')
        os.chmod(target, 0o600)
        sidecar = external / 'session_overrides.json'
        sidecar.symlink_to(target)
        before = target.read_bytes()
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(config)}):
            store = self.build(config)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(sidecar.is_symlink())

    def test_internal_config_directory_link_to_external_is_not_external_write_grant(self):
        external = self.root.parent / 'fake-external-target'
        external.mkdir(mode=0o700)
        config = external / 'business.json'
        config.write_text('{}')
        os.chmod(config, 0o600)
        link = self.root / 'external-link'
        link.symlink_to(external, target_is_directory=True)
        source = link / 'business.json'
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(source)}):
            store = self.build(source)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse((external / 'session_overrides.json').exists())

    def test_config_directory_link_retarget_preserves_verified_b(self):
        left, right = self.root / 'left-config', self.root / 'right-config'
        left.mkdir(mode=0o700)
        right.mkdir(mode=0o700)
        for folder in (left, right):
            (folder / 'business.json').write_text('{}')
            os.chmod(folder / 'business.json', 0o600)
        link = self.root / 'config-link'
        link.symlink_to(left, target_is_directory=True)
        source = link / 'business.json'
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(source)}):
            store = self.build(source)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        before = (left / 'session_overrides.json').read_bytes()
        link.unlink()
        link.symlink_to(right, target_is_directory=True)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        self.assertEqual((left / 'session_overrides.json').read_bytes(), before)
        self.assertFalse((right / 'session_overrides.json').exists())

    def test_staging_creation_unknown_latches_sidecar(self):
        original = os.mkdir
        def create_then_fail(path, mode=0o777, *, dir_fd=None):
            original(path, mode, dir_fd=dir_fd)
            raise OSError('fake creation result unknown')
        with patch.object(os, 'mkdir', side_effect=create_then_fail):
            store = self.build()
        self.assertTrue((self.root / '.runtime' / 'tmp').is_dir())
        self.assertTrue(store._resource_unknown)
        with self.assertRaises(SidecarWriteError) as caught:
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertFalse((self.config.parent / 'session_overrides.json').exists())

    def test_unknown_after_real_commit_keeps_b_until_verified_recovery(self):
        from core import secure_files
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        original = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = original(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        store.maybe_reload()
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-c'})

    def test_unknown_before_replace_recovers_only_verified_b(self):
        from core import secure_files
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = self.config.parent / 'session_overrides.json'
        before = path.read_bytes()
        error = secure_files.SecureWriteError('interrupted', state='UNKNOWN')
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=error):
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertTrue(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_then_third_file_is_not_published_or_overwritten(self):
        from core import secure_files
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        original = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = original(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            with self.assertRaises(SidecarWriteError):
                store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        path = self.config.parent / 'session_overrides.json'
        replacement = self.config.parent / 'fake-third.json'
        replacement.write_text('{"fake-client":{"fake-session":{"route_id":"third"}}}')
        os.chmod(replacement, 0o600)
        os.replace(replacement, path)
        before = path.read_bytes()
        self.assertFalse(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-d')
        self.assertEqual(path.read_bytes(), before)

    def test_external_staging_namespace_conflict_is_not_authorized(self):
        external = self.root.parent / 'fake-external-config'
        external.mkdir(mode=0o700)
        config = external / ('.model-proxy-runtime-' + 'a' * 24)
        config.write_text('{}')
        os.chmod(config, 0o600)
        before = config.read_bytes()
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': str(config)}):
            store = self.build(config)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((external / 'session_overrides.json').exists())

    def test_resource_unknown_after_commit_remains_isolated(self):
        from core import secure_files
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        original = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = original(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate, close_unknown=True)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(store.recover())
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-d')

    def test_adapter_construction_failure_uses_fail_closed_placeholder(self):
        from core.sidecar_runtime_guard import UnavailableSidecarIO
        with patch('core.sidecar_secure_io.SidecarSecureIO', side_effect=RuntimeError('fake sensitive')):
            store = self.build()
        self.assertIsInstance(store._secure_io, UnavailableSidecarIO)
        self.assertTrue(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse((self.config.parent / 'session_overrides.json').exists())

    def test_runtime_role_drift_blocks_writes_without_losing_verified_b(self):
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        sidecar = self.config.parent / 'session_overrides.json'
        before = sidecar.read_bytes()
        paths = dict(self.fixture.paths)
        paths['pid'] = sidecar
        self.fixture.path_config.write_text(json.dumps({key: str(value) for key, value in paths.items()}))
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        self.assertEqual(sidecar.read_bytes(), before)

    def test_runtime_artifact_guard_checks_exact_name_and_roles(self):
        store = self.build()
        guard = store._secure_io.guard
        original = str(self.config.parent / 'session_overrides.json')
        stage = str(self.root / '.runtime' / 'tmp')
        artifact = str(Path(stage) / ('.model-proxy-runtime-' + 'a' * 24))
        self.assertTrue(guard(original, original, stage, artifact))
        with self.assertRaises(Exception):
            guard(original, original, stage, str(Path(stage) / 'unsafe-name'))

    def test_missing_staging_is_created_by_real_guard(self):
        self.assertFalse((self.root / '.runtime' / 'tmp').exists())
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertTrue((self.root / '.runtime' / 'tmp').is_dir())
        self.assertEqual((self.root / '.runtime' / 'tmp').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])

    def test_bound_server_injects_real_guarded_adapter_before_serving(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        record = {'state': 'STARTING'}
        expected = {'active': record, 'install': 'fake', 'last_good_paths': 'fake'}
        ready_records = dict(expected, active=dict(record, state='READY'))
        proxy = SimpleNamespace(serve_forever=Mock(), shutdown=Mock())
        listen = {'host': '127.0.0.1', 'port': 0, 'host_check_enabled': False, 'allow_hosts': []}
        ready, attempted = {'serving_started': False}, {'active.json': [record]}
        migration = (None, None, None, self.fixture.snapshot, None)
        with patch.object(server, '_VERSION_FILE', self.root / 'missing-version'), \
             patch.object(server, 'usage_totals', None), \
             patch.object(server, '_verify_migration_guard'), \
             patch.object(server, 'init_logging'), \
             patch.object(server, 'write_instance_record'), \
             patch.object(server, 'read_instance_set', return_value=ready_records):
            server._run_bound_server(proxy, self.config, listen, object(), self.fixture.state,
                                     record, ready, attempted, self.anchor, self.legacy,
                                     migration, expected, None)
        self.assertTrue(ready['serving_started'])
        proxy.serve_forever.assert_called_once_with()
        proxy.shutdown.assert_called_once_with()
        self.assertIsInstance(proxy.sidecar_store._secure_io, SidecarSecureIO)
        self.assertFalse(proxy.sidecar_store._unavailable)
        proxy.sidecar_store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = self.config.parent / 'session_overrides.json'
        self.assertEqual(json.loads(path.read_text())['fake-client']['fake-session']['route_id'], 'route-b')

    def test_default_real_guard_reads_and_writes(self):
        store = self.build()
        self.assertIsInstance(store._secure_io, SidecarSecureIO)
        self.assertFalse(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = self.config.parent / 'session_overrides.json'
        self.assertEqual(json.loads(path.read_text())['fake-client']['fake-session']['route_id'], 'route-b')
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])


class SyntheticWindowsSidecarIntegration(unittest.TestCase):
    """Native writer/guard orchestration on fake Win32; held-lock verification mocked."""
    def setUp(self):
        from pathlib import PureWindowsPath
        from tests.test_ledger_secure_io import SyntheticWindowsLedgerTests
        from core import commands, sidecar_secure_io
        self.fixture = SyntheticWindowsLedgerTests('test_real_windows_adapter_with_fake_native_backend')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.stack = []
        for item in (patch.object(commands, 'Path', PureWindowsPath),
                     patch.object(sidecar_secure_io, 'Path', PureWindowsPath),
                     patch.dict(os.environ, {'MODEL_PROXY_CONFIG': self.fixture.CONFIG})):
            item.start()
            self.addCleanup(item.stop)
        self.config = PureWindowsPath(self.fixture.CONFIG)

    def build(self, config=None):
        return server._sidecar_runtime_store(config or self.config, object(), self.fixture.legacy)

    def test_windows_explicit_external_same_volume_fake_backend(self):
        from pathlib import PureWindowsPath
        config = r'C:\fixture\runtime\business.json'
        self.fixture.fs.objects[config] = dict(self.fixture.fs.obj(901), data=b'{}')
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': config}):
            store = self.build(PureWindowsPath(config))
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        target = r'C:\fixture\runtime\session_overrides.json'
        self.assertEqual(json.loads(self.fixture.fs.objects[target]['data'])['fake-client']['fake-session']['route_id'], 'route-b')
        self.assertTrue(self.fixture.fs.commits[-1][2].startswith(self.fixture.STAGE + '\\'))
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_explicit_external_cross_volume_fake_backend(self):
        from pathlib import PureWindowsPath
        config = r'D:\backups\business.json'
        self.fixture.fs.objects[config] = dict(self.fixture.fs.obj(902), volume=18, data=b'{}')
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': config}):
            store = self.build(PureWindowsPath(config))
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        target = r'D:\backups\session_overrides.json'
        self.assertEqual(json.loads(self.fixture.fs.objects[target]['data'])['fake-client']['fake-session']['route_id'], 'route-b')
        self.assertTrue(self.fixture.fs.commits[-1][2].startswith(r'D:\backups\.model-proxy-stage-'))
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_unselected_external_config_cannot_write(self):
        from pathlib import PureWindowsPath
        config = r'D:\backups\business.json'
        self.fixture.fs.objects[config] = dict(self.fixture.fs.obj(903), volume=18, data=b'{}')
        store = self.build(PureWindowsPath(config))
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertNotIn(r'D:\backups\session_overrides.json', self.fixture.fs.objects)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_wrong_installation_cannot_supply_guard(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from core.sidecar_runtime_guard import UnavailableSidecarIO
        wrong = SimpleNamespace(snapshot=replace(self.fixture.legacy.snapshot, project_root=r'C:\other'))
        store = server._sidecar_runtime_store(self.config, object(), wrong)
        self.assertIsInstance(store._secure_io, UnavailableSidecarIO)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse(self.fixture.fs.commits)

    def test_windows_missing_staging_is_created_by_real_guard_fake_native(self):
        del self.fixture.fs.objects[self.fixture.STAGE]
        def mkdir(path, *, user_sid):
            if path in self.fixture.fs.objects:
                raise FileExistsError('fake')
            self.fixture.fs.objects[path] = self.fixture.fs.obj(907, directory=True)
        self.fixture.fs.create_private_directory = mkdir
        store = self.build()
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertIn(self.fixture.STAGE, self.fixture.fs.objects)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_sidecar_hardlink_is_not_loaded_or_overwritten(self):
        path = self.fixture.CONFIG_DIR + r'\session_overrides.json'
        data = b'{"fake-client":{"fake-session":{"route_id":"untrusted"}}}'
        self.fixture.fs.objects[path] = dict(self.fixture.fs.obj(908), links=2, data=data)
        store = self.build()
        self.assertTrue(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(self.fixture.fs.objects[path]['data'], data)
        self.assertFalse(self.fixture.fs.commits)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_corrupt_sidecar_falls_back_without_write(self):
        path = self.fixture.CONFIG_DIR + r'\session_overrides.json'
        self.fixture.fs.objects[path] = dict(self.fixture.fs.obj(905), data=b'{bad')
        store = self.build()
        self.assertTrue(store._unavailable)
        self.assertEqual(store.get_overrides_for('fake-client'), {})
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(self.fixture.fs.objects[path]['data'], b'{bad')
        self.assertFalse(self.fixture.fs.commits)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_held_lock_identity_drift_is_rejected(self):
        from dataclasses import replace
        from types import SimpleNamespace
        snapshot = self.fixture.legacy.snapshot
        identities = tuple((key, replace(value, canonical=value.canonical + '-other') if key == 'lock' else value)
                           for key, value in snapshot.identities)
        wrong = SimpleNamespace(snapshot=replace(snapshot, identities=identities))
        store = server._sidecar_runtime_store(self.config, object(), wrong)
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertFalse(self.fixture.fs.commits)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_preflight_handle_release_unknown_latches_store(self):
        from core import discovery
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = self.fixture.CONFIG_DIR + r'\session_overrides.json'
        before = self.fixture.fs.objects[path]['data']
        real_preflight = discovery._windows_open_preflight
        calls, failed = 0, []
        def open_preflight(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                root_probes = 0
                def fail_probe(typ, probe_path, count):
                    nonlocal root_probes
                    if typ == 'probe' and probe_path == 'C:\\':
                        root_probes += 1
                        if root_probes == 2:
                            handle = self.fixture.fs.next_handle + 1
                            failed.append(handle)
                            self.fixture.fs.close_fail.add(handle)
                            self.fixture.fs.on_open = None
                self.fixture.fs.on_open = fail_probe
            return real_preflight(*args, **kwargs)
        def cleanup_fake_handles():
            self.fixture.fs.on_open = None
            self.fixture.fs.close_fail.clear()
            for handle in list(self.fixture.fs.handles):
                self.fixture.fs.close(handle)
        self.addCleanup(cleanup_fake_handles)
        with patch.object(discovery, '_windows_open_preflight', side_effect=open_preflight):
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
            self.assertEqual(caught.exception.state, 'UNKNOWN')
            self.assertTrue(caught.exception.release_unknown)
            self.assertTrue(failed)
            self.assertIn(failed[0], self.fixture.fs.handles)
            self.assertFalse(store.recover())
            with self.assertRaises(SidecarWriteError):
                store.apply_command('fake-client', 'fake-session', 'set', 'route-d')
            self.assertEqual(calls, 2)
        self.assertEqual(self.fixture.fs.objects[path]['data'], before)
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})

    def test_windows_unknown_commit_never_publishes_unverified_c(self):
        store = self.build()
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        real = self.fixture.fs.replace_existing
        def replace_then_fail(target, stage):
            real(target, stage)
            raise OSError('fake commit outcome')
        with patch.object(self.fixture.fs, 'replace_existing', side_effect=replace_then_fail):
            with self.assertRaises(SidecarWriteError) as caught:
                store.apply_command('fake-client', 'fake-session', 'set', 'route-c')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        store.maybe_reload()
        self.assertEqual(store.get_overrides_for('fake-client'), {'fake-session': 'route-b'})
        self.assertFalse(store.recover())
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_external_staging_namespace_role_collision_is_rejected(self):
        from pathlib import PureWindowsPath
        config = r'D:\backups\business.json'
        self.fixture.fs.objects[config] = dict(self.fixture.fs.obj(906), volume=18, data=b'{}')
        self.fixture.paths['ensure_log'] = PureWindowsPath(r'D:\backups\.model-proxy-stage-' + 'a' * 24)
        with patch.dict(os.environ, {'MODEL_PROXY_CONFIG': config}):
            store = self.build(PureWindowsPath(config))
        self.assertTrue(store._unavailable)
        with self.assertRaises(SidecarWriteError):
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertNotIn(r'D:\backups\session_overrides.json', self.fixture.fs.objects)
        self.assertFalse(self.fixture.fs.commits)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_staging_creation_unknown_latches_sidecar(self):
        from core import platform_windows
        failure = platform_windows.WindowsPrivateCreationError('release_unknown', created=None,
                                                               release_unknown=True)
        with patch.object(platform_windows, 'ensure_private_directory', side_effect=failure):
            store = self.build()
        self.assertTrue(store._resource_unknown)
        with self.assertRaises(SidecarWriteError) as caught:
            store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertFalse(self.fixture.fs.commits)
        self.assertFalse(self.fixture.fs.handles)

    def test_windows_default_real_guard_and_fake_platform_writer(self):
        store = self.build()
        self.assertIsInstance(store._secure_io, SidecarSecureIO)
        self.assertFalse(store._unavailable)
        store.apply_command('fake-client', 'fake-session', 'set', 'route-b')
        path = self.fixture.CONFIG_DIR + r'\session_overrides.json'
        self.assertEqual(json.loads(self.fixture.fs.objects[path]['data'])['fake-client']['fake-session']['route_id'], 'route-b')
        self.assertEqual(self.fixture.fs.commits[-1][0], 'move')
        self.assertFalse(self.fixture.fs.handles)



if __name__ == '__main__':
    unittest.main()
