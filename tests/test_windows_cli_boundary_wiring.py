"""Windows CLI trust-boundary wiring, entirely synthetic; no real config or OS handles."""
import contextlib
import io
import json
import ntpath
import os
import sys
import threading
import unittest
from pathlib import PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import discovery, platform_lock, platform_windows, runtime_paths as runtime_module
from core import windows_acl, windows_paths
from core import windows_secure_files as secure
from core.cli import dispatch, windows_control as control_module
from core.runtime_paths import PATH_KEYS, DEFAULTS
from tests import test_windows_discovery as discovery_fixtures
from tests.test_windows_adapter import VirtualACL, VirtualCRT
from tests.test_windows_same_volume_commit import MultiVolumeFS


class DirectLockBoundaryTests(unittest.TestCase):
    def setUp(self):
        fixture = discovery_fixtures.WindowsDiscoveryLockTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.fs, self.root, self.state = fixture.fs, fixture.ROOT, fixture.STATE
        self.external = fixture.RUNTIME
        self.fs.objects['C:\\']['unsafe_group'] = True
        self.fs.objects[r'C:\fixture']['unsafe_group'] = True
        self.paths = {key: self.state + '\\' + key for key in PATH_KEYS}
        self.paths['lock'] = self.external + r'\lock'
        self.anchor = self.state + r'\anchor.lock'
        self.control_path = self.state + r'\control.lock'
        for index, path in enumerate((self.anchor, self.control_path, self.paths['lock']), 60):
            self.fs.objects[path] = self.fs.obj(index)
        self.control = control_module.WindowsControl(self.root, out=io.StringIO(), err=io.StringIO())
        self.listen = {'host': '127.0.0.1', 'port': 12345}

    def capture_lock_calls(self):
        calls = []
        original = platform_windows.acquire_guarded_path_lock
        def acquire(path, **kwargs):
            calls.append((path, kwargs.get('create'), kwargs.get('trusted_boundary')))
            return original(path, **kwargs)
        return calls, acquire

    def test_idle_readonly_uses_held_project_and_external_boundaries(self):
        calls, acquire = self.capture_lock_calls()
        with patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=acquire), \
             patch.object(self.control, '_probe_port') as port:
            self.control._idle_readonly(self.paths, self.listen)
        self.assertCountEqual(calls, [
            (self.control_path, False, self.root),
            (self.anchor, False, self.root),
            (self.paths['lock'], False, self.external),
        ])
        port.assert_called_once_with(self.listen)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_idle_with_borrowed_control_only_acquires_other_locks(self):
        with self.control._locked(self.paths) as held:
            calls, acquire = self.capture_lock_calls()
            with patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=acquire), \
                 patch.object(self.control, '_probe_port'):
                self.control._idle_readonly(self.paths, self.listen, held_control=held)
            self.assertFalse(held.closed)
            self.assertCountEqual(calls, [(self.anchor, False, self.root),
                                         (self.paths['lock'], False, self.external)])
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_start_probe_uses_snapshot_matched_leases(self):
        with self.control._locked(self.paths) as held:
            calls, acquire = self.capture_lock_calls()
            with patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=acquire):
                self.control._probe_start_locks(self.paths, held)
            self.assertCountEqual(calls, [(self.anchor, False, self.root),
                                         (self.paths['lock'], False, self.external)])
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_external_direct_parent_acl_is_not_bypassed(self):
        self.fs.objects[self.external]['unsafe_group'] = True
        with patch.object(self.control, '_probe_port') as port:
            with self.assertRaises(Exception):
                self.control._idle_readonly(self.paths, self.listen)
        port.assert_not_called()
        self.assertFalse(self.fs.handles)

    def test_unsafe_project_boundary_is_not_bypassed(self):
        self.fs.objects[self.root]['unsafe_group'] = True
        with patch.object(self.control, '_probe_port') as port:
            with self.assertRaises(Exception):
                self.control._idle_readonly(self.paths, self.listen)
        port.assert_not_called()
        self.assertFalse(self.fs.handles)

    def test_boundary_or_ancestor_replacement_blocks_start(self):
        with self.control._locked(self.paths) as held:
            original = platform_windows.acquire_guarded_path_lock
            def swap(path, **kwargs):
                self.fs.objects[r'C:\fixture'] = self.fs.obj(999, directory=True)
                return original(path, **kwargs)
            with patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=swap):
                with self.assertRaises(Exception):
                    self.control._probe_start_locks(self.paths, held)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_closed_lease_and_wrong_spelling_cannot_select_boundary(self):
        lease = windows_paths.inspect(self.anchor, backend=self.fixture.backend,
                                      trusted_boundary=self.root, single_link=True)
        with self.assertRaises(control_module.WindowsControlError):
            control_module._verified_lock_boundary(self.anchor.upper(), lease)
        lease.close()
        with self.assertRaises(control_module.WindowsControlError):
            control_module._verified_lock_boundary(self.anchor, lease)
        self.assertFalse(self.fs.handles)

    def test_guard_close_unknown_is_latched_and_not_success(self):
        original = platform_windows.acquire_guarded_path_lock
        failed = []
        def acquire(path, **kwargs):
            guard = original(path, **kwargs)
            if not failed:
                failed.append(guard._parents[0].owner.handle)
                self.fs.close_fail.add(failed[0])
            return guard
        with patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=acquire), \
             patch.object(self.control, '_probe_port'):
            with self.assertRaises(control_module.WindowsControlError) as caught:
                self.control._idle_readonly(self.paths, self.listen)
            self.assertEqual(caught.exception.code, 'lock_release_unknown')
            self.assertTrue(platform_lock._windows_release_unknown)
            self.assertEqual(self.fs.close_calls.count(failed[0]), 1)
            with self.assertRaises(control_module.WindowsControlError) as second:
                self.control._idle_readonly(self.paths, self.listen)
            self.assertEqual(second.exception.code, 'lock_release_unknown')

    def test_start_probe_unknown_close_is_latched(self):
        with self.control._locked(self.paths) as held:
            original = platform_windows.acquire_guarded_path_lock
            failed = []
            def acquire(path, **kwargs):
                guard = original(path, **kwargs)
                if not failed:
                    failed.append(guard._parents[0].owner.handle)
                    self.fs.close_fail.add(failed[0])
                return guard
            with patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=acquire):
                with self.assertRaises(control_module.WindowsControlError) as caught:
                    self.control._probe_start_locks(self.paths, held)
            self.assertEqual(caught.exception.code, 'lock_release_unknown')
            self.assertTrue(platform_lock._windows_release_unknown)
            self.assertEqual(self.fs.close_calls.count(failed[0]), 1)

    def test_status_never_publishes_stopped_after_unknown_lock_release(self):
        original = platform_windows.acquire_guarded_path_lock
        failed = []
        def acquire(path, **kwargs):
            guard = original(path, **kwargs)
            if not failed:
                failed.append(guard._parents[0].owner.handle)
                self.fs.close_fail.add(failed[0])
            return guard
        with patch.object(self.control, '_configuration', return_value=(self.paths, self.listen)), \
             patch.object(self.control, '_locator_hint', return_value=False), \
             patch.object(self.control, '_probe_port'), \
             patch.object(platform_windows, 'acquire_guarded_path_lock', side_effect=acquire):
            self.assertEqual(self.control.status(), 1)
        self.assertEqual(self.control.out.getvalue(), '')
        self.assertIn('lock_release_unknown', self.control.err.getvalue())
        self.assertIn('unknown', self.control.err.getvalue())

    def test_reparse_above_boundary_is_not_ignored(self):
        self.fs.objects[r'C:\fixture']['attributes'] |= 0x400
        with patch.object(self.control, '_probe_port') as port:
            with self.assertRaises(Exception):
                self.control._idle_readonly(self.paths, self.listen)
        port.assert_not_called()
        self.assertFalse(self.fs.handles)

    def test_lease_close_unknown_is_latched(self):
        original = windows_paths.inspect
        failed = []
        def inspect(path, **kwargs):
            lease = original(path, **kwargs)
            if path == self.state:
                failed.append(lease._handles[0].raw)
                self.fs.close_fail.add(failed[0])
            return lease
        with patch.object(windows_paths, 'inspect', side_effect=inspect), \
             patch.object(self.control, '_probe_port'):
            with self.assertRaises(control_module.WindowsControlError) as caught:
                self.control._idle_readonly(self.paths, self.listen)
        self.assertEqual(caught.exception.code, 'lock_release_unknown')
        self.assertTrue(platform_lock._windows_release_unknown)
        self.assertEqual(self.fs.close_calls.count(failed[0]), 1)

    def test_startup_diagnostics_keep_fixed_unknown_codes(self):
        for code in ('target_create_unknown', 'windows_creation_unknown',
                     'windows_creation_release_unknown'):
            done = threading.Event()
            done.set()
            capture = (bytearray(f'model_proxy: startup failed ({code})\n'.encode()),
                       threading.Lock(), done)
            self.assertEqual(control_module._startup_diagnostic(capture), code)


