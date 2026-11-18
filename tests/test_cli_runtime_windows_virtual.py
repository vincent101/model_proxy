"""Real CLI guards and writer over a synthetic Win32 filesystem, never native."""
import ntpath
import os
import pathlib
from pathlib import Path, PureWindowsPath
import contextlib
import io
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if ROOT != ISOLATION / 'project' or not (ISOLATION / '.model-proxy-isolation').is_file():
    raise RuntimeError('isolated test project required')
if not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISOLATION):
    raise RuntimeError('isolated HOME required')

from core import discovery, platform_windows as win, windows_acl as acl, windows_secure_files as secure
from core.cli import _config_ops as cli, _install_ops as install, dispatch
from core.runtime_paths import DEFAULTS
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL, VirtualCRT
from tests.test_windows_discovery import PathWinBridge
from tests.test_windows_same_volume_commit import MultiVolumeFS


class WindowsDefaultRuntimeVirtual(unittest.TestCase):
    ROOT = r'C:\fixture\project'
    CONFIG = ROOT + r'\config\model_proxy_config.json'
    PATHS = ROOT + r'\config\runtime_paths.json'
    STAGE = ROOT + r'\.runtime\tmp'
    BACKUP = ROOT + r'\.runtime\backups'

    def setUp(self):
        self.fs = MultiVolumeFS()
        self.fs.objects[self.ROOT + r'\config'] = self.fs.obj(501, directory=True)
        self.fs.objects[self.CONFIG] = dict(self.fs.obj(502), data=b'{"fake":"old"}')
        self.fs.objects[self.PATHS] = dict(self.fs.obj(503), data=b'{}')
        self.paths = {key: ntpath.join(self.ROOT, value.replace('/', '\\'))
                      for key, value in DEFAULTS.items()}
        self.acl = VirtualACL(self.fs)
        self.crt = VirtualCRT(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        def create_directory(path, *, user_sid):
            self.fs.objects[path] = self.fs.obj(self.fs.next_index, directory=True)
            self.fs.next_index += 1
        self.fs.create_private_directory = create_directory
        self.fs.open_private_candidate = lambda path, *, original_handle=None, user_sid=None: \
            self.fs.open_file(path, True)
        original_ensure = win.ensure_private_directory
        def ensure(path, *, required_access=0x200A1, trusted_boundary=None):
            return original_ensure(path, required_access=required_access,
                                   trusted_boundary=trusted_boundary, backend=self.fs,
                                   acl_backend=self.acl, paths_backend=self.backend)
        for item in (
            patch.object(os, 'name', 'nt'),
            patch.object(secure, '_windows_path_backend', return_value=self.backend),
            patch.object(discovery, '_windows_path_backend', return_value=self.backend),
            patch.object(win, '_api', side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(win, '_NativeFDAPI', return_value=self.crt),
            patch.object(acl, '_NativeBackend', return_value=self.acl),
            patch.object(win, 'ensure_private_directory', side_effect=ensure),
            patch.object(dispatch, 'runtime_paths', side_effect=lambda root: dict(self.paths)),
            patch.object(dispatch, 'write_preflight', return_value=True),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.close_handles)

    def close_handles(self):
        self.fs.close_fail.clear()
        for handle in list(self.fs.handles):
            self.fs.close(handle)

    def capture(self, path=None, root=None):
        path = path or self.CONFIG
        return secure.capture_target(path, authorized_root=root or self.ROOT,
                                     max_bytes=1024, trusted_boundary=root or self.ROOT)

    def prepare(self, marker=None, targets=()):
        return cli._windows_runtime_locations(self.ROOT, self.CONFIG, 'switch', marker, targets)

    def test_default_directories_real_writer_and_actual_candidate_guard(self):
        snapshot = self.capture()
        try:
            locations = self.prepare()
            self.assertEqual((locations.stage, locations.backup), (self.STAGE, self.BACKUP))
            options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch',
                                                snapshot, None, locations, make_backup=True)
            result = secure.write_target(snapshot, b'{"fake":"new"}', **options)
            self.assertEqual(result.state, 'VERIFIED')
            self.assertTrue(result.backup.startswith(self.BACKUP + '\\'))
            self.assertEqual(self.fs.objects[result.backup]['data'], b'{"fake":"old"}')
            self.assertEqual(self.fs.objects[self.CONFIG]['data'], b'{"fake":"new"}')
            self.assertFalse(any(p.startswith(self.STAGE + r'\.model-proxy-stage-')
                                 for p in self.fs.objects))
            self.assertFalse(any(p.startswith(self.ROOT + r'\config\.model-proxy-')
                                 for p in self.fs.objects))
        finally:
            snapshot.close()

    def test_candidate_hardlink_to_fixed_role_is_rejected(self):
        log = self.paths['log']
        self.fs.objects[log] = dict(self.fs.obj(601), data=b'protected')
        snapshot = self.capture()
        try:
            locations = self.prepare()
            candidate = self.STAGE + r'\.model-proxy-stage-0123456789abcdef01234567'
            self.fs.objects[candidate] = dict(self.fs.objects[log])
            options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch',
                                                snapshot, None, locations, make_backup=True)
            with self.assertRaises(cli.WritePreflightFailed):
                options['artifact_preflight']('stage', candidate)
            self.assertEqual(self.fs.objects[self.CONFIG]['data'], b'{"fake":"old"}')
        finally:
            snapshot.close()

    def test_sidecar_hardlink_to_runtime_role_rejected_before_creation(self):
        log = self.paths['log']
        sidecar = self.ROOT + r'\config\session_overrides.json'
        self.fs.objects[log] = dict(self.fs.obj(602), data=b'fake')
        self.fs.objects[sidecar] = dict(self.fs.objects[log])
        with self.assertRaises(cli.WritePreflightFailed):
            self.prepare()
        self.assertNotIn(self.BACKUP, self.fs.objects)
        self.assertEqual(self.fs.objects[self.CONFIG]['data'], b'{"fake":"old"}')

    def test_unknown_or_wrong_candidate_directory_is_rejected(self):
        snapshot = self.capture()
        try:
            locations = self.prepare()
            options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch',
                                                snapshot, None, locations, make_backup=True)
            self.assertFalse(options['artifact_preflight']('stage',
                                                          self.BACKUP + r'\.model-proxy-stage-fake'))
            self.assertFalse(options['artifact_preflight']('other',
                                                          self.STAGE + r'\.model-proxy-stage-fake'))
        finally:
            snapshot.close()

    def test_changed_staging_directory_is_not_reauthorized(self):
        snapshot = self.capture()
        try:
            locations = self.prepare()
            self.fs.objects[self.STAGE] = self.fs.obj(604, directory=True)
            options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch',
                                                snapshot, None, locations, make_backup=True)
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(snapshot, b'new', **options)
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertFalse(any(p.startswith(self.BACKUP + '\\') for p in self.fs.objects))
            self.assertEqual(self.fs.objects[self.CONFIG]['data'], b'{"fake":"old"}')
        finally:
            snapshot.close()

    def test_changed_runtime_role_is_not_silently_rebased(self):
        snapshot = self.capture()
        try:
            locations = self.prepare()
            self.paths['pid'] = self.ROOT + r'\.runtime\other-pid'
            options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch',
                                                snapshot, None, locations, make_backup=True)
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(snapshot, b'new', **options)
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertFalse(any(p.startswith(self.BACKUP + '\\') for p in self.fs.objects))
        finally:
            snapshot.close()

    def test_cross_volume_refused_before_backup_creation(self):
        external = r'D:\outside'
        target = external + r'\fake.json'
        self.fs.objects[external] = dict(self.fs.obj(702, directory=True), volume=18)
        self.fs.objects[target] = dict(self.fs.obj(703), volume=18, data=b'old')
        snapshot = self.capture(target, external)
        try:
            locations = self.prepare(targets=(target,))
            options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch', snapshot,
                                                None, locations, make_backup=True, targets=(target,))
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(snapshot, b'new', **options)
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertFalse(any(p.startswith(self.BACKUP + '\\') for p in self.fs.objects))
            self.assertEqual(self.fs.objects[target]['data'], b'old')
        finally:
            snapshot.close()

    def test_cross_volume_cli_config_reports_refusal_before_write(self):
        external = r'D:\outside'
        target = external + r'\fake.json'
        self.fs.objects[external] = dict(self.fs.obj(704, directory=True), volume=18)
        self.fs.objects[target] = dict(self.fs.obj(705), volume=18, data=b'{}')
        snapshot = self.capture(target, external)
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, environ=os.environ, fspath=os.fspath)
        try:
            locations = cli._windows_runtime_locations(self.ROOT, target, 'switch', None)
            with patch.object(cli, 'os', fake_os), patch.object(dispatch, 'ROOT', self.ROOT), \
                 patch.object(cli, 'confirm', side_effect=[True, False]), \
                 patch.multiple(cli, _MARKER_REQUIRED=False, _WRITE_BASELINE=snapshot,
                                _WRITE_PREFLIGHT=lambda: True, _WRITE_INPUT_PATH=target,
                                _WRITE_OPERATION='switch', _WRITE_STARTED=False,
                                _WRITE_LOCATIONS=locations, _WRITE_COMMITTED=None), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(cli.WritePreflightFailed) as caught:
                cli.atomic_write(target, {'routes': [], 'strategies': [], 'supplies': []})
            self.assertIn('跨卷', str(caught.exception))
            self.assertFalse(any(p.startswith(self.BACKUP + '\\') for p in self.fs.objects))
            self.assertEqual(self.fs.objects[target]['data'], b'{}')
        finally:
            snapshot.close()

    def test_marker_write_uses_private_stage_without_backup_siblings(self):
        locations = self.prepare()
        directory = self.STAGE + r'\model-proxy-cli-0123456789abcdef0123456789abcdef'
        marker_path = directory + r'\reload'
        self.fs.objects[directory] = self.fs.obj(710, directory=True)
        self.fs.objects[marker_path] = dict(self.fs.obj(711), data=b'started')
        marker = self.capture(marker_path, self.ROOT)
        locations = self.prepare(marker)
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, environ=os.environ, fspath=os.fspath)
        try:
            with patch.object(cli, 'os', fake_os), patch.object(dispatch, 'ROOT', self.ROOT), \
                 patch.object(cli, '_verify_marker_binding', return_value=marker), \
                 patch.multiple(cli, _MARKER_REQUIRED=True, _MARKER_CONTENT=None,
                                _WRITE_LOCATIONS=locations, _WRITE_INPUT_PATH=self.CONFIG,
                                _WRITE_OPERATION='switch', _WRITE_COMMITTED=object()):
                cli._set_marker('yes')
                self.assertEqual(cli._MARKER_CONTENT, 'yes')
            self.assertEqual(self.fs.objects[marker_path]['data'], b'yes')
            self.assertFalse(any(p.startswith(self.BACKUP + '\\') for p in self.fs.objects))
            self.assertEqual([p for p in self.fs.objects if p.startswith(directory + '\\')],
                             [marker_path])
        finally:
            marker.close()

    def test_candidate_alias_of_marker_is_rejected(self):
        locations = self.prepare()
        directory = self.STAGE + r'\model-proxy-cli-0123456789abcdef0123456789abcdef'
        marker_path = directory + r'\reload'
        self.fs.objects[directory] = self.fs.obj(720, directory=True)
        self.fs.objects[marker_path] = dict(self.fs.obj(721), data=b'started')
        marker = self.capture(marker_path, self.ROOT)
        locations = self.prepare(marker)
        snapshot = self.capture()
        try:
            candidate = self.STAGE + r'\.model-proxy-stage-0123456789abcdef01234567'
            self.fs.objects[candidate] = dict(self.fs.objects[marker_path])
            with patch.object(cli, '_verify_marker_binding', return_value=marker):
                options = cli._windows_write_options(self.ROOT, self.CONFIG, 'switch',
                                                    snapshot, marker, locations, make_backup=True)
                with self.assertRaises(cli.WritePreflightFailed):
                    options['artifact_preflight']('stage', candidate)
        finally:
            snapshot.close()
            marker.close()

    def test_cli_atomic_write_holds_backup_during_marker_transaction(self):
        locations = self.prepare()
        directory = self.STAGE + r'\model-proxy-cli-0123456789abcdef0123456789abcdef'
        marker_path = directory + r'\reload'
        self.fs.objects[directory] = self.fs.obj(740, directory=True)
        self.fs.objects[marker_path] = dict(self.fs.obj(741), data=b'started')
        marker = self.capture(marker_path, self.ROOT)
        locations = self.prepare(marker)
        snapshot = self.capture()
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, environ=os.environ, fspath=os.fspath)
        try:
            with patch.object(cli, 'os', fake_os), patch.object(dispatch, 'ROOT', self.ROOT), \
                 patch.object(cli, 'confirm', return_value=True), \
                 patch.object(cli, '_verify_marker_binding', return_value=marker), \
                 patch.multiple(cli, _MARKER_REQUIRED=True, _MARKER_CONTENT=None,
                                _MARKER_BASELINE=marker, _WRITE_BASELINE=snapshot,
                                _WRITE_PREFLIGHT=lambda: True, _WRITE_INPUT_PATH=self.CONFIG,
                                _WRITE_OPERATION='switch', _WRITE_STARTED=False,
                                _WRITE_LOCATIONS=locations, _WRITE_COMMITTED=None,
                                _WRITE_BACKUP_BASELINE=None), \
                 contextlib.redirect_stdout(io.StringIO()):
                cli.atomic_write(self.CONFIG, {'routes': [], 'strategies': [], 'supplies': []})
                self.assertEqual(cli._MARKER_CONTENT, 'yes')
                self.assertIsNone(cli._WRITE_BACKUP_BASELINE)
            self.assertEqual(self.fs.objects[marker_path]['data'], b'yes')
            backups = [p for p in self.fs.objects if p.startswith(self.BACKUP + '\\')]
            self.assertEqual(len(backups), 1)
            self.assertEqual(self.fs.objects[backups[0]]['data'], b'{"fake":"old"}')
            self.assertEqual([p for p in self.fs.objects if p.startswith(directory + '\\')],
                             [marker_path])
        finally:
            marker.close()
            snapshot.close()

    def test_changed_backup_after_target_postcheck_prevents_marker_yes(self):
        locations = self.prepare()
        directory = self.STAGE + r'\model-proxy-cli-0123456789abcdef0123456789abcdef'
        marker_path = directory + r'\reload'
        self.fs.objects[directory] = self.fs.obj(750, directory=True)
        self.fs.objects[marker_path] = dict(self.fs.obj(751), data=b'started')
        marker = self.capture(marker_path, self.ROOT)
        locations = self.prepare(marker)
        snapshot = self.capture()
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, environ=os.environ, fspath=os.fspath)
        original_capture = secure.capture_target
        def capture(path, **kwargs):
            if path.startswith(self.BACKUP + '\\'):
                self.fs.objects[path]['data'] = b'changed-after-target-postcheck'
            return original_capture(path, **kwargs)
        try:
            with patch.object(cli, 'os', fake_os), patch.object(dispatch, 'ROOT', self.ROOT), \
                 patch.object(cli, 'confirm', return_value=True), \
                 patch.object(cli, '_verify_marker_binding', return_value=marker), \
                 patch.object(secure, 'capture_target', side_effect=capture), \
                 patch.multiple(cli, _MARKER_REQUIRED=True, _MARKER_CONTENT=None,
                                _MARKER_BASELINE=marker, _WRITE_BASELINE=snapshot,
                                _WRITE_PREFLIGHT=lambda: True, _WRITE_INPUT_PATH=self.CONFIG,
                                _WRITE_OPERATION='switch', _WRITE_STARTED=False,
                                _WRITE_LOCATIONS=locations, _WRITE_COMMITTED=None,
                                _WRITE_BACKUP_BASELINE=None), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(secure.SecureWriteError) as caught:
                cli.atomic_write(self.CONFIG, {'routes': [], 'strategies': [], 'supplies': []})
            self.assertTrue(caught.exception.target_was_verified)
            self.assertEqual(caught.exception.state, 'UNKNOWN')
            self.assertEqual(caught.exception.backup_status, 'UNKNOWN')
            self.assertEqual(self.fs.objects[marker_path]['data'], b'started')
        finally:
            marker.close()
            snapshot.close()

    def test_install_write_uses_private_directories_and_complete_role_scan(self):
        directory = r'C:\fixture\client'
        target = directory + r'\fake.json'
        self.fs.objects[directory] = self.fs.obj(730, directory=True)
        self.fs.objects[target] = dict(self.fs.obj(731), data=b'old')
        snapshot = self.capture(target, directory)
        token = install._WRITE_CONTEXT.set({'root': self.ROOT, 'config': self.CONFIG, 'targets': []})
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
        try:
            with patch.object(install, 'os', fake_os), patch.object(install, 'Path', PureWindowsPath), \
                 patch.object(install, 'confirm', return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install._preview_confirm_captured(
                    PureWindowsPath(target), 'old', 'new', 'fake', [],
                    snap=snapshot, captured='old', field_changes=None))
            self.assertEqual(self.fs.objects[target]['data'], b'new')
            backups = [p for p in self.fs.objects if p.startswith(self.BACKUP + '\\')]
            self.assertEqual(len(backups), 1)
            self.assertEqual(self.fs.objects[backups[0]]['data'], b'old')
            self.assertFalse(any(p.startswith(directory + r'\.model-proxy-') for p in self.fs.objects))
        finally:
            install._WRITE_CONTEXT.reset(token)
            snapshot.close()

    def test_install_cross_volume_reports_refusal_before_backup(self):
        directory = r'D:\outside'
        target = directory + r'\fake.json'
        self.fs.objects[directory] = dict(self.fs.obj(760, directory=True), volume=18)
        self.fs.objects[target] = dict(self.fs.obj(761), volume=18, data=b'old')
        snapshot = self.capture(target, directory)
        token = install._WRITE_CONTEXT.set({'root': self.ROOT, 'config': self.CONFIG, 'targets': []})
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
        try:
            with patch.object(install, 'os', fake_os), patch.object(install, 'Path', PureWindowsPath), \
                 patch.object(install, 'confirm', side_effect=[True, False]), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(install.InstallFailure) as caught:
                install._preview_confirm_captured(PureWindowsPath(target), 'old', 'new', 'fake', [],
                                                  snap=snapshot, captured='old', field_changes=None)
            self.assertIn('跨卷', str(caught.exception))
            self.assertFalse(any(p.startswith(self.BACKUP + '\\') for p in self.fs.objects))
            self.assertEqual(self.fs.objects[target]['data'], b'old')
        finally:
            install._WRITE_CONTEXT.reset(token)
            snapshot.close()

    def test_dispatch_initial_marker_uses_real_lock_and_role_guard(self):
        from core import platform_lock

        original_write = secure.write_target
        guard_calls = []
        def observe(snapshot, payload, **options):
            original_guard = options['artifact_preflight']
            def checked(role, path):
                self.assertTrue(self.fs.locked)
                guard_calls.append(role)
                return original_guard(role, path)
            options['artifact_preflight'] = checked
            return original_write(snapshot, payload, **options)
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(secure, 'write_target', side_effect=observe), \
             patch.object(dispatch, '_cleanup_windows_marker') as cleanup:
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG) as holder:
                self.assertEqual(self.fs.objects[holder.path]['data'], b'started')
                self.assertFalse(self.fs.locked)
                self.assertTrue(any(call[0] == 'lock' for call in self.fs.calls))
                self.assertTrue(any(call[0] == 'unlock' for call in self.fs.calls))
                self.assertFalse(any(p.startswith(self.STAGE + r'\.model-proxy-stage-')
                                     for p in self.fs.objects))
                holder.cleanup_ok = True
            cleanup.assert_called_once()
        self.assertGreaterEqual(guard_calls.count('stage'), 2)

    def test_dispatch_marker_lock_conflict_cannot_create_or_publish(self):
        from core import platform_lock

        control = self.ROOT + r'\.runtime\control.lock'
        self.fs.objects[control] = self.fs.obj(780)
        self.fs.locked.add(780)
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             self.assertRaises(platform_lock.LockError):
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('lock conflict must not publish a marker')
        self.assertFalse(any('model-proxy-cli-' in p for p in self.fs.objects))

    def test_dispatch_marker_stage_alias_to_runtime_role_is_rejected(self):
        from core import platform_lock
        from core.windows_secure_native import ProductWriteBackend

        log = self.paths['log']
        self.fs.objects[log] = dict(self.fs.obj(781), data=b'fake-protected')
        original = ProductWriteBackend.reserve
        def collide(backend, role):
            path = original(backend, role)
            if role == 'stage':
                self.fs.objects[path] = dict(self.fs.objects[log])
            return path
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(ProductWriteBackend, 'reserve', collide), \
             contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(secure.SecureWriteError) as caught:
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('colliding marker stage cannot be published')
        self.assertEqual(caught.exception.code, 'artifact_preflight_failed')
        self.assertFalse(self.fs.locked)
        self.assertFalse(any(p.endswith(r'\reload') for p in self.fs.objects))
        self.assertEqual(self.fs.objects[log]['data'], b'fake-protected')

    def test_dispatch_marker_replaced_control_lock_blocks_write(self):
        from core import platform_lock
        from core.windows_secure_native import ProductWriteBackend

        control = self.ROOT + r'\.runtime\control.lock'
        original = ProductWriteBackend.reserve
        def replace_lock(backend, role):
            path = original(backend, role)
            if role == 'stage':
                self.fs.objects[control] = self.fs.obj(782)
            return path
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(ProductWriteBackend, 'reserve', replace_lock), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(secure.SecureWriteError):
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('changed lock cannot authorize a marker')
        self.assertFalse(self.fs.locked)
        self.assertFalse(any(p.endswith(r'\reload') for p in self.fs.objects))

    def test_dispatch_marker_parent_acl_change_blocks_write(self):
        from core import platform_lock
        from core.windows_secure_native import ProductWriteBackend

        original = ProductWriteBackend.reserve
        def change_parent(backend, role):
            path = original(backend, role)
            if role == 'stage':
                self.fs.objects[ntpath.dirname(backend.snapshot.path)]['unsafe_group'] = True
            return path
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(ProductWriteBackend, 'reserve', change_parent), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(secure.SecureWriteError):
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('changed parent cannot authorize a marker')
        self.assertFalse(self.fs.locked)
        self.assertFalse(any(p.endswith(r'\reload') for p in self.fs.objects))

    def test_dispatch_unknown_control_lock_release_never_publishes(self):
        from core import platform_lock
        from core.windows_secure_native import ProductWriteBackend

        original = ProductWriteBackend.commit
        def fail_release(backend, stage):
            result = original(backend, stage)
            self.crt.fail_close = True
            return result
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(ProductWriteBackend, 'commit', fail_release), \
             contextlib.redirect_stderr(io.StringIO()) as output, \
             self.assertRaises(platform_lock.LockError):
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('unknown lock release must not publish the marker')
        self.assertIn('retained', output.getvalue())
        self.assertTrue(any(p.endswith(r'\reload') and data.get('data') == b'started'
                            for p, data in self.fs.objects.items()))

    def test_dispatch_unknown_marker_directory_creation_never_writes(self):
        from core import platform_lock

        original_ensure = win.ensure_private_directory
        def uncertain(path, **kwargs):
            if 'model-proxy-cli-' in path:
                self.fs.objects[path] = self.fs.obj(790, directory=True)
                raise win.WindowsPrivateCreationError('release_unknown', created=True,
                                                      release_unknown=True)
            return original_ensure(path, **kwargs)
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(win, 'ensure_private_directory', side_effect=uncertain), \
             self.assertRaises(win.WindowsPrivateCreationError) as caught:
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('unconfirmed marker directory cannot be used')
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(self.fs.locked)
        self.assertFalse(any(p.endswith(r'\reload') for p in self.fs.objects))

    def test_dispatch_unknown_marker_writer_release_retains_marker_without_yield(self):
        from core import platform_lock
        from core.windows_secure_native import ProductWriteBackend

        original_finish = ProductWriteBackend.finish
        def uncertain(backend):
            original_finish(backend)
            if backend.snapshot.path.endswith(r'\reload') and not backend.snapshot.exists:
                raise OSError('synthetic release failure')
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', PureWindowsPath), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(ProductWriteBackend, 'finish', uncertain), \
             contextlib.redirect_stderr(io.StringIO()) as output, \
             self.assertRaises(secure.SecureWriteError) as caught:
            with dispatch._windows_marker_scope(self.ROOT, self.CONFIG):
                self.fail('unconfirmed marker cannot be published')
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertFalse(self.fs.locked)
        self.assertIn('retained', output.getvalue())

    def run_fake_dispatch_contract(self, failure=None):
        """Exercise the actual dispatcher/helper/writer on the virtual filesystem."""
        from core import platform_lock
        from core.windows_secure_native import ProductWriteBackend

        class FakeWindowsPath(PureWindowsPath):
            def resolve(self, *args, **kwargs):
                return self

        events = []
        original_commit = ProductWriteBackend.commit
        original_postcheck = ProductWriteBackend.postcheck
        def commit(backend, stage):
            target = backend.snapshot.path
            role = ('config' if target == self.CONFIG else
                    'marker' if target.endswith(r'\reload') and backend.snapshot.exists else None)
            if role:
                events.append(role + '-write')
            if role is not None and role == failure:
                original_commit(backend, stage)
                raise RuntimeError('synthetic commit outcome unknown')
            return original_commit(backend, stage)
        def postcheck(backend):
            result = original_postcheck(backend)
            if failure == 'backup-changed' and backend.snapshot.path == self.CONFIG:
                path = next(p for p in self.fs.objects if p.startswith(self.BACKUP + '\\'))
                self.fs.objects[path]['data'] = b'changed fake backup'
            return result
        def edit(path):
            cfg = cli.load_config(path)
            cfg.setdefault('routes', []).append({'id': 'fake'})
            cli.atomic_write(path, cfg)
            cli.done(True)
        def run_child(args, *, env):
            self.assertFalse(self.fs.locked, 'initial control lock must be released before helper')
            fake_os = types.SimpleNamespace(name='nt', path=ntpath, environ=env, fspath=os.fspath)
            with patch.object(cli, 'os', fake_os), \
                 patch.object(cli, '__file__', self.ROOT + r'\core\cli\_config_ops.py'), \
                 patch.object(pathlib, 'Path', FakeWindowsPath), \
                 patch.object(cli.sys, 'argv', [args[1], args[2], args[3]]):
                try:
                    cli.main()
                    rc = 0
                except SystemExit as exc:
                    rc = int(exc.code or 0)
            return types.SimpleNamespace(returncode=rc)
        output = io.StringIO()
        control = types.SimpleNamespace(reload=lambda: events.append('reload') or 0)
        with patch.object(platform_lock, '_windows_release_unknown', False), \
             patch.object(dispatch, 'Path', FakeWindowsPath), \
             patch.object(dispatch, 'ROOT', self.ROOT), \
             patch.object(dispatch, 'read_runtime_paths', return_value=dict(self.paths)), \
             patch.object(dispatch.os.path, 'isfile', return_value=True), \
             patch.object(sys, 'platform', 'win32'), \
             patch.object(cli, '_DISPATCH', {'route-add': (edit, 0)}), \
             patch.object(cli, 'confirm', return_value=True), \
             patch.object(ProductWriteBackend, 'commit', commit), \
             patch.object(ProductWriteBackend, 'postcheck', postcheck), \
             patch.object(dispatch.subprocess, 'run', side_effect=run_child), \
             patch.object(dispatch, '_cleanup_windows_marker'), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = dispatch.run_helper('route-add', [], self.CONFIG, control, root=self.ROOT)
        return rc, events, output.getvalue()

    def test_dispatch_contract_save_then_marker_then_reload(self):
        rc, events, output = self.run_fake_dispatch_contract()
        self.assertEqual(rc, 0, output)
        self.assertEqual(events, ['config-write', 'marker-write', 'reload'])
        self.assertIn(b'"id": "fake"', self.fs.objects[self.CONFIG]['data'])

    def test_dispatch_contract_unknown_config_never_reloads(self):
        rc, events, output = self.run_fake_dispatch_contract('config')
        self.assertEqual(rc, 1, output)
        self.assertEqual(events, ['config-write'])
        self.assertIn('提交结果未知', output)

    def test_dispatch_contract_changed_backup_never_reloads(self):
        rc, events, output = self.run_fake_dispatch_contract('backup-changed')
        self.assertEqual(rc, 1, output)
        self.assertEqual(events, ['config-write'])
        self.assertIn('曾通过后验', output)
        self.assertIn('整体写入未完成', output)

    def test_dispatch_contract_marker_unknown_never_reloads(self):
        rc, events, output = self.run_fake_dispatch_contract('marker')
        self.assertEqual(rc, 1, output)
        self.assertEqual(events, ['config-write', 'marker-write'])
        self.assertIn('未触发 reload', output)


if __name__ == '__main__':
    unittest.main()
