"""Concurrency at the real _windows_main call site with POSIX test locks.

Not evidence for Win32 locking or ACLs. All files/processes are fixture-owned.
"""
import contextlib
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import fcntl
except ImportError:
    fcntl = None


def _run_fixture(root_text, entered, release, result):
    from core import discovery, server
    from core.runtime_paths import PATH_KEYS
    root = Path(root_text)
    paths = {key: root / key for key in PATH_KEYS}
    state = root / 'state'
    state.mkdir(exist_ok=True)
    def make_snapshot(log_exists):
        values = {key: SimpleNamespace(canonical=str(path), exists=(log_exists if key == 'log' else False))
                  for key, path in paths.items()}
        values['state_dir'] = SimpleNamespace(canonical=str(state), exists=True)
        return SimpleNamespace(project_root=str(root), as_dict=lambda: values)
    initial = make_snapshot(False)
    final = make_snapshot(True)
    @contextlib.contextmanager
    def acquire(snapshot, role, **kwargs):
        with (root / (role + '.fixture')).open('a+b') as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise server.LockError('lock_conflict') from None
            try:
                yield SimpleNamespace(snapshot=snapshot)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    def prepare_log(project, runtime, snapshot):
        entered.set()
        if not release.wait(5):
            raise RuntimeError('fixture timeout')
        paths['log'].write_bytes(b'fixture-owned-log')
        return final
    def capture(_config, _store, _listen, legacy):
        result.put(('snapshot', legacy.snapshot is final))
        raise RuntimeError('fixture_done')
    listen = {'host': '127.0.0.1', 'port': 12345, 'allow_hosts': [], 'host_check_enabled': False}
    with contextlib.ExitStack() as stack:
        def p(obj, name, **kwargs):
            stack.enter_context(patch.object(obj, name, **kwargs))
        p(server, '_PACKAGE_DIR', new=root)
        p(server, 'resolve_runtime_paths', return_value=paths)
        p(server, 'resolve_listen', return_value=listen)
        p(server, '_require_windows_service_launch')
        p(discovery, 'prepare_windows_runtime_directories')
        p(discovery, 'prepare_windows_log', side_effect=prepare_log)
        p(server, '_check_config_targets')
        # _windows_main 直接调用 _windows_check_config_targets（不走 _check_config_targets）
        p(server, '_windows_check_config_targets')
        p(server, 'inspect_runtime_identities', return_value=initial)
        p(server, 'verify_record_directory')
        p(server, 'acquire_lock', side_effect=acquire)
        p(server, 'verify_held_instance_locks')
        p(discovery, 'read_windows_active', return_value=None)
        p(server, '_check_legacy_observations')
        p(server, 'ConfigStore', return_value=object())
        p(server, '_capture_migration_guard', side_effect=capture)
        import core.platform_lock as locks
        p(locks, '_windows_compare_post', side_effect=lambda before, after, role:
          result.put(('compare', before is initial and after is final and role == 'log')))
        try:
            server._windows_main(root / 'fake-config.json')
        except BaseException as exc:
            result.put(('end', getattr(exc, 'code', str(exc))))


@unittest.skipUnless(fcntl is not None and hasattr(os, 'fork'), 'requires POSIX fixture locks')
class StartupConcurrencyTests(unittest.TestCase):
    def test_contender_cannot_create_log_or_touch_ledger_before_both_locks(self):
        ctx = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory(prefix='mp-startup-') as temp:
            root = Path(temp)
            (root / 'totals').write_bytes(b'fixture-ledger')
            entered, release = ctx.Event(), ctx.Event()
            first_result, second_result = ctx.Queue(), ctx.Queue()
            first = ctx.Process(target=_run_fixture, args=(temp, entered, release, first_result))
            second = ctx.Process(target=_run_fixture, args=(temp, ctx.Event(), ctx.Event(), second_result))
            first.start()
            try:
                self.assertTrue(entered.wait(5), 'first fixture never held both locks')
                self.assertFalse((root / 'log').exists())
                second.start()
                second.join(5)
                self.assertFalse(second.is_alive())
                self.assertEqual(second_result.get(timeout=2), ('end', 'lock_conflict'))
                self.assertFalse((root / 'log').exists())
                self.assertEqual((root / 'totals').read_bytes(), b'fixture-ledger')
                release.set()
                first.join(5)
                self.assertFalse(first.is_alive())
                self.assertEqual(first_result.get(timeout=2), ('compare', True))
                self.assertEqual(first_result.get(timeout=2), ('snapshot', True))
                self.assertEqual(first_result.get(timeout=2), ('end', 'fixture_done'))
                self.assertEqual((root / 'log').read_bytes(), b'fixture-owned-log')
                self.assertEqual((root / 'totals').read_bytes(), b'fixture-ledger')
            finally:
                release.set()
                for proc in (first, second):
                    if proc.pid and proc.is_alive():
                        proc.terminate()  # only the process created above
                        proc.join(5)
                first_result.close()
                second_result.close()

    def test_legacy_lock_conflict_prevents_log_creation(self):
        ctx = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory(prefix='mp-startup-') as temp:
            root = Path(temp)
            (root / 'totals').write_bytes(b'fixture-ledger')
            entered, release, result = ctx.Event(), ctx.Event(), ctx.Queue()
            with (root / 'lock.fixture').open('a+b') as legacy:
                fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
                proc = ctx.Process(target=_run_fixture, args=(temp, entered, release, result))
                proc.start()
                try:
                    proc.join(5)
                    self.assertFalse(proc.is_alive())
                    self.assertEqual(result.get(timeout=2), ('end', 'lock_conflict'))
                    self.assertFalse(entered.is_set())
                    self.assertFalse((root / 'log').exists())
                    self.assertEqual((root / 'totals').read_bytes(), b'fixture-ledger')
                finally:
                    release.set()
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(5)
                    result.close()
                    fcntl.flock(legacy, fcntl.LOCK_UN)


