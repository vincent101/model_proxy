"""Offline install/Windows writer wiring; all paths and secrets are fake."""
import contextlib
import io
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core.cli import _install_ops as install
from core import windows_secure_files as win_secure


class FakeSnapshot:
    exists = True
    contents = b'old-fake'
    def __init__(self, *, close_fails=False):
        self.closes = 0
        self.revalidations = 0
        self.close_fails = close_fails
    def revalidate(self):
        if self.closes:
            raise win_secure.SecureSnapshotError('snapshot_invalid')
        self.revalidations += 1
    def close(self):
        self.closes += 1
        if self.close_fails:
            raise win_secure.SecureSnapshotError('release_unknown')


class WindowsInstallWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / 'fake-client.json'
        self.root = Path(self.temp.name)
        self.fake_os = types.SimpleNamespace(name='nt', path=os.path)
        self.snapshot = FakeSnapshot()
        self.snapshot.path = str(self.target)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(install, 'os', self.fake_os))
        self.stack.enter_context(patch.object(install, '_root_for', return_value=self.root))
        self.stack.enter_context(patch('core.cli.dispatch.write_preflight', return_value=True))
        self.stack.enter_context(patch.object(install.urllib.request, 'urlopen',
                                             side_effect=OSError('fake offline')))
        self.capture = self.stack.enter_context(patch.object(win_secure, 'capture_target', return_value=self.snapshot))
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))

    def install_with(self, action):
        def run(token, port):
            self.assertEqual(token, 'fake-token')
            action()
        with patch.object(install, 'load_config', return_value={}), \
             patch.object(install, 'choose_token', return_value='fake-token'), \
             patch.object(install, 'ensure_session_hook'), \
             patch.dict(install._INSTALLERS, {'claude': run}):
            return install.cmd_install('fake-config', '12345', ['claude'], preflight_root=self.root)

    def test_windows_snapshot_and_blocked_writer_are_wired_and_closed(self):
        def action():
            snap, old = install._snapshot_text(self.target)
            self.assertEqual(old, 'old-fake')
            def blocked(snapshot, payload, *, preflight):
                self.assertIs(snapshot, snap)
                self.assertEqual(payload, b'new-fake')
                self.assertTrue(preflight())
                raise win_secure.SecureWriteError('candidate_protection_unverified')
            with patch.object(install, 'confirm', return_value=True), \
                 patch.object(win_secure, 'write_target', side_effect=blocked) as writer, \
                 patch.object(install, 'secure_replace') as mac_writer:
                try:
                    install.preview_confirm_write(self.target, old, 'new-fake', 'claude', [], snapshot=snap)
                finally:
                    writer.assert_called_once()
                    mac_writer.assert_not_called()
        self.assertEqual(self.install_with(action), 1)
        self.assertEqual(self.snapshot.closes, 1)
        self.capture.assert_called_once()
        self.assertFalse(self.target.exists())
        self.assertIn('candidate_protection_unverified', self.stderr.getvalue())
        self.assertNotIn('fake-token', self.stdout.getvalue() + self.stderr.getvalue())

    def _run_first_codex_catalog_install(self, *, creation_failure=None):
        from core.platform_windows import WindowsPrivateCreationError
        from core.windows_secure_files import WriteOutcome
        codex = self.root / '.codex'
        codex.mkdir(mode=0o700)
        config = codex / 'config.toml'
        config.write_text('', encoding='utf-8')
        catalog = codex / 'model-catalogs' / 'model_proxy_catalog.json'
        template = self.root / 'catalog-template.json'
        template.write_text('{"models": [{"base_instructions": "placeholder"}]}', encoding='utf-8')
        events = []
        def capture(path, **kwargs):
            snap = FakeSnapshot()
            snap.path = str(path)
            snap.exists = Path(path).exists()
            snap.contents = Path(path).read_bytes() if snap.exists else None
            return snap
        self.capture.side_effect = capture
        def write(snap, payload, *, preflight):
            self.assertTrue(preflight())
            events.append(Path(snap.path).name)
            Path(snap.path).write_bytes(payload)
            return WriteOutcome('VERIFIED', None, False, 'NONE')
        def make_dir(path, *, required_access):
            self.assertEqual(path, str(catalog.parent))
            self.assertEqual(required_access, 0x200A3)
            events.append('mkdir')
            if creation_failure:
                if creation_failure.created is True:
                    catalog.parent.mkdir(mode=0o700)
                raise creation_failure
            catalog.parent.mkdir(mode=0o700)
            return types.SimpleNamespace(created=True)
        def action():
            with patch.object(Path, 'home', return_value=self.root), \
                 patch.dict(install.SDKS['codex'], {'config_path': config}), \
                 patch.object(install, '_CODEX_CATALOG_TARGET', catalog), \
                 patch.object(install, '_CODEX_CATALOG_TEMPLATE', template), \
                 patch.object(install, 'confirm', return_value=True), \
                 patch.object(install.urllib.request, 'urlopen', return_value=io.BytesIO(b'fake-prompt')), \
                 patch.object(win_secure, 'write_target', side_effect=write), \
                 patch.object(install, '_windows_catalog_directory_preflight', return_value=True), \
                 patch('core.platform_windows.ensure_private_directory', side_effect=make_dir):
                install.install_codex('fake-token', '12345')
        result = self.install_with(action)
        return result, config, catalog, events

    def test_windows_first_codex_install_creates_private_catalog_and_links_it(self):
        result, config, catalog, events = self._run_first_codex_catalog_install()
        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual(events, ['config.toml', 'mkdir', 'model_proxy_catalog.json', 'config.toml'])
        self.assertTrue(catalog.is_file())
        self.assertIn('model_catalog_json', config.read_text())
        self.assertIn('codex: VERIFIED', self.stdout.getvalue())

    def _run_existing_catalog_preflight(self, *, close_fails=False):
        codex = self.root / '.codex'
        directory = codex / 'model-catalogs'
        directory.mkdir(parents=True, mode=0o700)
        config = codex / 'config.toml'
        config.write_text('', encoding='utf-8')
        catalog = directory / 'model_proxy_catalog.json'
        catalog.write_text('{}', encoding='utf-8')
        template = self.root / 'catalog-template.json'
        template.write_text('{"models":[]}', encoding='utf-8')
        snapshots, writes = [], []
        def capture(path, **kwargs):
            file = Path(path)
            snap = FakeSnapshot(close_fails=close_fails and file == catalog)
            snap.path = str(file)
            snap.exists = file.exists()
            snap.contents = file.read_bytes() if snap.exists else None
            snapshots.append(snap)
            return snap
        self.capture.side_effect = capture
        def write(snap, payload, *, preflight):
            self.assertTrue(preflight())
            writes.append(Path(snap.path))
            Path(snap.path).write_bytes(payload)
            return win_secure.WriteOutcome('VERIFIED', None, False, 'NONE')
        def action():
            with patch.object(Path, 'home', return_value=self.root), \
                 patch.dict(install.SDKS['codex'], {'config_path': config}), \
                 patch.object(install, '_CODEX_CATALOG_TARGET', catalog), \
                 patch.object(install, '_CODEX_CATALOG_TEMPLATE', template), \
                 patch.object(install.urllib.request, 'urlopen',
                              return_value=io.BytesIO(b'fake-prompt')), \
                 patch.object(install, '_install_catalog_asset', return_value=False), \
                 patch.object(install, 'confirm', return_value=True), \
                 patch.object(win_secure, 'write_target', side_effect=write):
                install.install_codex('fake-token', '12345')
        return self.install_with(action), config, catalog, snapshots, writes

    def test_existing_catalog_preflight_releases_before_config_capture(self):
        result, config, catalog, snapshots, writes = self._run_existing_catalog_preflight()
        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual([Path(s.path) for s in snapshots], [catalog, config])
        self.assertEqual((snapshots[0].revalidations, snapshots[0].closes), (1, 1))
        self.assertEqual(writes, [config])

    def test_catalog_preflight_unknown_release_blocks_config(self):
        result, config, catalog, snapshots, writes = self._run_existing_catalog_preflight(close_fails=True)
        self.assertEqual(result, 1)
        self.assertEqual([Path(s.path) for s in snapshots], [catalog])
        self.assertEqual((snapshots[0].revalidations, snapshots[0].closes), (1, 1))
        self.assertEqual(writes, [])
        self.assertEqual(config.read_text(), '')
        self.assertIn('release_unknown', self.stderr.getvalue())
        self.assertIn('Codex 配置未修改', self.stderr.getvalue())

    def test_windows_catalog_directory_failure_reports_prior_saved_config(self):
        from core.platform_windows import WindowsPrivateCreationError
        failure = WindowsPrivateCreationError('protection_unverified', created=True)
        result, config, catalog, events = self._run_first_codex_catalog_install(creation_failure=failure)
        self.assertEqual(result, 1)
        self.assertEqual(events, ['config.toml', 'mkdir'])
        self.assertIn('model_provider', config.read_text())
        self.assertNotIn('model_catalog_json', config.read_text())
        self.assertFalse(catalog.exists())
        self.assertIn('空目录可能已创建', self.stderr.getvalue())
        self.assertIn('主配置已保存并验证；catalog 未完成', self.stdout.getvalue())
        self.assertIn('codex: VERIFIED', self.stdout.getvalue())

    def test_windows_catalog_unknown_creation_reports_unknown_and_saved_config(self):
        from core.platform_windows import WindowsPrivateCreationError
        failure = WindowsPrivateCreationError('creation_unverified', created=None, release_unknown=True)
        result, config, catalog, events = self._run_first_codex_catalog_install(creation_failure=failure)
        self.assertEqual(result, 1)
        self.assertEqual(events, ['config.toml', 'mkdir'])
        self.assertIn('model_provider', config.read_text())
        self.assertFalse(catalog.exists())
        self.assertIn('目录创建状态未知', self.stderr.getvalue())
        self.assertIn('句柄关闭状态未知', self.stderr.getvalue())
        self.assertIn('主配置已保存并验证；catalog 未完成', self.stdout.getvalue())
        self.assertIn('codex: VERIFIED', self.stdout.getvalue())

    def test_preflight_target_must_match_snapshot_target(self):
        def action():
            snap, old = install._snapshot_text(self.target)
            snap.path = str(self.root / 'other-fake.json')
            with patch.object(install, 'confirm') as confirm, \
                 patch.object(win_secure, 'write_target') as writer:
                try:
                    install.preview_confirm_write(self.target, old, 'new-fake', 'claude', [], snapshot=snap)
                finally:
                    confirm.assert_not_called()
                    writer.assert_not_called()
        self.assertEqual(self.install_with(action), 1)
        self.assertEqual(self.snapshot.closes, 1)
        self.assertIn('目标与预览快照不一致', self.stderr.getvalue())

    def test_verified_target_changed_backup_reports_independent_states(self):
        def action():
            snap, old = install._snapshot_text(self.target)
            error = win_secure.SecureWriteError('backup_changed', state='VERIFIED',
                                                  backup='fake-backup', backup_status='CHANGED')
            with patch.object(install, 'confirm', return_value=True), \
                 patch.object(win_secure, 'write_target', side_effect=error):
                install.preview_confirm_write(self.target, old, 'new-fake', 'claude', [], snapshot=snap)
        self.assertEqual(self.install_with(action), 1)
        output = self.stdout.getvalue()
        self.assertIn('目标已替换，后验验证通过', output)
        self.assertIn('备份状态: CHANGED', output)
        self.assertNotIn('目标替换状态未知', output)
        self.assertNotIn('目标未替换', output)
        self.assertEqual(self.snapshot.closes, 1)

    def test_verified_target_unknown_close_reports_both(self):
        def action():
            snap, old = install._snapshot_text(self.target)
            error = win_secure.SecureWriteError('release_unknown', state='POSTCHECK_FAILED',
                                                target_state='VERIFIED', backup='fake-backup',
                                                backup_status='UNKNOWN', release_unknown=True)
            with patch.object(install, 'confirm', return_value=True), \
                 patch.object(win_secure, 'write_target', side_effect=error):
                install.preview_confirm_write(self.target, old, 'new-fake', 'claude', [], snapshot=snap)
        self.assertEqual(self.install_with(action), 1)
        output = self.stdout.getvalue()
        self.assertIn('目标已替换，后验验证通过', output)
        self.assertIn('备份状态: UNKNOWN', output)
        self.assertIn('句柄关闭状态未知', output)
        self.assertNotIn('目标替换状态未知', output)
        self.assertNotIn('后验验证失败', output)
        self.assertIn('VERIFIED_RELEASE_UNKNOWN', output)

    def test_codex_unchanged_config_releases_before_catalog_reference_write(self):
        # The first real Codex rewrite is already current; its read-only lease
        # must be gone before the second rewrite of the same target starts.
        text, _ = install._rewrite_codex_toml('', top={
            'model': 'claude-sonnet', 'model_provider': 'model_proxy'}, provider={
            'base_url': 'http://localhost:12345/v1', 'wire_api': 'responses',
            'experimental_bearer_token': 'fake-token'})
        self.snapshot.contents = text.encode('utf-8')
        second = FakeSnapshot()
        second.contents = self.snapshot.contents
        second.path = str(self.target)
        self.capture.side_effect = [self.snapshot, second]
        def action():
            def blocked(snapshot, payload, *, preflight):
                self.assertIs(snapshot, second)
                self.assertEqual(self.snapshot.revalidations, 1)
                self.assertEqual(self.snapshot.closes, 1)
                self.assertEqual(second.closes, 0)
                self.assertIn(b'model_catalog_json', payload)
                self.assertTrue(preflight())
                raise win_secure.SecureWriteError('candidate_protection_unverified')
            with patch.dict(install.SDKS['codex'], {'config_path': self.target}), \
                 patch.object(install, '_install_catalog_asset', return_value=True), \
                 patch.object(install, 'confirm', return_value=True), \
                 patch.object(win_secure, 'write_target', side_effect=blocked) as writer:
                try:
                    install.install_codex('fake-token', '12345')
                finally:
                    writer.assert_called_once()
        self.assertEqual(self.install_with(action), 1)
        self.assertEqual(self.snapshot.closes, 1)
        self.assertEqual(second.closes, 1)
        self.assertEqual(self.capture.call_count, 2)

    def test_unconsumed_snapshot_blocks_second_capture_without_closing_it(self):
        def action():
            install._snapshot_text(self.target)
            try:
                install._snapshot_text(self.target)
            finally:
                self.assertEqual(self.snapshot.closes, 0)
        self.assertEqual(self.install_with(action), 1)
        self.assertEqual(self.snapshot.closes, 1)
        self.capture.assert_called_once()
        self.assertIn('prior_snapshot_unreleased', self.stderr.getvalue())

    def test_snapshot_closed_when_cancelled_before_writer(self):
        def action():
            snap, old = install._snapshot_text(self.target)
            with patch.object(install, 'confirm', return_value=False), \
                 patch.object(win_secure, 'write_target') as writer:
                self.assertFalse(install.preview_confirm_write(self.target, old, 'new-fake',
                                                               'claude', [], snapshot=snap))
                writer.assert_not_called()
        self.assertEqual(self.install_with(action), 0)
        self.assertEqual(self.snapshot.closes, 1)

    def test_snapshot_close_failure_turns_success_into_failure(self):
        self.snapshot.close_fails = True
        self.assertEqual(self.install_with(lambda: install._snapshot_text(self.target)), 1)
        self.assertEqual(self.snapshot.closes, 1)
        self.assertIn('释放状态未知', self.stderr.getvalue())

    def test_unknown_commit_is_never_reported_as_not_replaced(self):
        def action():
            snap, old = install._snapshot_text(self.target)
            with patch.object(install, 'confirm', return_value=True), \
                 patch.object(win_secure, 'write_target', side_effect=win_secure.SecureWriteError(
                     'commit_unknown', state='UNKNOWN')):
                install.preview_confirm_write(self.target, old, 'new-fake', 'claude', [], snapshot=snap)
        self.assertEqual(self.install_with(action), 1)
        self.assertIn('替换状态未知', self.stdout.getvalue())
        self.assertNotIn('目标未替换', self.stdout.getvalue())
        self.assertEqual(self.snapshot.closes, 1)


if __name__ == '__main__':
    unittest.main()
