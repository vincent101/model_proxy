"""Real macOS temporary-file ledger tests; no real service/configuration."""
import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core import server
from core.discovery import PATH_KEYS, inspect_runtime_identities
from core.platform_lock import acquire_lock, verify_local_access
from core.ledger_secure_io import MacLedgerSecureIO, LedgerIOError


ACC = {'status': 200, 'supply': 'fake', 'route': 'fake', 'strategy': 'fake'}


@unittest.skipUnless(sys.platform == 'darwin' and os.name == 'posix', 'requires native macOS')
class RealMacLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ledger-secure-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        self.root.mkdir(mode=0o700)
        self.state = self.root / '.runtime'
        self.state.mkdir(mode=0o700)
        self.config_dir = self.root / 'config'
        self.config_dir.mkdir(mode=0o700)
        self.paths = {name: self.state / (name + '.json') for name in PATH_KEYS}
        self.paths['lock'] = self.state / 'service.lock'
        self.paths['start_lock'] = self.state / 'starter.lock'
        self.config = self.config_dir / 'fake.json'
        self.config.write_text('{}', encoding='utf-8')
        os.chmod(self.config, 0o600)
        self.path_config = self.config_dir / 'runtime_paths.json'
        self.path_config.write_text(json.dumps({key: str(value) for key, value in self.paths.items()}))
        os.chmod(self.path_config, 0o600)
        self.ledger = self.paths['totals']
        data = server.UsageTotalsStore._empty_data()
        data['total']['requests'] = 100
        data['total']['ok'] = 100
        self.ledger.write_text(json.dumps(data), encoding='utf-8')
        os.chmod(self.ledger, 0o600)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, '_PACKAGE_DIR', self.root))
        self.stack.enter_context(patch.object(server, '_RUNTIME_PATHS_FILE', self.path_config))
        self.stack.enter_context(patch.object(server, '_runtime_paths', self.paths))
        snapshot = inspect_runtime_identities(self.root, self.paths)
        self.anchor = self.stack.enter_context(acquire_lock(snapshot, 'anchor.lock', create=True,
                                                            access_verifier=verify_local_access))
        self.legacy = self.stack.enter_context(acquire_lock(self.anchor.snapshot, 'lock', create=True,
                                                            access_verifier=verify_local_access,
                                                            anchor=self.anchor))
        self.snapshot = server._capture_migration_ledger(self.legacy.snapshot)
        self.adapter = server._ledger_runtime_adapter(self.config, self.anchor, self.legacy)
        self.assertIsInstance(self.adapter, MacLedgerSecureIO)
        self.store = server.UsageTotalsStore.from_verified_snapshot(self.ledger, self.snapshot,
                                                                     adapter=self.adapter)

    def test_real_guard_and_real_mac_commit(self):
        self.assertFalse((self.state / 'tmp').exists())
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 101)
        self.assertEqual((self.state / 'tmp').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list((self.state / 'tmp').iterdir()), [])
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 102)

    def test_real_unknown_c_then_reconcile_103(self):
        from core import secure_files
        real = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = real(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                 candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 101)
        with patch.object(self.adapter, 'observe', side_effect=LedgerIOError('unavailable')):
            self.store.record(ACC, 1)
        self.assertEqual(self.store._data['total']['requests'], 102)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 103)

    def test_real_not_replaced_pending_then_retry(self):
        from core import secure_files
        failure = secure_files.SecureWriteError('interrupted', state='NOT_REPLACED')
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=failure):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'PENDING')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 100)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 102)

    def _resource_unknown_after_commit(self, flag):
        from core import secure_files
        real = secure_files.secure_replace_runtime
        def committed_then_resource_unknown(*args, **kwargs):
            result = real(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate, **{flag: True})
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=committed_then_resource_unknown):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.store._data['total']['requests'], 102)
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 101)

    def test_postcommit_close_unknown_stays_quarantined(self):
        self._resource_unknown_after_commit('close_unknown')

    def test_postcommit_cleanup_unknown_stays_quarantined(self):
        self._resource_unknown_after_commit('cleanup_unknown')

    def test_capture_close_unknown_stays_quarantined(self):
        from core import secure_files
        failure = secure_files.SecureWriteError('fd_close_unknown', close_unknown=True)
        before = self.ledger.read_bytes()
        with patch.object(secure_files, 'capture_target', side_effect=failure):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_staging_creation_unknown_keeps_residual_and_pauses(self):
        original_mkdir = os.mkdir
        def create_then_fail(path, mode=0o777, *, dir_fd=None):
            original_mkdir(path, mode, dir_fd=dir_fd)
            raise OSError('fake unknown creation result')
        before = self.ledger.read_bytes()
        with patch.object(os, 'mkdir', side_effect=create_then_fail):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertTrue((self.state / 'tmp').is_dir())
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_staging_release_unknown_is_not_reported_as_pending(self):
        before = self.ledger.read_bytes()
        with patch.object(self.adapter.guard, 'prepare_staging',
                          side_effect=server.PreflightError('release_unknown', 'fake')):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_real_unknown_b_then_reconcile_103(self):
        from core import secure_files
        failure = secure_files.SecureWriteError('interrupted', state='UNKNOWN')
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=failure):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 100)
        with patch.object(self.adapter, 'observe', side_effect=LedgerIOError('unavailable')):
            self.store.record(ACC, 1)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 103)

    def test_third_real_file_is_not_overwritten(self):
        from core import secure_files
        real = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = real(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                 candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            self.store.record(ACC, 1)
        outsider = self.state / 'fake-external-write.json'
        third = copy.deepcopy(self.store._data)
        third['total']['requests'] = 500
        outsider.write_text(json.dumps(third), encoding='utf-8')
        os.chmod(outsider, 0o600)
        os.replace(outsider, self.ledger)
        before = self.ledger.read_bytes()
        self.store.record(ACC, 1)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.store._data['total']['requests'], 103)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_equal_content_different_identity_is_not_candidate_proof(self):
        from core import secure_files
        real = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = real(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                 candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            self.store.record(ACC, 1)
        content = self.ledger.read_bytes()
        replacement = self.state / 'fake-equal-content.json'
        replacement.write_bytes(content)
        os.chmod(replacement, 0o600)
        old_inode = self.ledger.stat().st_ino
        os.replace(replacement, self.ledger)
        self.assertNotEqual(self.ledger.stat().st_ino, old_inode)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), content)

    def test_real_verified_absence_creates_without_overwrite(self):
        self.legacy.close()
        self.anchor.close()
        self.ledger.unlink()
        snapshot = inspect_runtime_identities(self.root, self.paths)
        anchor = self.stack.enter_context(acquire_lock(snapshot, 'anchor.lock',
                                                       access_verifier=verify_local_access))
        legacy = self.stack.enter_context(acquire_lock(anchor.snapshot, 'lock',
                                                       access_verifier=verify_local_access,
                                                       anchor=anchor))
        absent = server._capture_migration_ledger(legacy.snapshot)
        self.assertIsNone(absent)
        adapter = server._ledger_runtime_adapter(self.config, anchor, legacy)
        store = server.UsageTotalsStore.from_verified_snapshot(self.ledger, absent, adapter=adapter)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 1)

    def test_real_restart_uses_verified_disk_baseline(self):
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        # A real restart takes a fresh runtime identity snapshot, not the old one.
        fresh = inspect_runtime_identities(self.root, self.paths)
        disk = server._capture_migration_ledger(fresh)
        restarted = server.UsageTotalsStore.from_verified_snapshot(self.ledger, disk, adapter=self.adapter)
        self.assertEqual(restarted._data['total']['requests'], 101)
        restarted.record(ACC, 1)
        self.assertEqual(restarted.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 102)

    def test_unprotected_ledger_is_not_written(self):
        before = self.ledger.read_bytes()
        os.chmod(self.ledger, 0o644)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o644)

    def test_mixed_platform_and_unbound_external_path_are_rejected(self):
        with patch.object(server.sys, 'platform', 'win32'):
            self.assertIsNone(server._ledger_runtime_adapter(self.config, self.anchor, self.legacy))
        external_paths = dict(self.paths)
        external_paths['totals'] = self.root.parent / 'external-ledger.json'
        with self.assertRaises(server.PreflightError):
            server._LedgerRuntimeGuard(self.config, self.anchor, self.legacy, external_paths)

    def test_corrupt_verified_file_is_unavailable_without_overwrite(self):
        self.ledger.write_text('{invalid', encoding='utf-8')
        before = self.ledger.read_bytes()
        snapshot = server._capture_migration_ledger(self.legacy.snapshot)
        self.assertIsInstance(snapshot, server.LedgerUnavailable)
        store = server.UsageTotalsStore.from_verified_snapshot(self.ledger, snapshot, adapter=self.adapter)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNAVAILABLE')
        self.assertEqual(store._data['total']['requests'], 1)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_changed_role_path_blocks_real_commit(self):
        before = self.ledger.read_bytes()
        changed = dict(self.paths)
        changed['log'] = changed['totals']
        self.path_config.write_text(json.dumps({key: str(value) for key, value in changed.items()}))
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)
        self.path_config.write_text(json.dumps({key: str(value) for key, value in self.paths.items()}))
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 102)

    def test_two_other_runtime_roles_alias_blocks_commit(self):
        before = self.ledger.read_bytes()
        self.paths['log'].write_text('fake', encoding='utf-8')
        os.chmod(self.paths['log'], 0o600)
        os.link(self.paths['log'], self.paths['pid'])
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_config_alias_into_staging_namespace_blocks_commit(self):
        before = self.ledger.read_bytes()
        self.adapter.guard.prepare_staging()
        other = self.state / 'tmp' / 'fake-config.json'
        other.write_text('{}', encoding='utf-8')
        os.chmod(other, 0o600)
        self.config.unlink()
        self.config.symlink_to(other)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_observation_repr_does_not_disclose_contents(self):
        value = self.adapter.observe(self.ledger)
        self.assertNotIn('requests', repr(value))
        self.assertNotIn('fake', repr(value))

    def test_ambiguous_guard_failure_remains_quarantined_after_restore(self):
        before = self.ledger.read_bytes()
        with patch.object(server, 'verify_held_instance_locks',
                          side_effect=server.PreflightError('protection_unavailable', 'fake')):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.store._data['total']['requests'], 102)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_closed_instance_lock_blocks_real_commit(self):
        before = self.ledger.read_bytes()
        self.legacy.close()
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_unprotected_runtime_parent_blocks_commit_without_repair(self):
        before = self.ledger.read_bytes()
        os.chmod(self.state, 0o777)
        try:
            self.store.record(ACC, 1)
            self.assertEqual(self.store.persistence_state, 'UNKNOWN')
            self.assertEqual(self.ledger.read_bytes(), before)
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o777)
        finally:
            os.chmod(self.state, 0o700)

    def test_target_hardlink_blocks_real_commit(self):
        link = self.state / 'fake-hardlink'
        os.link(self.ledger, link)
        before = self.ledger.read_bytes()
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.ledger.read_bytes(), before)

    def _retarget_external(self, *, reserved_role=False, inside_project=False):
        self.legacy.close()
        self.anchor.close()
        external = (self.root / 'fake-ledgers' if inside_project
                    else self.root.parent / 'fake-external')
        external.mkdir(mode=0o700)
        target = external / 'ledger.json'
        value = server.UsageTotalsStore._empty_data()
        value['total']['requests'] = 100
        value['total']['ok'] = 100
        target.write_text(json.dumps(value), encoding='utf-8')
        os.chmod(target, 0o600)
        self.paths['totals'] = target
        if reserved_role:
            self.paths['ensure_log'] = external / ('.model-proxy-runtime-' + 'a' * 24)
        self.path_config.write_text(json.dumps({key: str(value) for key, value in self.paths.items()}))
        snapshot = inspect_runtime_identities(self.root, self.paths)
        anchor = self.stack.enter_context(acquire_lock(snapshot, 'anchor.lock',
                                                       access_verifier=verify_local_access))
        legacy = self.stack.enter_context(acquire_lock(anchor.snapshot, 'lock',
                                                       access_verifier=verify_local_access,
                                                       anchor=anchor))
        captured = server._capture_migration_ledger(legacy.snapshot)
        adapter = server._ledger_runtime_adapter(self.config, anchor, legacy)
        self.assertIsInstance(adapter, MacLedgerSecureIO)
        store = server.UsageTotalsStore.from_verified_snapshot(target, captured, adapter=adapter)
        return target, adapter, store

    def test_internal_nonruntime_same_volume_real_mac_commit(self):
        target, adapter, store = self._retarget_external(inside_project=True)
        self.assertFalse(adapter.external)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(target.read_text())['total']['requests'], 101)
        self.assertEqual(list((self.state / 'tmp').iterdir()), [])

    def test_external_same_volume_real_mac_commit(self):
        target, adapter, store = self._retarget_external()
        self.assertEqual(target.parent.stat().st_dev, self.state.stat().st_dev)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(target.read_text())['total']['requests'], 101)
        self.assertEqual(list((self.state / 'tmp').iterdir()), [])
        self.assertEqual([x.name for x in target.parent.iterdir()], ['ledger.json'])

    def test_external_same_volume_unknown_c_recovers_103(self):
        from core import secure_files
        target, adapter, store = self._retarget_external()
        original = secure_files.secure_replace_runtime
        def commit_then_unknown(*args, **kwargs):
            result = original(*args, **kwargs)
            raise secure_files.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                                candidate=result.candidate)
        with patch.object(secure_files, 'secure_replace_runtime', side_effect=commit_then_unknown):
            store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        with patch.object(adapter, 'observe', side_effect=LedgerIOError('fake')):
            store.record(ACC, 1)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(target.read_text())['total']['requests'], 103)

    def test_external_target_parent_staging_selection_with_injected_cross_volume(self):
        from core import secure_files
        target, adapter, store = self._retarget_external()
        # Branch injection only: this is NOT a real cross-volume test.
        with patch.object(secure_files, '_same_volume', return_value=False), \
             patch.object(adapter, '_same_volume_evidence', return_value=False):
            store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(target.read_text())['total']['requests'], 101)
        self.assertEqual([x.name for x in target.parent.iterdir()], ['ledger.json'])

    def test_external_same_volume_does_not_require_target_parent_namespace(self):
        target, adapter, store = self._retarget_external(reserved_role=True)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(target.read_text())['total']['requests'], 101)

    def test_external_cross_volume_staging_namespace_collision_is_rejected(self):
        from core import secure_files
        target, adapter, store = self._retarget_external(reserved_role=True)
        before = target.read_bytes()
        with patch.object(secure_files, '_same_volume', return_value=False), \
             patch.object(adapter, '_same_volume_evidence', return_value=False):
            store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'PENDING')
        self.assertEqual(target.read_bytes(), before)

    def test_real_guard_cannot_authorize_a_different_target(self):
        with self.assertRaises(LedgerIOError):
            MacLedgerSecureIO(root=self.root, target=self.state / 'other.json', guard=self.adapter.guard)

    def test_real_mac_v2_v3_migration_archive_reasoning_and_restart(self):
        combo_key = 'supply=fake|route=fake|strategy=fake'
        def bucket(n, reasoning):
            return {'requests': n, 'ok': n, 'fail': 0, 'sum_ms': n * 10,
                    'combos': {combo_key: {'requests': n, 'ok': n, 'fail': 0,
                                           'usage_in': n * 2, 'usage_out': n * 3,
                                           'usage_reasoning': reasoning}}}
        for version in (2, 3):
            with self.subTest(version=version):
                data = {'version': version, 'since': '2026-01-01', 'keep_days': 400,
                        'total': bucket(3, 5),
                        'days': {'2026-01-01': bucket(1, 2), '2026-01-02': bucket(1, 3)},
                        'months_archive': {'2026-01': bucket(1, 7)}}
                self.ledger.write_text(json.dumps(data), encoding='utf-8')
                fresh = inspect_runtime_identities(self.root, self.paths)
                snapshot = server._capture_migration_ledger(fresh)
                store = server.UsageTotalsStore.from_verified_snapshot(self.ledger, snapshot, adapter=self.adapter)
                fixed = datetime(2026, 1, 3, tzinfo=timezone.utc)
                with patch.object(server, 'KEEP_DAYS', 1), patch.object(server, '_cst_now', return_value=fixed):
                    store.record(ACC, 7)
                self.assertEqual(store.persistence_state, 'CLEAN')
                disk = json.loads(self.ledger.read_text())
                self.assertEqual(disk['version'], 3)
                self.assertEqual(disk['total']['requests'], 4)
                self.assertEqual(disk['total']['combos'][combo_key]['usage_reasoning'], 5)
                self.assertEqual(disk['months_archive']['2026-01']['requests'], 3)
                self.assertEqual(disk['months_archive']['2026-01']['combos'][combo_key]['usage_reasoning'], 12)
                fresh = inspect_runtime_identities(self.root, self.paths)
                restart_snapshot = server._capture_migration_ledger(fresh)
                restarted = server.UsageTotalsStore.from_verified_snapshot(self.ledger, restart_snapshot,
                                                                           adapter=self.adapter)
                restarted.record(ACC, 5)
                self.assertEqual(restarted.persistence_state, 'CLEAN')
                self.assertEqual(json.loads(self.ledger.read_text())['total']['requests'], 5)

    def test_fake_guard_cannot_be_used_as_platform_authority(self):
        with self.assertRaises(LedgerIOError):
            MacLedgerSecureIO(root=self.root, target=self.ledger, guard=lambda: True)