class WindowsCLIVirtualFixture:
    """Virtual Win32 APIs; actual CLI preflights, lock, ACL and writer code."""

    def __init__(self, case):
        self.case = case
        self.root = r'C:\fixture\project'
        self.state = self.root + r'\.runtime'
        self.config = self.root + r'\config\model_proxy_config.json'
        self.paths_file = self.root + r'\config\runtime_paths.json'
        self.tmp = self.state + r'\tmp'
        self.backups = self.state + r'\backups'
        self.fs = MultiVolumeFS()
        self.fs.objects[self.root + r'\config'] = self.fs.obj(501, directory=True)
        self.original = b'{"routes": [], "strategies": [], "supplies": []}'
        self.fs.objects[self.config] = dict(self.fs.obj(502), data=self.original)
        self.fs.objects[self.paths_file] = dict(self.fs.obj(503), data=json.dumps(DEFAULTS).encode())
        self.fs.objects['C:\\']['unsafe_group'] = True
        self.fs.objects[r'C:\fixture']['unsafe_group'] = True
        self.paths = {key: ntpath.join(self.root, value.replace('/', '\\'))
                      for key, value in DEFAULTS.items()}
        self.acl = VirtualACL(self.fs)
        self.crt = VirtualCRT(self.fs)
        self.backend = windows_paths.WindowsPathBackend(
            win32=discovery_fixtures.PathWinBridge(self.fs), acl_backend=self.acl)

        def create_directory(path, *, user_sid):
            self.fs.objects[path] = self.fs.obj(self.fs.next_index, directory=True)
            self.fs.next_index += 1
        self.fs.create_private_directory = create_directory
        self.fs.open_private_candidate = lambda path, *, original_handle=None, user_sid=None: \
            self.fs.open_file(path, True)
        fs = self.fs
        class VirtualPath(PureWindowsPath):
            def resolve(self, *args, **kwargs):
                return self
            def open(self, mode='r', encoding=None, **kwargs):
                if mode != 'r':
                    raise OSError('virtual fixture is read-only through Path')
                obj = fs.objects.get(str(self))
                if obj is None or obj['attributes'] & 0x10:
                    raise FileNotFoundError('virtual path missing')
                return io.StringIO(obj.get('data', b'').decode(encoding or 'utf-8'))
        self.Path = VirtualPath

        class VirtualPathOps:
            def __getattr__(self, name):
                return getattr(ntpath, name)
            def isfile(self, path):
                obj = fs.objects.get(os.fspath(path))
                return obj is not None and not (obj['attributes'] & 0x10)
        self.path_ops = VirtualPathOps()
        self.cleanup_observations = []

        class VirtualEntry:
            def __init__(self, path):
                self.path, self.name = path, ntpath.basename(path)
            def is_file(self, *, follow_symlinks=True):
                item = fs.objects[self.path]
                return not (item['attributes'] & (0x10 | 0x400))

        @contextlib.contextmanager
        def scandir(directory):
            if not directory.startswith(self.tmp + r'\model-proxy-cli-'):
                raise OSError('outside virtual marker scope')
            if directory not in fs.objects:
                raise FileNotFoundError('virtual directory missing')
            prefix = directory + '\\'
            entries = [VirtualEntry(path) for path in fs.objects
                       if path.startswith(prefix) and '\\' not in path[len(prefix):]]
            yield iter(entries)

        def unlink(path):
            if not path.startswith(self.tmp + r'\model-proxy-cli-') or ntpath.basename(path) != 'reload':
                raise OSError('outside virtual marker scope')
            del fs.objects[path]

        def rmdir(path):
            if not path.startswith(self.tmp + r'\model-proxy-cli-'):
                raise OSError('outside virtual marker scope')
            if any(name.startswith(path + '\\') for name in fs.objects):
                raise OSError('virtual directory not empty')
            del fs.objects[path]

        self.os = SimpleNamespace(name='nt', path=self.path_ops, fspath=os.fspath,
                                  environ=os.environ, scandir=scandir, unlink=unlink, rmdir=rmdir)
        original_ensure = platform_windows.ensure_private_directory
        def ensure(path, *, required_access=0x200A1, trusted_boundary=None):
            return original_ensure(path, required_access=required_access,
                                   trusted_boundary=trusted_boundary, backend=self.fs,
                                   acl_backend=self.acl, paths_backend=self.backend)
        self.ensure = ensure
        original_cleanup = dispatch._cleanup_windows_marker
        def cleanup(directory, expected, root):
            marker = directory + r'\reload'
            self.cleanup_observations.append((marker, fs.objects.get(marker, {}).get('data')))
            return original_cleanup(directory, expected, root)
        original_runtime_paths = dispatch.runtime_paths
        def runtime_paths(root):
            return original_runtime_paths(VirtualPath(root))
        self.stack = contextlib.ExitStack()
        case.addCleanup(self.close)
        for item in (
            patch.object(os, 'name', 'nt'),
            patch.object(sys, 'platform', 'win32'),
            patch.object(platform_lock, '_windows_release_unknown', False),
            patch.object(discovery, '_windows_path_backend', return_value=self.backend),
            patch.object(secure, '_windows_path_backend', return_value=self.backend),
            patch.object(platform_windows, '_api', side_effect=lambda backend=None: backend or self.fs),
            patch.object(platform_windows, '_NativeFDAPI', return_value=self.crt),
            patch.object(windows_acl, '_NativeBackend', return_value=self.acl),
            patch.object(platform_windows, 'ensure_private_directory', side_effect=ensure),
            patch.object(runtime_module, 'Path', VirtualPath),
            patch.object(dispatch, 'Path', VirtualPath),
            patch.object(dispatch, 'os', self.os),
            patch.object(dispatch, 'ROOT', self.root),
            patch.object(dispatch, 'runtime_paths', side_effect=runtime_paths),
            patch.object(dispatch, '_cleanup_windows_marker', side_effect=cleanup),
        ):
            self.stack.enter_context(item)

    def close(self):
        self.fs.close_fail.clear()
        self.crt.fail_close = False
        for fd in list(self.crt.mapping):
            self.crt.close(fd)
        for handle in list(self.fs.handles):
            self.fs.close(handle)
        self.fs.locked.clear()
        self.stack.close()


class MarkerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = WindowsCLIVirtualFixture(self)
        self.fs, self.root, self.config = self.fixture.fs, self.fixture.root, self.fixture.config

    def test_real_fake_primitives_use_project_boundary_for_both_directories(self):
        actual_write = secure.write_target
        seen = []
        def write(snapshot, payload, **options):
            self.assertEqual(payload, b'started')
            self.assertFalse(snapshot.exists)
            self.assertEqual(options['stage_directory'], self.fixture.tmp)
            self.assertFalse(options['make_backup'])
            check = options['artifact_preflight']
            def verify(role, path):
                self.assertTrue(self.fs.locked)
                self.assertEqual(role, 'stage')
                self.assertEqual(ntpath.dirname(path), self.fixture.tmp)
                seen.append(role)
                return check(role, path)
            options['artifact_preflight'] = verify
            return actual_write(snapshot, payload, **options)
        with patch.object(platform_windows, 'ensure_private_directory',
                          wraps=platform_windows.ensure_private_directory) as ensure, \
             patch.object(secure, 'capture_target', wraps=secure.capture_target) as capture, \
             patch.object(secure, 'write_target', side_effect=write) as writer:
            with dispatch._windows_marker_scope(self.root, self.config) as holder:
                self.assertTrue(holder.path.startswith(self.root + r'\.runtime\tmp\model-proxy-cli-'))
                self.assertEqual(self.fs.objects[holder.path]['data'], b'started')
                self.assertFalse(self.fs.locked)
                holder.cleanup_ok = True
        self.assertGreaterEqual(ensure.call_count, 2)
        ensured = [call.args[0] for call in ensure.call_args_list]
        for path in (self.fixture.state, self.fixture.tmp, self.fixture.backups,
                     ntpath.dirname(holder.path)):
            self.assertIn(path, ensured)
        self.assertTrue(all(call.kwargs['trusted_boundary'] == self.root for call in ensure.call_args_list))
        self.assertTrue(all(call.kwargs['trusted_boundary'] == self.root
                            for call in capture.call_args_list))
        writer.assert_called_once()
        self.assertGreaterEqual(seen.count('stage'), 2)
        self.assertTrue(any(call[0] == 'lock' for call in self.fs.calls))
        self.assertTrue(any(call[0] == 'unlock' for call in self.fs.calls))
        dispatch._cleanup_windows_marker.assert_called_once()
        self.assertNotIn(holder.path, self.fs.objects)
        self.assertEqual(self.fixture.cleanup_observations, [(holder.path, b'started')])
        self.assertFalse(self.fs.handles)

    def test_unconfirmed_creation_and_release_do_not_create_marker_or_cleanup(self):
        failures = (
            ('tmp', platform_windows.WindowsPrivateCreationError(
                'release_unknown', created=None, release_unknown=True)),
            ('marker', SimpleNamespace(created=None)),
            ('marker', platform_windows.WindowsPrivateCreationError(
                'release_unknown', created=True, release_unknown=True)),
            ('marker', SimpleNamespace(created=False)),
            ('marker', SimpleNamespace(created=True, identity=(), protection=())),
            ('marker', SimpleNamespace(created=True, identity=(17, 99, 1, 16),
                                       protection=('private',), release_unknown=True)),
        )
        for where, outcome in failures:
            with self.subTest(where=where, outcome=outcome):
                dispatch._cleanup_windows_marker.reset_mock()
                appeared = []
                def ensure(path, **kwargs):
                    matched = (where == 'tmp' and path == self.fixture.tmp) or (
                        where == 'marker' and ntpath.basename(path).startswith('model-proxy-cli-'))
                    if matched:
                        # created=True may leave our directory behind; created=False
                        # models a directory appearing concurrently after the scan.
                        if where == 'marker' and getattr(outcome, 'created', None) in (True, False):
                            self.fs.objects[path] = self.fs.obj(self.fs.next_index, directory=True)
                            self.fs.next_index += 1
                            appeared.append(path)
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome
                    return self.fixture.ensure(path, **kwargs)
                with patch.object(platform_windows, 'ensure_private_directory', side_effect=ensure), \
                     patch.object(secure, 'capture_target', wraps=secure.capture_target) as capture, \
                     patch.object(secure, 'write_target', wraps=secure.write_target) as writer:
                    with self.assertRaises(platform_windows.WindowsPrivateCreationError) as caught:
                        with dispatch._windows_marker_scope(self.root, self.config):
                            self.fail('unconfirmed directory cannot authorize marker')
                if isinstance(outcome, platform_windows.WindowsPrivateCreationError):
                    self.assertIs(caught.exception.created, outcome.created)
                    self.assertEqual(caught.exception.release_unknown, outcome.release_unknown)
                else:
                    self.assertIs(caught.exception.created, outcome.created)
                    if getattr(outcome, 'release_unknown', False):
                        self.assertTrue(caught.exception.release_unknown)
                capture.assert_not_called()
                writer.assert_not_called()
                dispatch._cleanup_windows_marker.assert_not_called()
                self.assertFalse(self.fs.locked)
                self.assertFalse(self.fs.handles)
                for path in appeared:
                    self.assertIn(path, self.fs.objects)
                    self.assertNotIn(path + r'\reload', self.fs.objects)

    def test_marker_creation_requires_protected_direct_parent(self):
        self.fs.objects[self.root + r'\.runtime']['unsafe_group'] = True
        with patch.object(secure, 'capture_target', wraps=secure.capture_target) as capture, \
             patch.object(secure, 'write_target', wraps=secure.write_target) as writer:
            with self.assertRaises(platform_lock.LockError) as caught:
                with dispatch._windows_marker_scope(self.root, self.config):
                    self.fail('unsafe parent cannot authorize marker')
        self.assertEqual(caught.exception.code, 'config_path_unknown')
        capture.assert_not_called()
        writer.assert_not_called()
        dispatch._cleanup_windows_marker.assert_not_called()
        self.assertFalse(self.fs.handles)

    def test_helper_reports_unknown_and_never_spawns_after_creation_unknown(self):
        error = platform_windows.WindowsPrivateCreationError(
            'release_unknown', created=None, release_unknown=True)
        control = Mock()
        with patch.object(platform_windows, 'ensure_private_directory', side_effect=error), \
             patch.object(dispatch.subprocess, 'run') as spawn, \
             patch('sys.stderr', new_callable=io.StringIO) as errors:
            result = dispatch.run_helper('route-add', [], self.config, control, root=self.root)
        self.assertEqual(result, 1)
        self.assertIn('unknown', errors.getvalue())
        spawn.assert_not_called()
        control.reload.assert_not_called()
        dispatch._cleanup_windows_marker.assert_not_called()

    def test_unknown_marker_write_is_not_cleaned_or_published(self):
        from core.windows_secure_native import ProductWriteBackend
        original_finish = ProductWriteBackend.finish
        def finish(backend):
            original_finish(backend)
            if backend.snapshot.path.endswith(r'\reload') and not backend.snapshot.exists:
                raise OSError('synthetic close failure')
        with patch.object(ProductWriteBackend, 'finish', finish), \
             patch.object(secure, 'capture_target', wraps=secure.capture_target) as capture, \
             patch('sys.stderr', new_callable=io.StringIO) as errors:
            with self.assertRaises(secure.SecureWriteError) as caught:
                with dispatch._windows_marker_scope(self.root, self.config):
                    self.fail('unknown write cannot be published')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertTrue(caught.exception.release_unknown)
        self.assertIn('retained', errors.getvalue())
        dispatch._cleanup_windows_marker.assert_not_called()
        self.assertEqual(capture.call_count, 1)
        self.assertFalse(self.fs.locked)
        self.assertTrue(any(path.endswith(r'\reload') and item.get('data') == b'started'
                            for path, item in self.fs.objects.items()))


if __name__ == '__main__':
    unittest.main()
