"""Synthetic Windows path/ACL preflight only; no native Windows claim."""
from dataclasses import replace
import ntpath
import os
from pathlib import PureWindowsPath
import types
import unittest
from unittest.mock import patch

from core import discovery, windows_acl
from core.cli import _install_ops as install
from core.runtime_paths import PATH_KEYS
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_discovery import VirtualFS, PathWinBridge


class CreationACL(VirtualACL):
    def __init__(self, fs, codex):
        super().__init__(fs)
        self.codex = codex.casefold()
        self.allow_file = False
        self.codex_required = []

    def read_observation(self, handle, kind, required):
        obs = super().read_observation(handle, kind, required)
        path = self.api.handles[handle][0]
        if path.casefold() == self.codex:
            self.codex_required.append(required)
            if required & 0x2 and not self.allow_file:
                return replace(obs, access_granted=False, granted=0)
        return obs


class WindowsCatalogPreflight(unittest.TestCase):
    ROOT = r'C:\fixture\project'
    CONFIG = ROOT + r'\config\fake.json'
    RUNTIME_FILE = ROOT + r'\config\runtime_paths.json'
    HOME = r'C:\fixture\home'
    CODEX = HOME + r'\.codex'
    DIRECTORY = CODEX + r'\model-catalogs'
    TARGET = DIRECTORY + r'\model_proxy_catalog.json'

    def setUp(self):
        self.fs = VirtualFS()
        for index, path, directory in (
            (40, self.ROOT + r'\config', True),
            (41, self.CONFIG, False), (42, self.RUNTIME_FILE, False),
            (43, self.HOME, True), (44, self.CODEX, True),
        ):
            self.fs.objects[path] = self.fs.obj(index, directory=directory)
        self.paths = {name: r'C:\fixture\runtime' + '\\' + name for name in PATH_KEYS}
        for index, path in enumerate(self.paths.values(), 100):
            self.fs.objects[path] = self.fs.obj(index)
        self.acl = CreationACL(self.fs, self.CODEX)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        patches = (
            patch.object(discovery, '_windows_path_backend', return_value=self.backend),
            patch('core.cli.dispatch.runtime_paths', side_effect=lambda root: dict(self.paths)),
            patch('core.cli.dispatch.write_preflight', return_value=True),
        )
        self.patches = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def call(self, *, config=None, target=None):
        return install._windows_catalog_directory_preflight(
            {'root': self.ROOT, 'config': config or self.CONFIG},
            self.DIRECTORY, target or self.TARGET)

    def test_add_subdirectory_without_add_file_is_accepted(self):
        self.assertTrue(self.call())
        # Base preflight receives no file-like target for the missing directory.
        self.patches[2].assert_called_once_with(self.ROOT, self.CONFIG, operation='install')
        self.assertTrue(any(value & 0x4 for value in self.acl.codex_required))
        self.assertFalse(any(value & 0x2 for value in self.acl.codex_required))
        self.assertFalse(self.fs.handles)

    def test_directory_creation_uses_add_subdirectory_not_add_file_on_parent(self):
        from core import platform_windows as win
        class FakeWindowsPath(PureWindowsPath):
            @classmethod
            def home(cls):
                return cls(WindowsCatalogPreflight.HOME)
        def mkdir(path, *, user_sid):
            self.fs.objects[path] = self.fs.obj(333, directory=True)
        self.fs.create_private_directory = mkdir
        original_ensure = win.ensure_private_directory
        def ensure(path, *, required_access, trusted_boundary):
            return original_ensure(path, required_access=required_access,
                trusted_boundary=trusted_boundary, backend=self.fs,
                acl_backend=self.acl, paths_backend=self.backend)
        token = install._WRITE_CONTEXT.set({'root': self.ROOT, 'config': self.CONFIG, 'targets': []})
        try:
            fake_os = types.SimpleNamespace(name='nt', path=ntpath, fspath=os.fspath)
            with patch.object(install, 'Path', FakeWindowsPath), patch.object(install, 'os', fake_os), \
                 patch.object(win, 'ensure_private_directory', side_effect=ensure):
                install._create_catalog_directory(FakeWindowsPath(self.DIRECTORY),
                                                  target=FakeWindowsPath(self.TARGET))
        finally:
            install._WRITE_CONTEXT.reset(token)
        self.assertIn(self.DIRECTORY, self.fs.objects)
        self.assertTrue(any(value & 0x4 for value in self.acl.codex_required))
        self.assertFalse(any(value & 0x2 for value in self.acl.codex_required))
        self.assertFalse(self.fs.handles)

    def test_file_or_directory_overlap_with_runtime_or_config_rejected(self):
        self.acl.allow_file = True
        for kind in ('runtime-directory', 'runtime-catalog', 'config-directory', 'config-catalog',
                     'runtime-case-alias', 'config-case-alias'):
            with self.subTest(kind=kind):
                original = self.paths['log']
                config = self.CONFIG
                if kind == 'runtime-directory': self.paths['log'] = self.DIRECTORY
                if kind == 'runtime-catalog': self.paths['log'] = self.TARGET
                if kind == 'config-directory': config = self.DIRECTORY
                if kind == 'config-catalog': config = self.TARGET
                if kind == 'runtime-case-alias': self.paths['log'] = self.CODEX + r'\MODEL-CATALOGS'
                if kind == 'config-case-alias': config = self.CODEX + r'\MODEL-CATALOGS'
                self.assertFalse(self.call(config=config))
                self.assertFalse(self.fs.handles)
                self.paths['log'] = original

    def test_wrong_target_or_reparse_directory_is_rejected(self):
        self.assertFalse(self.call(target=self.CODEX + r'\other.json'))
        self.fs.objects[self.DIRECTORY] = self.fs.obj(220, directory=True)
        self.fs.objects[self.DIRECTORY]['attributes'] |= 0x400
        self.assertFalse(self.call())
        self.assertFalse(self.fs.handles)

    def test_internal_release_unknown_is_reported_not_confused_with_conflict(self):
        for source in ('base_preflight', 'runtime_paths', 'open_preflight'):
            with self.subTest(source=source):
                error = discovery.PreflightError('release_unknown', 'fake release failure')
                where = {'base_preflight': 'core.cli.dispatch.write_preflight',
                         'runtime_paths': 'core.cli.dispatch.runtime_paths',
                         'open_preflight': 'core.discovery._windows_open_preflight'}[source]
                with patch(where, side_effect=error):
                    with self.assertRaises(install.InstallFailure) as caught:
                        self.call()
                self.assertIn('句柄关闭状态未知', str(caught.exception))
                self.assertFalse(self.fs.handles)

    def test_false_base_preflight_retains_conditional_release_unknown_guidance(self):
        with patch('core.cli.dispatch.write_preflight', return_value=False):
            with self.assertRaises(install.InstallFailure) as caught:
                self.call()
        self.assertIn('如前述为 release_unknown', str(caught.exception))
        self.assertIn('句柄关闭状态未知', str(caught.exception))
        self.assertFalse(self.fs.handles)

    def test_uncertain_lease_release_aborts_preflight(self):
        original_close = discovery._windows_close_leases
        def close_then_fail(leases):
            original_close(leases)
            raise discovery.PreflightError('release_unknown', 'fake close failure')
        with patch.object(discovery, '_windows_close_leases', side_effect=close_then_fail):
            with self.assertRaises(install.InstallFailure) as caught:
                self.call()
        self.assertIn('关闭状态未知', str(caught.exception))
        self.assertFalse(self.fs.handles)

    def test_no_add_subdirectory_is_rejected(self):
        original = self.acl.read_observation
        def denied(handle, kind, required):
            obs = original(handle, kind, required)
            if self.fs.handles[handle][0].casefold() == self.CODEX.casefold() and required & 0x4:
                return replace(obs, access_granted=False, granted=0)
            return obs
        self.acl.read_observation = denied
        self.assertFalse(self.call())
        self.assertFalse(self.fs.handles)


if __name__ == '__main__':
    unittest.main()
