"""Real macOS filesystem, fake default project; run only in an isolated copy."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if ROOT != ISOLATION / 'project' or not (ISOLATION / '.model-proxy-isolation').is_file():
    raise RuntimeError('isolated test project required')
if not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISOLATION):
    raise RuntimeError('isolated HOME required')

from core.cli import _config_ops as config, _install_ops as install, dispatch
from core.runtime_paths import DEFAULTS


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS filesystem and ACLs required')
class MacDefaultRuntimeWiring(unittest.TestCase):
    def setUp(self):
        # /private/tmp avoids macOS per-user temp ancestors with rootless xattrs.
        self.temp = tempfile.TemporaryDirectory(prefix='mp-cli-runtime-fake-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.directory = self.root / 'config'
        self.directory.mkdir(parents=True, mode=0o700)
        self.config = self.directory / 'model_proxy_config.json'
        self.paths = self.directory / 'runtime_paths.json'
        self.paths.write_text(json.dumps(DEFAULTS), encoding='utf-8')
        self.original = b'{"routes": [], "strategies": [], "supplies": []}\n'
        self.config.write_bytes(self.original)
        self.config.chmod(0o600)

    @contextlib.contextmanager
    def prepared(self, source=None):
        with mock.patch.object(dispatch, 'ROOT', self.root), \
             mock.patch.multiple(config, _WRITE_BASELINE=None, _WRITE_STARTED=False,
                                 _WRITE_OPERATION='switch', _WRITE_AUTHORIZATION=None,
                                 _WRITE_PREFLIGHT=None, _WRITE_INPUT_PATH=None,
                                 _WRITE_COMMITTED=None, _WRITE_LOCATIONS=None,
                                 _MARKER_BASELINE=None, _MARKER_CONTENT=None,
                                 _MARKER_REQUIRED=False):
            config._prepare_write(str(source or self.config), 'switch')
            yield

    def save(self, source=None):
        with mock.patch.object(config, 'confirm', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            config.atomic_write(str(source or self.config),
                                {'routes': [], 'strategies': [], 'supplies': [],
                                 'fake_test_value': 'updated'})

    def test_default_runtime_real_write_and_backup(self):
        stage = self.root / '.runtime' / 'tmp'
        backup = self.root / '.runtime' / 'backups'
        real_replace = os.replace
        calls = []
        def observe(src, dst, *, src_dir_fd, dst_dir_fd):
            calls.append((os.fstat(src_dir_fd).st_ino, os.fstat(dst_dir_fd).st_ino))
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        with self.prepared(), mock.patch('core.mac_config_writer.os.replace', side_effect=observe):
            self.save()
        self.assertEqual(calls, [(stage.stat().st_ino, self.directory.stat().st_ino)])
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(list(stage.iterdir()), [])
        backups = list(backup.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(list(self.directory.glob('*.bak.*')), [])
        self.assertEqual(json.loads(self.config.read_text())['fake_test_value'], 'updated')

    def test_approved_config_symlink_still_works(self):
        target = self.directory / 'real.json'
        self.config.rename(target)
        self.config.symlink_to(target.name)
        link_inode = self.config.lstat().st_ino
        with self.prepared(), contextlib.redirect_stdout(io.StringIO()):
            self.save()
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(self.config.lstat().st_ino, link_inode)
        self.assertEqual(json.loads(target.read_text())['fake_test_value'], 'updated')
        self.assertEqual(len(list((self.root / '.runtime' / 'backups').iterdir())), 1)

    def test_external_symlink_requires_existing_authorization_and_preserves_link(self):
        external = self.base / 'external'
        external.mkdir(mode=0o700)
        target = external / 'real.json'
        target.write_bytes(self.original)
        target.chmod(0o600)
        self.config.unlink()
        self.config.symlink_to(target)
        link_inode = self.config.lstat().st_ino
        with mock.patch('core.cli.authority._require_same_terminal'), \
             mock.patch.object(sys, 'stdin', io.StringIO('YES\n')), \
             mock.patch.object(dispatch, '_confirm_config_write',
                               wraps=dispatch._confirm_config_write) as grant, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.prepared():
                self.save()
        grant.assert_called_once()
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(self.config.lstat().st_ino, link_inode)
        self.assertEqual(json.loads(target.read_text())['fake_test_value'], 'updated')
        self.assertEqual([p.read_bytes() for p in (self.root / '.runtime' / 'backups').iterdir()],
                         [self.original])
        self.assertEqual(list(external.glob('*.bak.*')), [])
        self.assertEqual(list(external.glob('.model-proxy-*')), [])

    def test_external_symlink_denied_authorization_never_writes(self):
        from core.discovery import PreflightError
        external = self.base / 'external'
        external.mkdir(mode=0o700)
        target = external / 'real.json'
        target.write_bytes(self.original)
        target.chmod(0o600)
        self.config.unlink()
        self.config.symlink_to(target)
        with mock.patch('core.cli.authority._require_same_terminal'), \
             mock.patch.object(sys, 'stdin', io.StringIO('NO\n')), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(PreflightError):
            with self.prepared():
                self.save()
        self.assertEqual(target.read_bytes(), self.original)
        self.assertFalse((self.root / '.runtime' / 'backups').exists())

    def test_cross_volume_fails_closed_without_sibling_stage(self):
        from core.mac_config_writer import MacWriteError
        with mock.patch('core.mac_config_writer._same_volume', return_value=False):
            with self.assertRaises(MacWriteError):
                with self.prepared():
                    self.save()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(list(self.directory.glob('.model-proxy-*')), [])
        self.assertEqual(list(self.directory.glob('*.bak.*')), [])

    def test_runtime_role_in_staging_namespace_is_rejected(self):
        values = dict(DEFAULTS)
        values['log'] = '.runtime/tmp/designated-log'
        self.paths.write_text(json.dumps(values), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            with self.prepared():
                self.save()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.root / '.runtime' / 'tmp' / 'designated-log').exists())

    def test_existing_unsafe_backup_directory_is_not_repaired(self):
        from core.secure_files import SecureSnapshotError

        state = self.root / '.runtime'
        state.mkdir(mode=0o700)
        (state / 'tmp').mkdir(mode=0o700)
        backup = state / 'backups'
        backup.mkdir(mode=0o755)
        backup.chmod(0o755)
        with self.assertRaises(SecureSnapshotError):
            with self.prepared():
                self.save()
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o755)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(list(backup.iterdir()), [])

    def test_runtime_paths_drift_after_authorization_stops_before_write(self):
        from core.mac_config_writer import MacWriteError
        with self.prepared():
            values = dict(DEFAULTS)
            values['log'] = '.runtime/changed-log'
            self.paths.write_text(json.dumps(values), encoding='utf-8')
            with self.assertRaises(MacWriteError):
                self.save()
        self.assertEqual(self.config.read_bytes(), self.original)
        backups = self.root / '.runtime' / 'backups'
        # A refusal before staging preparation must not leave a backup; the
        # directory may legitimately not have been created yet.
        self.assertEqual(sorted(path.name for path in backups.iterdir()) if backups.exists() else [], [])

    def test_config_runtime_collision_after_actual_stage_blocks_commit(self):
        from core import mac_config_writer

        original_open = mac_config_writer._open_private
        def open_and_change(*args, **kwargs):
            result = original_open(*args, **kwargs)
            if args[1].startswith('.model-proxy-config-'):
                values = dict(DEFAULTS)
                values['pid'] = values['lock']
                self.paths.write_text(json.dumps(values), encoding='utf-8')
            return result
        with self.prepared(), \
             mock.patch.object(mac_config_writer, '_open_private', side_effect=open_and_change), \
             self.assertRaises(mac_config_writer.MacWriteError) as caught:
            self.save()
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])
        self.assertEqual([p.read_bytes() for p in (self.root / '.runtime' / 'backups').iterdir()],
                         [self.original])

    def test_install_client_write_uses_same_private_runtime_directories(self):
        from core.secure_files import capture_target, mac_private_protection

        home = self.base / 'home'
        directory = home / '.codex'
        directory.mkdir(parents=True, mode=0o700)
        client = directory / 'config.toml'
        client.write_text('model = "fake-old"\n', encoding='utf-8')
        client.chmod(0o600)
        before = client.read_bytes()
        snapshot = capture_target(client, authorized_root=home,
                                  protection_reader=mac_private_protection, max_bytes=1024)
        token = install._WRITE_CONTEXT.set({'root': self.root, 'config': str(self.config),
                                            'targets': []})
        real_replace = os.replace
        calls = []
        def observe(src, dst, *, src_dir_fd, dst_dir_fd):
            calls.append((os.fstat(src_dir_fd).st_ino, os.fstat(dst_dir_fd).st_ino))
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        try:
            with mock.patch.dict(os.environ, {'HOME': str(home)}), \
                 mock.patch.object(install, 'confirm', return_value=True), \
                 mock.patch('core.secure_files.os.replace', side_effect=observe), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install.preview_confirm_write(
                    client, before.decode(), 'model = "fake-new"\n', 'codex', [],
                    snapshot=snapshot))
        finally:
            install._WRITE_CONTEXT.reset(token)
        stage, backup = self.root / '.runtime' / 'tmp', self.root / '.runtime' / 'backups'
        self.assertEqual(calls, [(stage.stat().st_ino, directory.stat().st_ino)])
        self.assertEqual(client.read_text(), 'model = "fake-new"\n')
        self.assertEqual([path.read_bytes() for path in backup.iterdir()], [before])
        self.assertEqual(list(stage.iterdir()), [])
        self.assertEqual(list(directory.glob('*.bak.*')), [])

    def test_install_cross_volume_refused_without_staging_confirmation(self):
        from core.secure_files import capture_target, mac_private_protection

        home = self.base / 'home'
        home.mkdir(mode=0o700)
        client = home / 'fake.json'
        client.write_text('old', encoding='utf-8')
        client.chmod(0o600)
        snapshot = capture_target(client, authorized_root=home,
                                  protection_reader=mac_private_protection, max_bytes=1024)
        token = install._WRITE_CONTEXT.set({'root': self.root, 'config': str(self.config),
                                            'targets': []})
        try:
            with mock.patch.dict(os.environ, {'HOME': str(home)}), \
                 mock.patch.object(install, 'confirm', side_effect=[True, False]), \
                 mock.patch('core.secure_files._same_volume', return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(install.InstallFailure):
                install.preview_confirm_write(client, 'old', 'new', 'fake', [], snapshot=snapshot)
        finally:
            install._WRITE_CONTEXT.reset(token)
        self.assertEqual(client.read_text(), 'old')
        self.assertEqual(list(home.glob('*.bak.*')), [])
        self.assertEqual(list(home.glob('.model-proxy-*')), [])
        backups = self.root / '.runtime' / 'backups'
        # A refusal before staging preparation must not leave a backup; the
        # directory may legitimately not have been created yet.
        self.assertEqual(sorted(path.name for path in backups.iterdir()) if backups.exists() else [], [])

    def test_install_new_client_uses_no_overwrite_runtime_staging(self):
        from core.secure_files import capture_target, mac_private_protection

        home = self.base / 'home'
        home.mkdir(mode=0o700)
        client = home / 'new.json'
        snapshot = capture_target(client, authorized_root=home,
                                  protection_reader=mac_private_protection, max_bytes=1024)
        token = install._WRITE_CONTEXT.set({'root': self.root, 'config': str(self.config),
                                            'targets': []})
        try:
            with mock.patch.dict(os.environ, {'HOME': str(home)}), \
                 mock.patch.object(install, 'confirm', return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install.preview_confirm_write(client, '', 'new', 'fake', [],
                                                               snapshot=snapshot))
        finally:
            install._WRITE_CONTEXT.reset(token)
        self.assertEqual(client.read_text(), 'new')
        self.assertEqual(stat.S_IMODE(client.stat().st_mode), 0o600)
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])
        backups = self.root / '.runtime' / 'backups'
        # A refusal before staging preparation must not leave a backup; the
        # directory may legitimately not have been created yet.
        self.assertEqual(sorted(path.name for path in backups.iterdir()) if backups.exists() else [], [])
        self.assertEqual(list(home.glob('.model-proxy-*')), [])

    def test_install_runtime_collision_after_actual_stage_blocks_commit(self):
        from core import secure_files

        home = self.base / 'home'
        home.mkdir(mode=0o700)
        client = home / 'fake.json'
        client.write_text('old', encoding='utf-8')
        client.chmod(0o600)
        snapshot = secure_files.capture_target(client, authorized_root=home,
                    protection_reader=secure_files.mac_private_protection, max_bytes=1024)
        token = install._WRITE_CONTEXT.set({'root': self.root, 'config': str(self.config),
                                            'targets': []})
        original_open = secure_files._open_private
        def open_and_change(*args, **kwargs):
            result = original_open(*args, **kwargs)
            if args[1].startswith('.model-proxy-runtime-'):
                values = dict(DEFAULTS)
                values['pid'] = values['lock']
                self.paths.write_text(json.dumps(values), encoding='utf-8')
            return result
        try:
            with mock.patch.dict(os.environ, {'HOME': str(home)}), \
                 mock.patch.object(install, 'confirm', return_value=True), \
                 mock.patch.object(secure_files, '_open_private', side_effect=open_and_change), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(install.InstallFailure):
                install.preview_confirm_write(client, 'old', 'new', 'fake', [], snapshot=snapshot)
        finally:
            install._WRITE_CONTEXT.reset(token)
        self.assertEqual(client.read_text(), 'old')
        self.assertEqual(list((self.root / '.runtime' / 'tmp').iterdir()), [])
        self.assertEqual([p.read_text() for p in (self.root / '.runtime' / 'backups').iterdir()],
                         ['old'])


if __name__ == '__main__':
    unittest.main()