if __name__ == '__main__':
    unittest.main()

class SnapshotRefreshTests(unittest.TestCase):
    def test_real_snapshot_comparison_accepts_only_fixture_log_creation(self):
        from core import discovery, platform_lock, platform_windows
        from core.runtime_paths import PATH_KEYS
        from tests.test_windows_secure_creation import PrivateCreation
        fixture = PrivateCreation('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.ROOT
        paths = {key: r'C:\fixture\runtime' + '\\' + key for key in PATH_KEYS}
        with patch.object(os, 'name', 'nt'), \
             patch.object(discovery, '_windows_path_backend', return_value=fixture.paths):
            before = discovery.inspect_runtime_identities(root, paths, windows_light=True)
            self.assertFalse(before.as_dict()['log'].exists)
            after = discovery.prepare_windows_log(
                root, paths, before,
                create_empty_file=lambda path, *, trusted_boundary: platform_windows.create_private_empty_file(
                    path, trusted_boundary=trusted_boundary, **fixture.args()))
            platform_lock._windows_compare_post(before, after, 'log')
            self.assertTrue(after.as_dict()['log'].exists)
            fixture.fs.objects[paths['totals']] = fixture.fs.obj(777)
            changed = discovery.inspect_runtime_identities(root, paths, windows_light=True)
            with self.assertRaises(platform_lock.LockError) as caught:
                platform_lock._windows_compare_post(before, changed, 'log')
            self.assertEqual(caught.exception.code, 'preflight_changed')
        self.assertFalse(fixture.fs.handles)

class RealHeldLockRefreshTests(unittest.TestCase):
    """Real product lock/snapshot checks against the existing in-memory Win32 fixture."""
    def exercise(self, *, drift):
        from pathlib import PureWindowsPath
        from core import discovery, server, platform_lock
        from tests.test_windows_service_startup import WindowsCoreMockStartupTests
        fixture = WindowsCoreMockStartupTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.close_locks()
        fixture.fs.objects.pop(fixture.paths['log'], None)
        reached = []
        def create_log(root, paths, snapshot):
            self.assertFalse(snapshot.as_dict()['log'].exists)
            fixture.fs.objects[fixture.paths['log']] = fixture.fs.obj(884)
            if drift is True:
                fixture.fs.objects[fixture.paths['totals']] = fixture.fs.obj(885)
            return discovery.inspect_runtime_identities(root, paths, windows_light=True)
        def guard(*args):
            reached.append(True)
            raise RuntimeError('fixture_complete')
        real_compare = platform_lock._windows_compare_post
        def compare_then_drift(before, after, role):
            real_compare(before, after, role)
            if role == 'log':
                fixture.fs.objects[fixture.paths['totals']] = fixture.fs.obj(886)
        with contextlib.ExitStack() as stack:
            if drift == 'after_comparison':
                stack.enter_context(patch.object(platform_lock, '_windows_compare_post', side_effect=compare_then_drift))
            with patch.object(server, '_require_windows_service_launch'), \
             patch.object(discovery, 'prepare_windows_runtime_directories'), \
             patch.object(discovery, 'prepare_windows_log', side_effect=create_log), \
             patch.object(server, '_check_config_targets'), \
             patch.object(server, '_windows_check_config_targets'), \
             patch.object(server, '_check_legacy_observations'), \
             patch.object(server, 'ConfigStore', return_value=object()), \
             patch.object(server, '_capture_migration_guard', side_effect=guard):
                with self.assertRaises(Exception) as caught:
                    server._windows_main(PureWindowsPath(fixture.config))
        if drift:
            self.assertEqual(getattr(caught.exception, 'code', None), 'preflight_changed')
            self.assertFalse(reached)
        else:
            self.assertEqual(str(caught.exception), 'fixture_complete')
            self.assertEqual(reached, [True])
        self.assertFalse(fixture.fs.handles)
        self.assertFalse(fixture.fs.locked)

    def test_first_log_creation_refreshes_real_held_locks(self):
        self.exercise(drift=False)

    def test_first_log_creation_rejects_other_identity_drift(self):
        self.exercise(drift=True)

    def test_real_held_lock_check_rejects_drift_after_comparison(self):
        self.exercise(drift='after_comparison')
