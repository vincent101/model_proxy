"""Offline CLI follow-up tests; run only from a disposable isolation copy."""
import contextlib
import io
import ntpath
import os
from pathlib import Path, PureWindowsPath
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ISOLATION = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if ROOT != ISOLATION / 'project' or not (ISOLATION / '.model-proxy-isolation').is_file():
    raise RuntimeError('isolated test project required')
if not Path(os.environ.get('HOME', '/invalid')).is_relative_to(ISOLATION):
    raise RuntimeError('isolated HOME required')

from core.cli import _config_ops as config, _install_ops as install
from core import windows_secure_files as secure


class MarkerCloseTests(unittest.TestCase):
    def run_close_interruption(self, *, mutation):
        proof = object()
        with patch.multiple(config, _MARKER_REQUIRED=True, _MARKER_BASELINE=None,
                            _MARKER_CONTENT=None, _WRITE_BASELINE=object()), \
             patch.object(config, '_verify_marker_binding', return_value=(111, proof)), \
             patch('core.cli.dispatch._pipe_endpoint_protection', return_value=proof), \
             patch('core.mac_config_path.verify_config',
                   side_effect=None if mutation else RuntimeError('before write')), \
             patch.object(config.os, 'dup', return_value=222), \
             patch.object(config.os, 'get_inheritable', return_value=False), \
             patch.object(config.os, 'write', side_effect=lambda fd, data: len(data)), \
             patch.object(config.os, 'close', side_effect=KeyboardInterrupt):
            with self.assertRaises(config.MarkerSafetyError) as caught:
                config._set_marker('writing')
        self.assertTrue(caught.exception.close_unknown)
        self.assertEqual(caught.exception.marker_unknown, mutation)

    def test_interrupted_close_after_marker_write_is_unknown(self):
        self.run_close_interruption(mutation=True)

    def test_interrupted_close_before_marker_write_is_not_mutation(self):
        self.run_close_interruption(mutation=False)


class HistoricalTargetTests(unittest.TestCase):
    def test_config_reports_historical_postcheck_not_success(self):
        error = secure.SecureWriteError('backup_changed', state='UNKNOWN',
                                        backup_status='CHANGED', target_was_verified=True)
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['helper', 'supply-add', 'fake.json']), \
             patch.object(config, '_precheck_inherited_marker'), \
             patch.object(config, '_prepare_write', side_effect=error), \
             contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            config.main()
        text = stderr.getvalue()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('曾通过后验', text)
        self.assertIn('整体写入未完成', text)
        self.assertIn('未触发 reload', text)
        self.assertNotIn('后验失败', text)

    def test_config_still_reports_actual_postcheck_failure(self):
        error = secure.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                        target_was_verified=False)
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['helper', 'supply-add', 'fake.json']), \
             patch.object(config, '_precheck_inherited_marker'), \
             patch.object(config, '_prepare_write', side_effect=error), \
             contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            config.main()
        self.assertIn('后验失败', stderr.getvalue())
        self.assertNotIn('曾通过后验', stderr.getvalue())

    def test_install_reports_historical_postcheck_as_unknown(self):
        target = ISOLATION / 'fake-client.json'
        snapshot = types.SimpleNamespace(path=str(target), contents=b'old', revalidate=lambda: None)
        error = secure.SecureWriteError('backup_changed', state='UNKNOWN',
                                        backup_status='CHANGED', target_was_verified=True)
        token = install._WRITE_CONTEXT.set({'root': ISOLATION, 'config': 'fake',
                                            'targets': []})
        try:
            output = io.StringIO()
            fake_os = types.SimpleNamespace(name='nt', path=os.path, fspath=os.fspath)
            install._EVENTS.clear()
            with patch.object(install, 'os', fake_os), \
                 patch.object(install, 'confirm', return_value=True), \
                 patch('core.cli.dispatch.write_preflight', return_value=True), \
                 patch.object(install, '_windows_runtime_locations', return_value=('stage', 'backup')), \
                 patch('core.cli._config_ops._windows_cross_volume', return_value=False), \
                 patch.object(install, '_windows_write_options', return_value={'preflight': lambda: True}), \
                 patch.object(secure, 'write_target', side_effect=error), \
                 contextlib.redirect_stdout(output), self.assertRaises(install.InstallFailure):
                install._preview_confirm_captured(target, 'old', 'new', 'fake', [],
                                                  snap=snapshot, captured='old', field_changes=None)
            self.assertIn('曾通过后验', output.getvalue())
            self.assertIn('整体安装未完成', output.getvalue())
            self.assertNotIn('后验验证失败', output.getvalue())
            self.assertEqual(install._EVENTS[-1][1], 'UNKNOWN_AFTER_TARGET_VERIFIED')
        finally:
            install._WRITE_CONTEXT.reset(token)

    def test_install_still_reports_actual_postcheck_failure(self):
        target = ISOLATION / 'fake-client.json'
        snapshot = types.SimpleNamespace(path=str(target), contents=b'old', revalidate=lambda: None)
        error = secure.SecureWriteError('postcheck_failed', state='POSTCHECK_FAILED',
                                        target_was_verified=False)
        token = install._WRITE_CONTEXT.set({'root': ISOLATION, 'config': 'fake',
                                            'targets': []})
        try:
            output = io.StringIO()
            with patch.object(install, 'os', types.SimpleNamespace(name='nt', path=os.path, fspath=os.fspath)), \
                 patch.object(install, 'confirm', return_value=True), \
                 patch('core.cli.dispatch.write_preflight', return_value=True), \
                 patch.object(install, '_windows_runtime_locations', return_value=('stage', 'backup')), \
                 patch('core.cli._config_ops._windows_cross_volume', return_value=False), \
                 patch.object(install, '_windows_write_options', return_value={'preflight': lambda: True}), \
                 patch.object(secure, 'write_target', side_effect=error), \
                 contextlib.redirect_stdout(output), self.assertRaises(install.InstallFailure):
                install._preview_confirm_captured(target, 'old', 'new', 'fake', [],
                                                  snap=snapshot, captured='old', field_changes=None)
            self.assertIn('后验验证失败', output.getvalue())
            self.assertNotIn('曾通过后验', output.getvalue())
            self.assertEqual(install._EVENTS[-1][1], 'POSTCHECK_FAILED')
        finally:
            install._WRITE_CONTEXT.reset(token)


