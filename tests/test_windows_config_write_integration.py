"""Windows CLI transaction contract over virtual Win32 APIs, not native Windows."""
import contextlib
import io
import json
import os
import pathlib
import types
import unittest
from unittest.mock import patch

from core import windows_secure_files as secure
from core.cli import _config_ops as ops, dispatch
from core.windows_secure_native import ProductWriteBackend
from tests.test_windows_cli_boundary_wiring import WindowsCLIVirtualFixture


class WindowsConfigIntegration(unittest.TestCase):
    def setUp(self):
        self.fixture = WindowsCLIVirtualFixture(self)
        self.fs = self.fixture.fs
        self.events = []
        self.failure = None
        self.control = types.SimpleNamespace(reload=lambda: self.events.append('reload') or 0)
        # Test first-use creation of the actual default .runtime layout.
        del self.fs.objects[self.fixture.state]

    def run_dispatch(self):
        original_commit = ProductWriteBackend.commit
        original_postcheck = ProductWriteBackend.postcheck

        def commit(backend, stage):
            target = backend.snapshot.path
            role = ('config' if target == self.fixture.config else
                    'marker' if target.endswith(r'\reload') and backend.snapshot.exists else None)
            if role is not None:
                self.events.append(role + '-write')
            if role is not None and self.failure == role:
                # Native call may have taken effect even though no receipt follows.
                original_commit(backend, stage)
                raise OSError('synthetic commit outcome unknown')
            return original_commit(backend, stage)

        def postcheck(backend):
            result = original_postcheck(backend)
            if self.failure == 'backup-changed' and backend.snapshot.path == self.fixture.config:
                backup = next(path for path in self.fs.objects
                              if path.startswith(self.fixture.backups + '\\'))
                self.fs.objects[backup]['data'] = b'changed fake backup'
            return result

        def edit(path):
            cfg = ops.load_config(path)
            cfg['routes'].append({'id': 'fake'})
            ops.atomic_write(path, cfg)
            ops.done(True)

        def run_child(args, *, env):
            self.assertFalse(self.fs.locked, 'marker control lock must be released before helper')
            fake_os = types.SimpleNamespace(name='nt', path=self.fixture.path_ops,
                                            environ=env, fspath=os.fspath)
            state_names = ('_WRITE_BASELINE', '_WRITE_STARTED', '_WRITE_OPERATION',
                           '_WRITE_AUTHORIZATION', '_WRITE_PREFLIGHT', '_WRITE_INPUT_PATH',
                           '_WRITE_COMMITTED', '_WRITE_LOCATIONS', '_WRITE_BACKUP_BASELINE',
                           '_MARKER_BASELINE', '_MARKER_CONTENT', '_MARKER_REQUIRED')
            saved = {name: getattr(ops, name) for name in state_names}
            with patch.object(ops, 'os', fake_os), \
                 patch.object(ops, '__file__', self.fixture.root + r'\core\cli\_config_ops.py'), \
                 patch.object(pathlib, 'Path', self.fixture.Path), \
                 patch.multiple(ops, **saved), \
                 patch.object(ops.sys, 'argv', [args[1], args[2], args[3]]):
                try:
                    ops.main()
                    rc = 0
                except SystemExit as exc:
                    rc = int(exc.code or 0)
            return types.SimpleNamespace(returncode=rc)

        output = io.StringIO()
        with patch.object(ops, '_DISPATCH', {'route-add': (edit, 0)}), \
             patch.object(ops, 'confirm', return_value=True), \
             patch.object(ProductWriteBackend, 'commit', commit), \
             patch.object(ProductWriteBackend, 'postcheck', postcheck), \
             patch.object(dispatch.subprocess, 'run', side_effect=run_child), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = dispatch.run_helper('route-add', [], self.fixture.config, self.control,
                                     root=self.fixture.root)
        return rc, output.getvalue()

    def current_config(self):
        return json.loads(self.fs.objects[self.fixture.config]['data'])

    def markers(self):
        return [(path, item['data']) for path, item in self.fs.objects.items()
                if path.endswith(r'\reload')]

    def test_save_before_yes_and_reload(self):
        rc, output = self.run_dispatch()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.events, ['config-write', 'marker-write', 'reload'])
        self.assertEqual(self.current_config()['routes'][0]['id'], 'fake')
        self.assertEqual(self.markers(), [])
        self.assertEqual([content for _, content in self.fixture.cleanup_observations], [b'yes'])
        backups = [path for path in self.fs.objects if path.startswith(self.fixture.backups + '\\')]
        self.assertEqual(len(backups), 1)
        self.assertIn(self.fixture.tmp, self.fs.objects)
        self.assertIn(self.fixture.backups, self.fs.objects)
        self.assertFalse(any(path.startswith(self.fixture.tmp + r'\.model-proxy-stage-')
                             for path in self.fs.objects))
        self.assertFalse(any(path.startswith(self.fixture.root + r'\config\.model-proxy-')
                             for path in self.fs.objects))
        with secure.capture_target(backups[0], authorized_root=self.fixture.backups,
                                   trusted_boundary=self.fixture.root, max_bytes=1024) as snapshot:
            self.assertEqual(snapshot.contents, self.fixture.original)
            snapshot.revalidate()
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        dispatch._cleanup_windows_marker.assert_called_once()

    def test_unknown_config_commit_never_requests_reload(self):
        self.failure = 'config'
        rc, output = self.run_dispatch()
        self.assertEqual(rc, 1)
        self.assertEqual(self.events, ['config-write'])
        self.assertIn('提交结果未知', output)
        self.assertIn('未触发 reload', output)
        self.assertEqual([content for _, content in self.markers()], [b'started'])
        backups = [path for path in self.fs.objects if path.startswith(self.fixture.backups + '\\')]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.fs.objects[backups[0]]['data'], self.fixture.original)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        dispatch._cleanup_windows_marker.assert_not_called()

    def test_verified_save_with_changed_backup_never_requests_reload(self):
        self.failure = 'backup-changed'
        rc, output = self.run_dispatch()
        self.assertEqual(rc, 1)
        self.assertEqual(self.events, ['config-write'])
        self.assertIn('目标曾通过后验', output)
        self.assertIn('整体写入未完成', output)
        self.assertIn('CHANGED', output)
        self.assertIn('未触发 reload', output)
        self.assertEqual(self.current_config()['routes'][0]['id'], 'fake')
        self.assertEqual([content for _, content in self.markers()], [b'started'])
        backups = [path for path in self.fs.objects if path.startswith(self.fixture.backups + '\\')]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.fs.objects[backups[0]]['data'], b'changed fake backup')
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        dispatch._cleanup_windows_marker.assert_not_called()

    def test_marker_failure_reports_saved_but_no_reload(self):
        self.failure = 'marker'
        rc, output = self.run_dispatch()
        self.assertEqual(rc, 1)
        self.assertEqual(self.events, ['config-write', 'marker-write'])
        self.assertIn('配置已保存并验证', output)
        self.assertIn('未触发 reload', output)
        self.assertEqual(self.current_config()['routes'][0]['id'], 'fake')
        # A possibly written yes must not override the failed helper exit.
        self.assertEqual([content for _, content in self.markers()], [b'yes'])
        backups = [path for path in self.fs.objects if path.startswith(self.fixture.backups + '\\')]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.fs.objects[backups[0]]['data'], self.fixture.original)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        dispatch._cleanup_windows_marker.assert_not_called()

    def test_runtime_role_collision_refuses_marker_and_reload(self):
        from core.runtime_paths import DEFAULTS
        values = dict(DEFAULTS)
        values['pid'] = values['lock']
        self.fs.objects[self.fixture.paths_file]['data'] = json.dumps(values).encode()
        rc, output = self.run_dispatch()
        self.assertEqual(rc, 1, output)
        self.assertEqual(self.events, [])
        self.assertEqual(self.current_config()['routes'], [])
        self.assertEqual(self.markers(), [])
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        dispatch._cleanup_windows_marker.assert_not_called()

    def test_invalid_business_config_refuses_marker_and_reload(self):
        self.fs.objects[self.fixture.config]['data'] = b'{'
        rc, output = self.run_dispatch()
        self.assertEqual(rc, 1, output)
        self.assertEqual(self.events, [])
        self.assertEqual(self.markers(), [])
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        dispatch._cleanup_windows_marker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
