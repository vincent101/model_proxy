"""Targeted fake-HOME regressions for marker binding and external staging."""
import contextlib
import io
import json
import os
from pathlib import Path
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

from core.cli import _config_ops as ops, _install_ops as install, dispatch
from tests.test_windows_cli_boundary_wiring import WindowsCLIVirtualFixture
from tests.test_cli_runtime_wiring import MacDefaultRuntimeWiring


class MarkerReplacementReproduction(unittest.TestCase):
    def setUp(self):
        self.fixture = WindowsCLIVirtualFixture(self)
        self.fs = self.fixture.fs

    def test_original_no_replaced_parent_fake_yes_must_not_reload(self):
        events = []
        def child(args, *, env):
            marker = env['CONFIG_OPS_RELOAD_MARKER']
            parent = self.fixture.path_ops.dirname(marker)
            self.fs.objects[marker]['data'] = b'no'
            self.fs.objects[parent] = self.fs.obj(self.fs.next_index, directory=True)
            self.fs.next_index += 1
            self.fs.objects[marker] = dict(self.fs.obj(self.fs.next_index), data=b'yes')
            self.fs.next_index += 1
            return types.SimpleNamespace(returncode=0)
        control = types.SimpleNamespace(reload=lambda: events.append('reload') or 0)
        output = io.StringIO()
        with patch.object(dispatch.subprocess, 'run', side_effect=child), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = dispatch.run_helper('route-add', [], self.fixture.config, control,
                                     root=self.fixture.root)
        self.assertEqual(rc, 1, output.getvalue())
        self.assertEqual(events, [])
        self.assertIn('marker', output.getvalue().lower())


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS ACL fixture required')
class MacExternalCrossVolumeReproduction(unittest.TestCase):
    def setUp(self):
        self.case = MacDefaultRuntimeWiring('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.home = self.case.base / 'fake-home'
        self.home.mkdir(mode=0o700)
        self.target = self.home / 'external.json'
        self.target.write_bytes(self.case.original)
        self.target.chmod(0o600)
        self.case.config.unlink()
        self.case.config.symlink_to(self.target)

    def test_external_confirmation_describes_runtime_and_cross_volume(self):
        output = io.StringIO()
        with patch('core.cli.authority._require_same_terminal'), \
             patch.object(sys, 'stdin', io.StringIO('YES\n')), \
             contextlib.redirect_stdout(output):
            with self.case.prepared():
                pass
        message = output.getvalue()
        self.assertIn('.runtime/backups', message)
        self.assertIn('.runtime/tmp', message)
        self.assertIn('跨', message)

    def test_external_link_cross_volume_authorized_write(self):
        with patch('core.cli.authority._require_same_terminal'), \
             patch.object(sys, 'stdin', io.StringIO('YES\n')), \
             patch('core.mac_config_writer._same_volume', return_value=False), \
             patch.dict(os.environ, {'HOME': str(self.home)}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.case.prepared():
                self.case.save()
        self.assertEqual(json.loads(self.target.read_text())['fake_test_value'], 'updated')
        self.assertTrue(self.case.config.is_symlink())
        self.assertEqual([p.read_bytes() for p in (self.case.root / '.runtime' / 'backups').iterdir()],
                         [self.case.original])
        self.assertEqual(list(self.home.glob('*.bak.*')), [])
        self.assertEqual(list(self.home.glob('.model-proxy-*')), [])

    def test_external_install_cross_volume_authorized_write(self):
        from core.secure_files import capture_target, mac_private_protection
        client = self.home / 'client.json'
        client.write_text('old', encoding='utf-8')
        client.chmod(0o600)
        snapshot = capture_target(client, authorized_root=self.home,
                                  protection_reader=mac_private_protection, max_bytes=1024)
        # Business config itself is internal for this independent install case.
        self.case.config.unlink()
        self.case.config.write_bytes(self.case.original)
        self.case.config.chmod(0o600)
        token = install._WRITE_CONTEXT.set({'root': self.case.root, 'config': str(self.case.config),
                                            'targets': []})
        try:
            with patch.dict(os.environ, {'HOME': str(self.home)}), \
                 patch.object(install, 'confirm', return_value=True), \
                 patch('core.secure_files._same_volume', return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install.preview_confirm_write(
                    client, 'old', 'new', 'fake', [], snapshot=snapshot))
        finally:
            install._WRITE_CONTEXT.reset(token)
        self.assertEqual(client.read_text(), 'new')
        self.assertEqual([p.read_text() for p in (self.case.root / '.runtime' / 'backups').iterdir()],
                         ['old'])
        self.assertEqual(list(self.home.glob('*.bak.*')), [])
        self.assertEqual(list(self.home.glob('.model-proxy-*')), [])


class WindowsExternalCrossVolumeReproduction(unittest.TestCase):
    def setUp(self):
        self.fixture = WindowsCLIVirtualFixture(self)
        self.fs = self.fixture.fs
        self.external = r'D:\fake-home'
        self.fs.objects[self.external] = dict(self.fs.obj(804, directory=True), volume=18)
        self.target = self.external + r'\external.json'
        self.fs.objects[self.target] = dict(self.fs.obj(805), volume=18,
                                            data=b'{"routes": [], "strategies": [], "supplies": []}')

    def test_external_config_cross_volume_authorized_write(self):
        fake_os = types.SimpleNamespace(name='nt', path=self.fixture.path_ops,
                                        environ=os.environ, fspath=os.fspath)
        saved = {'_WRITE_BASELINE': None, '_WRITE_STARTED': False,
                 '_WRITE_OPERATION': 'route-add', '_WRITE_AUTHORIZATION': None,
                 '_WRITE_PREFLIGHT': None, '_WRITE_INPUT_PATH': None,
                 '_WRITE_COMMITTED': None, '_WRITE_LOCATIONS': None,
                 '_WRITE_BACKUP_BASELINE': None, '_MARKER_BASELINE': None,
                 '_MARKER_CONTENT': None, '_MARKER_REQUIRED': False}
        with patch.object(ops, 'os', fake_os), patch.multiple(ops, **saved), \
             patch.object(ops, 'confirm', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            ops._prepare_write(self.target, 'route-add')
            ops.atomic_write(self.target, {'routes': [{'id': 'fake'}],
                                           'strategies': [], 'supplies': []})
        self.assertEqual(json.loads(self.fs.objects[self.target]['data'])['routes'][0]['id'], 'fake')
        backups = [path for path in self.fs.objects if path.startswith(self.fixture.backups + '\\')]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.fs.objects[backups[0]]['data'],
                         b'{"routes": [], "strategies": [], "supplies": []}')
        self.assertFalse(any(path.startswith(self.external + r'\.model-proxy-') for path in self.fs.objects))

    def test_external_install_cross_volume_authorized_write(self):
        class FakeHomePath(self.fixture.Path):
            @classmethod
            def home(inner):
                return inner(self.external)
        fake_os = types.SimpleNamespace(name='nt', path=self.fixture.path_ops,
                                        environ=os.environ, fspath=os.fspath)
        token = install._WRITE_CONTEXT.set({'root': self.fixture.root,
                                            'config': self.fixture.config, 'targets': []})
        try:
            with contextlib.ExitStack() as stack, patch.object(install, 'Path', FakeHomePath), \
                 patch.object(install, 'os', fake_os), patch.object(install, 'confirm', return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                install._WRITE_CONTEXT.get()['snapshots'] = stack
                snap, old = install._snapshot_text(FakeHomePath(self.target))
                self.assertTrue(install.preview_confirm_write(
                    FakeHomePath(self.target), old, '{"fake":"new"}', 'fake', [], snapshot=snap))
        finally:
            install._WRITE_CONTEXT.reset(token)
        self.assertEqual(self.fs.objects[self.target]['data'], b'{"fake":"new"}')
        backups = [path for path in self.fs.objects if path.startswith(self.fixture.backups + '\\')]
        self.assertEqual(len(backups), 1)
        self.assertFalse(any(path.startswith(self.external + r'\.model-proxy-') for path in self.fs.objects))


if __name__ == '__main__':
    unittest.main()