class CatalogBoundaryTests(unittest.TestCase):
    HOME = r'C:\fixture\home'
    DIRECTORY = HOME + r'\.codex\model-catalogs'
    BOUNDARY = HOME + r'\.codex'

    def test_creation_uses_boundary_only_after_authorization_preflight(self):
        class FakeWindowsPath(PureWindowsPath):
            @classmethod
            def home(cls):
                return cls(CatalogBoundaryTests.HOME)
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
        token = install._WRITE_CONTEXT.set({'root': r'C:\fixture\project',
                                            'config': 'fake', 'targets': []})
        try:
            with patch.object(install, 'Path', FakeWindowsPath), \
                 patch.object(install, 'os', fake_os), \
                 patch.object(install, '_windows_catalog_directory_preflight', return_value=True) as preflight, \
                 patch('core.discovery._windows_trusted_boundary', return_value=self.BOUNDARY), \
                 patch('core.platform_windows.ensure_private_directory') as create:
                install._create_catalog_directory(FakeWindowsPath(self.DIRECTORY))
                preflight.assert_called_once()
                create.assert_called_once_with(self.DIRECTORY, required_access=0x200A3,
                                               trusted_boundary=self.BOUNDARY)
        finally:
            install._WRITE_CONTEXT.reset(token)

    def test_snapshot_uses_boundary_in_addition_to_authorized_root(self):
        target = ISOLATION / 'fake-client.json'
        snapshot = types.SimpleNamespace(contents=b'fake', close=lambda: None)
        fake_os = types.SimpleNamespace(name='nt', path=os.path)
        with contextlib.ExitStack() as stack:
            token = install._WRITE_CONTEXT.set({'root': ISOLATION, 'config': 'fake',
                                                'targets': [], 'snapshots': stack})
            try:
                with patch.object(install, 'os', fake_os), \
                     patch.object(install, '_root_for', return_value=ISOLATION / 'home'), \
                     patch('core.cli.dispatch.write_preflight', return_value=True), \
                     patch('core.discovery._windows_trusted_boundary',
                           return_value='fake-parent-boundary'), \
                     patch.object(secure, 'capture_target', return_value=snapshot) as capture:
                    actual, text = install._snapshot_text(target)
                    self.assertIs(actual, snapshot)
                    self.assertEqual(text, 'fake')
                    capture.assert_called_once_with(str(target), authorized_root=str(ISOLATION / 'home'),
                                                    max_bytes=install._MAX_CLIENT_BYTES,
                                                    trusted_boundary='fake-parent-boundary')
            finally:
                install._WRITE_CONTEXT.reset(token)

    def test_boundary_does_not_override_failed_authorization_preflight(self):
        class FakeWindowsPath(PureWindowsPath):
            @classmethod
            def home(cls):
                return cls(CatalogBoundaryTests.HOME)
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
        token = install._WRITE_CONTEXT.set({'root': r'C:\fixture\project',
                                            'config': 'fake', 'targets': []})
        try:
            with patch.object(install, 'Path', FakeWindowsPath), \
                 patch.object(install, 'os', fake_os), \
                 patch.object(install, '_windows_catalog_directory_preflight', return_value=False), \
                 patch('core.platform_windows.ensure_private_directory') as create, \
                 self.assertRaises(install.InstallFailure):
                install._create_catalog_directory(FakeWindowsPath(self.DIRECTORY))
            create.assert_not_called()
        finally:
            install._WRITE_CONTEXT.reset(token)

    def test_ambiguous_creation_boundary_fails_closed(self):
        from core.discovery import PreflightError
        class FakeWindowsPath(PureWindowsPath):
            @classmethod
            def home(cls):
                return cls(CatalogBoundaryTests.HOME)
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
        token = install._WRITE_CONTEXT.set({'root': r'C:\fixture\project',
                                            'config': 'fake', 'targets': []})
        try:
            with patch.object(install, 'Path', FakeWindowsPath), \
                 patch.object(install, 'os', fake_os), \
                 patch.object(install, '_windows_catalog_directory_preflight', return_value=True), \
                 patch('core.discovery._windows_trusted_boundary',
                       side_effect=PreflightError('unknown', 'fake')), \
                 patch('core.platform_windows.ensure_private_directory') as create, \
                 self.assertRaises(install.InstallFailure):
                install._create_catalog_directory(FakeWindowsPath(self.DIRECTORY))
            create.assert_not_called()
        finally:
            install._WRITE_CONTEXT.reset(token)