class SyntheticWindowsLedgerTests(unittest.TestCase):
    """Fake Win32, fake held-lock verification; never a native Windows claim."""
    ROOT = r'C:\fixture\project'
    STATE = ROOT + r'\.runtime'
    STAGE = STATE + r'\tmp'
    TARGET = STATE + r'\totals.json'
    CONFIG_DIR = ROOT + r'\config'
    CONFIG = CONFIG_DIR + r'\business.json'
    PATHS_FILE = CONFIG_DIR + r'\runtime_paths.json'

    def setUp(self):
        import ntpath
        from pathlib import PureWindowsPath
        from types import SimpleNamespace
        from core import discovery, platform_windows as win, windows_acl as acl, windows_secure_files as secure
        from core.windows_paths import WindowsPathBackend
        from tests.test_windows_adapter import VirtualACL
        from tests.test_windows_discovery import PathWinBridge
        from tests.test_windows_same_volume_commit import MultiVolumeFS
        from core.ledger_secure_io import WindowsLedgerSecureIO
        self.fs = MultiVolumeFS()
        self.fs.objects[self.STAGE] = self.fs.obj(31, directory=True)
        self.fs.objects[self.CONFIG_DIR] = self.fs.obj(32, directory=True)
        self.fs.objects[self.CONFIG] = dict(self.fs.obj(33), data=b'{}')
        self.fs.objects[self.PATHS_FILE] = dict(self.fs.obj(34), data=b'{}')
        data = server.UsageTotalsStore._empty_data()
        data['total']['requests'] = 100
        data['total']['ok'] = 100
        self.fs.objects[self.TARGET] = dict(self.fs.obj(35), data=json.dumps(data).encode())
        self.paths = {name: PureWindowsPath(self.STATE + '\\' + name + '.json') for name in PATH_KEYS}
        self.paths['totals'] = PureWindowsPath(self.TARGET)
        self.acl = VirtualACL(self.fs)
        backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for item in (
            patch.object(os, 'name', 'nt'), patch.object(os, 'path', ntpath),
            patch.object(server.sys, 'platform', 'win32'), patch.object(server, 'Path', PureWindowsPath),
            patch.object(server, '_PACKAGE_DIR', PureWindowsPath(self.ROOT)),
            patch.object(server, '_RUNTIME_PATHS_FILE', PureWindowsPath(self.PATHS_FILE)),
            patch.object(server, '_runtime_paths', self.paths),
            patch.object(server, 'resolve_runtime_paths', return_value=self.paths),
            patch.object(server, 'verify_held_instance_locks'),
            patch.object(secure, '_windows_path_backend', return_value=backend),
            patch.object(discovery, '_windows_path_backend', return_value=backend),
            patch.object(win, '_api', side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, '_NativeBackend', return_value=self.acl),
        ):
            self.stack.enter_context(item)
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths, windows_light=True)
        self.legacy = SimpleNamespace(snapshot=snapshot)
        self.adapter = server._ledger_runtime_adapter(PureWindowsPath(self.CONFIG), object(), self.legacy)
        self.assertIsInstance(self.adapter, WindowsLedgerSecureIO)
        from core.config_snapshot import read_stable_config
        initial = read_stable_config(self.TARGET, reject_duplicate_keys=True)
        self.store = server.UsageTotalsStore.from_verified_snapshot(self.TARGET, initial, adapter=self.adapter)

    def _retarget_fake(self, target, *, exists=True, volume=17):
        from pathlib import PureWindowsPath
        from types import SimpleNamespace
        from core import discovery
        from core.config_snapshot import read_stable_config
        self.paths['totals'] = PureWindowsPath(target)
        if exists:
            data = server.UsageTotalsStore._empty_data()
            data['total']['requests'] = data['total']['ok'] = 100
            self.fs.objects[target] = dict(self.fs.obj(88), volume=volume, data=json.dumps(data).encode())
        else:
            self.fs.objects.pop(target, None)
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths, windows_light=True)
        self.legacy = SimpleNamespace(snapshot=snapshot)
        adapter = server._ledger_runtime_adapter(PureWindowsPath(self.CONFIG), object(), self.legacy)
        self.assertIsNotNone(adapter)
        initial = read_stable_config(target, reject_duplicate_keys=True) if exists else None
        return adapter, server.UsageTotalsStore.from_verified_snapshot(target, initial, adapter=adapter)

    def test_windows_guard_cannot_authorize_different_target(self):
        from core.ledger_secure_io import WindowsLedgerSecureIO
        with self.assertRaises(LedgerIOError):
            WindowsLedgerSecureIO(root=self.ROOT, target=self.STATE + r'\other.json', guard=self.adapter.guard)

    def test_windows_missing_staging_directory_is_privately_created(self):
        del self.fs.objects[self.STAGE]
        def mkdir(path, *, user_sid):
            if path in self.fs.objects:
                raise FileExistsError('fake')
            self.fs.objects[path] = self.fs.obj(9001, directory=True)
        self.fs.create_private_directory = mkdir
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertIn(self.STAGE, self.fs.objects)
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 101)
        self.assertFalse(self.fs.handles)

    def test_windows_new_target_concurrent_appearance_is_not_overwritten(self):
        adapter, store = self._retarget_fake(self.TARGET, exists=False)
        original = self.fs.move_new
        def conflict(source, target):
            self.fs.objects[target] = dict(self.fs.obj(1234), data=b'fake-other')
            return original(source, target)
        with patch.object(self.fs, 'move_new', side_effect=conflict):
            store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'fake-other')
        self.assertFalse(self.fs.handles)

    def test_windows_corrupt_ledger_is_unavailable_and_not_overwritten(self):
        self.fs.objects[self.TARGET]['data'] = b'{bad'
        snapshot = server._capture_migration_ledger(self.legacy.snapshot)
        self.assertIsInstance(snapshot, server.LedgerUnavailable)
        store = server.UsageTotalsStore.from_verified_snapshot(self.TARGET, snapshot, adapter=self.adapter)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNAVAILABLE')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'{bad')
        self.assertFalse(self.fs.commits)
        self.assertFalse(self.fs.handles)

    def test_windows_missing_target_create_no_overwrite(self):
        adapter, store = self._retarget_fake(self.TARGET, exists=False)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 1)
        self.assertEqual(self.fs.commits[0][0], 'move')
        self.assertFalse(self.fs.handles)

    def test_windows_external_same_volume_stages_in_runtime_tmp(self):
        target = r'C:\fixture\runtime\external-ledger.json'
        adapter, store = self._retarget_fake(target)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[target]['data'])['total']['requests'], 101)
        self.assertTrue(self.fs.commits[-1][2].startswith(self.STAGE + '\\'))
        self.assertFalse(self.fs.handles)

    def test_windows_external_cross_volume_stages_at_target_parent(self):
        target = r'D:\backups\external-ledger.json'
        adapter, store = self._retarget_fake(target, volume=18)
        store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[target]['data'])['total']['requests'], 101)
        self.assertTrue(self.fs.commits[-1][2].startswith(r'D:\backups\.model-proxy-stage-'))
        self.assertFalse(self.fs.handles)

    def test_windows_unknown_c_recovers_exactly_to_103(self):
        original = self.fs.replace_existing
        def replace_then_fail(target, stage):
            original(target, stage)
            raise OSError('fake outcome unknown')
        with patch.object(self.fs, 'replace_existing', side_effect=replace_then_fail):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 101)
        with patch.object(self.adapter, 'observe', side_effect=LedgerIOError('fake')):
            self.store.record(ACC, 1)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 103)
        self.assertFalse(self.fs.handles)

    def test_windows_not_replaced_pending_then_full_retry(self):
        from core import windows_secure_files as secure
        failure = secure.SecureWriteError('fake', state='NOT_REPLACED')
        with patch.object(secure, '_finite_write', side_effect=failure):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'PENDING')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 100)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 102)
        self.assertFalse(self.fs.handles)

    def test_windows_unverifiable_stage_volume_blocks_commit(self):
        before = self.fs.objects[self.TARGET]['data']
        self.fs.objects[self.STAGE]['volume'] = 0
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], before)
        self.assertFalse(self.fs.commits)
        self.assertFalse(self.fs.handles)

    def test_windows_unknown_b_recovers_exactly_to_103(self):
        def fail_with_consumed_stage(target, stage):
            self.fs.objects.pop(stage)
            raise OSError('fake outcome unknown')
        with patch.object(self.fs, 'replace_existing', side_effect=fail_with_consumed_stage):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 100)
        with patch.object(self.adapter, 'observe', side_effect=LedgerIOError('fake')):
            self.store.record(ACC, 1)
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 103)
        self.assertFalse(self.fs.handles)

    def test_windows_unknown_with_residual_stage_stays_quarantined(self):
        with patch.object(self.fs, 'replace_existing', side_effect=OSError('fake')):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertTrue(any('\\.model-proxy-stage-' in name for name in self.fs.objects))
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 100)
        self.assertFalse(self.fs.handles)

    def test_windows_unknown_c_with_different_identity_never_overwrites(self):
        original = self.fs.replace_existing
        def replace_then_fail(target, stage):
            original(target, stage)
            raise OSError('fake outcome unknown')
        with patch.object(self.fs, 'replace_existing', side_effect=replace_then_fail):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        replacement = dict(self.fs.objects[self.TARGET])
        replacement['index'] += 10000
        self.fs.objects[self.TARGET] = replacement
        before = replacement['data']
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], before)
        self.assertEqual(len(self.fs.commits), 1)
        self.assertFalse(self.fs.handles)

    def test_windows_unsafe_acl_never_overwrites(self):
        obj = self.fs.objects[self.TARGET]
        before = obj['data']
        obj['unsafe_group'] = True
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], before)
        self.assertFalse(self.fs.commits)
        self.assertFalse(self.fs.handles)

    def test_second_preflight_root_close_unknown_quarantines_future_writes(self):
        """Real guard code with fake Win32 and mocked held-lock verification."""
        from core import discovery
        real_preflight = discovery._windows_open_preflight
        calls = 0
        failed = []
        def open_preflight(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                root_probes = 0
                def fail_probe(typ, path, count):
                    nonlocal root_probes
                    if typ == 'probe' and path == 'C:\\':
                        root_probes += 1
                        # First handle is the held lease; the second is the
                        # temporary revalidation probe closed inside preflight.
                        if root_probes == 2:
                            handle = self.fs.next_handle + 1
                            failed.append(handle)
                            self.fs.close_fail.add(handle)
                            self.fs.on_open = None
                self.fs.on_open = fail_probe
            return real_preflight(*args, **kwargs)
        def cleanup_fake_handles():
            self.fs.on_open = None
            self.fs.close_fail.clear()
            for handle in list(self.fs.handles):
                self.fs.close(handle)
        self.addCleanup(cleanup_fake_handles)
        with patch.object(discovery, '_windows_open_preflight', side_effect=open_preflight), \
             self.assertLogs('core.ledger_secure_io', level='WARNING') as alerts:
            self.store.record(ACC, 1)
            self.assertEqual(calls, 2)
            self.assertEqual(self.store.persistence_state, 'UNKNOWN')
            self.assertTrue(self.adapter._resources_unknown)
            self.assertTrue(failed)
            self.assertIn(failed[0], self.fs.handles)
            self.assertEqual(self.fs.close_calls.count(failed[0]), 1)
            self.store.record(ACC, 1)
            self.assertEqual(calls, 2)
            self.assertEqual(self.fs.close_calls.count(failed[0]), 1)
        self.assertEqual(len(alerts.output), 1)
        self.assertNotIn(self.TARGET, alerts.output[0])
        self.assertNotIn('requests', alerts.output[0])
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.store._data['total']['requests'], 102)
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 100)
        self.assertFalse(self.fs.commits)

    def test_windows_config_preflight_preserves_release_category(self):
        from core import discovery
        for code, expected in (('release_unknown', 'lock_release_unknown'),
                               ('windows_evidence_unavailable', 'config_path_unknown')):
            with self.subTest(code=code), \
                 patch.object(discovery, '_windows_open_preflight',
                              side_effect=server.PreflightError(code, 'fake')):
                with self.assertRaises(server.LockError) as caught:
                    server._windows_check_config_targets(self.paths, self.CONFIG, project_root=self.ROOT)
                self.assertEqual(caught.exception.code, expected)

    def test_windows_guard_lock_failure_never_overwrites(self):
        before = self.fs.objects[self.TARGET]['data']
        with patch.object(server, 'verify_held_instance_locks',
                          side_effect=server.PreflightError('locks_unverified', 'fake')):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], before)
        self.assertFalse(self.fs.commits)
        self.assertFalse(self.fs.handles)

    def test_windows_external_stage_name_role_collision_is_rejected(self):
        from pathlib import PureWindowsPath
        import secrets
        target = r'D:\backups\external-ledger.json'
        stage = r'D:\backups\.model-proxy-stage-' + 'a' * 24
        self.paths['ensure_log'] = PureWindowsPath(stage)
        adapter, store = self._retarget_fake(target, volume=18)
        before = self.fs.objects[target]['data']
        with patch.object(secrets, 'token_hex', return_value='a' * 24):
            store.record(ACC, 1)
        self.assertEqual(store.persistence_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[target]['data'], before)
        self.assertNotIn(stage, self.fs.objects)
        self.assertFalse(self.fs.commits)
        self.assertFalse(self.fs.handles)

    def test_windows_release_unknown_after_commit_stays_quarantined(self):
        from core import windows_secure_files as secure
        original = secure._finite_write
        def committed_then_unknown(*args, **kwargs):
            original(*args, **kwargs)
            raise secure.SecureWriteError('release_unknown', state='UNKNOWN', release_unknown=True)
        with patch.object(secure, '_finite_write', side_effect=committed_then_unknown):
            self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'UNKNOWN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 101)
        self.assertEqual(self.store._data['total']['requests'], 102)
        self.assertFalse(self.fs.handles)

    def test_real_windows_adapter_with_fake_native_backend(self):
        self.store.record(ACC, 1)
        self.assertEqual(self.store.persistence_state, 'CLEAN')
        self.assertEqual(json.loads(self.fs.objects[self.TARGET]['data'])['total']['requests'], 101)
        self.assertEqual(self.fs.commits[0][0], 'replace')
        self.assertFalse(self.fs.handles)



if __name__ == '__main__':
    unittest.main()