class CatalogVirtualCreationTests(unittest.TestCase):
    def test_private_create_with_real_boundary_primitive_on_virtual_windows(self):
        from core import platform_windows as win
        from tests import test_windows_catalog_preflight as fixture

        case = fixture.WindowsCatalogPreflight('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        class FakeWindowsPath(PureWindowsPath):
            @classmethod
            def home(cls):
                return cls(case.HOME)

        case.fs.create_private_directory = (
            lambda path, *, user_sid: case.fs.objects.__setitem__(
                path, case.fs.obj(333, directory=True)))
        original = win.ensure_private_directory
        def ensure(path, *, required_access, trusted_boundary):
            self.assertEqual(trusted_boundary, case.CODEX)
            return original(path, required_access=required_access,
                            trusted_boundary=trusted_boundary, backend=case.fs,
                            acl_backend=case.acl, paths_backend=case.backend)

        token = install._WRITE_CONTEXT.set({'root': case.ROOT, 'config': case.CONFIG,
                                            'targets': []})
        try:
            fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
            with patch.object(install, 'Path', FakeWindowsPath), \
                 patch.object(install, 'os', fake_os), \
                 patch.object(win, 'ensure_private_directory', side_effect=ensure):
                install._create_catalog_directory(FakeWindowsPath(case.DIRECTORY),
                                                  target=FakeWindowsPath(case.TARGET))
            self.assertIn(case.DIRECTORY, case.fs.objects)
            self.assertTrue(any(value & 0x4 for value in case.acl.codex_required))
            self.assertFalse(any(value & 0x2 for value in case.acl.codex_required))
            self.assertFalse(case.fs.handles)
        finally:
            install._WRITE_CONTEXT.reset(token)

    def test_prior_install_target_inside_catalog_directory_is_rejected(self):
        from tests import test_windows_catalog_preflight as fixture

        case = fixture.WindowsCatalogPreflight('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        context = {'root': case.ROOT, 'config': case.CONFIG,
                   'targets': [('earlier', case.DIRECTORY + r'\other.json')]}
        self.assertFalse(install._windows_catalog_directory_preflight(
            context, case.DIRECTORY, case.TARGET))
        self.assertFalse(case.fs.handles)


if __name__ == '__main__':
    unittest.main()
