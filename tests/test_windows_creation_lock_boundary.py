"""Bounded Windows creation and locking with synthetic backends only."""
import unittest
import os
from dataclasses import replace
from unittest.mock import patch

from core import discovery, platform_lock as locks, platform_windows as win
from core.runtime_paths import PATH_KEYS, DEFAULTS, LEGACY_DEFAULTS, parse_runtime_paths
from tests import test_windows_secure_creation as creation_fixture
from tests import test_windows_discovery as discovery_fixture


class CreationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = creation_fixture.PrivateCreation('runTest')
        self.fixture.setUp()
        self.fs, self.acl, self.root = self.fixture.fs, self.fixture.acl, self.fixture.ROOT
        self.fs.objects['C:\\']['unsafe_group'] = True
        self.fs.objects[r'C:\fixture']['unsafe_group'] = True

    def args(self):
        return dict(self.fixture.args(), trusted_boundary=self.root)

    def test_directory_and_file_create_then_verify(self):
        directory = self.root + r'\runtime'
        self.assertTrue(win.ensure_private_directory(directory, **self.args()).created)
        self.assertFalse(win.ensure_private_directory(directory, **self.args()).created)
        self.assertTrue(win.create_private_empty_file(self.root + r'\log', **self.args()).created)
        self.assertFalse(self.fs.handles)

    def test_omitted_or_wrong_boundary_keeps_strict_failure(self):
        path = self.root + r'\runtime'
        for kwargs in (self.fixture.args(),
                       dict(self.fixture.args(), trusted_boundary=r'C:\fixture\PROJECT'),
                       dict(self.fixture.args(), trusted_boundary=r'C:\other')):
            with self.subTest(kwargs=kwargs), self.assertRaises(win.WindowsPrivateCreationError):
                win.ensure_private_directory(path, **kwargs)
            self.assertNotIn(path, self.fs.objects)
            self.assertFalse(self.fs.handles)

    def test_boundary_is_not_creation_authority(self):
        original = self.acl.read_observation
        def deny_create(handle, kind, required):
            result = original(handle, kind, required)
            if self.fs.handles[handle][0] == self.root and required & 0x4:
                return replace(result, granted=0)
            return result
        self.acl.read_observation = deny_create
        path = self.root + r'\runtime'
        with self.assertRaises(win.WindowsPrivateCreationError) as exc:
            win.ensure_private_directory(path, **self.args())
        self.assertFalse(exc.exception.created)
        self.assertNotIn(path, self.fs.objects)
        self.assertFalse(self.fs.handles)

    def test_boundary_and_direct_parent_acl_still_required(self):
        for parent in (self.root, self.root + r'\subdir'):
            with self.subTest(parent=parent):
                if parent != self.root:
                    self.fs.objects[parent] = self.fs.obj(444, directory=True)
                self.fs.objects[parent]['unsafe_group'] = True
                target = parent + r'\runtime'
                with self.assertRaises(win.WindowsPrivateCreationError):
                    win.ensure_private_directory(target, **self.args())
                self.assertNotIn(target, self.fs.objects)
                self.assertFalse(self.fs.handles)
                self.fs.objects[parent]['unsafe_group'] = False

    def test_ancestor_swap_during_creation_is_not_reported_verified(self):
        original_create = self.fs.create_private_directory
        def swap(path, *, user_sid):
            original_create(path, user_sid=user_sid)
            self.fs.objects[r'C:\fixture'] = self.fs.obj(678, directory=True)
        self.fs.create_private_directory = swap
        with self.assertRaises(win.WindowsPrivateCreationError) as exc:
            win.ensure_private_directory(self.root + r'\runtime', **self.args())
        self.assertTrue(exc.exception.created)
        self.assertFalse(self.fs.handles)

    def test_discovery_log_create_uses_project_or_external_parent_boundary(self):
        state = self.root + r'\.runtime'
        for log, boundary in ((state + r'\log', self.root),
                              (r'C:\fixture\runtime\log', r'C:\fixture\runtime')):
            with self.subTest(log=log):
                paths = {key: state + "\\" + key for key in PATH_KEYS}
                paths['log'] = log
                calls = []
                def create(path, *, trusted_boundary):
                    calls.append(trusted_boundary)
                    return win.create_private_empty_file(path, trusted_boundary=trusted_boundary,
                                                         **self.fixture.args())
                with patch.object(os, 'name', 'nt'), \
                     patch.object(discovery, '_windows_path_backend', return_value=self.fixture.paths):
                    before = discovery.inspect_runtime_identities(self.root, paths, windows_light=True)
                    after = discovery.prepare_windows_log(self.root, paths, before, create_empty_file=create)
                self.assertTrue(after.as_dict()['log'].exists)
                self.assertEqual(calls, [boundary])
                self.assertFalse(self.fs.handles)

    def test_reparse_above_boundary_still_refused(self):
        self.fs.objects[r'C:\fixture']['attributes'] |= 0x400
        with self.assertRaises(win.WindowsPrivateCreationError):
            win.ensure_private_directory(self.root + r'\runtime', **self.args())
        self.assertFalse(self.fs.handles)


class LockBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = discovery_fixture.WindowsDiscoveryLockTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fs, self.acl = self.fixture.fs, self.fixture.acl
        self.root, self.state = self.fixture.ROOT, self.fixture.STATE
        self.fs.objects['C:\\']['unsafe_group'] = True
        self.fs.objects[r'C:\fixture']['unsafe_group'] = True
        self.paths = {key: self.state + '\\' + key for key in PATH_KEYS}

    def snapshot(self):
        return discovery.inspect_runtime_identities(self.root, self.paths)

    def test_discovery_to_directory_creation_and_lifetime_lock(self):
        del self.fs.objects[self.state]
        def create_directory(path, *, user_sid):
            self.assertEqual(path, self.state)
            self.fs.objects[path] = self.fs.obj(12, directory=True)
        self.fs.create_private_directory = create_directory
        def ensure(path, *, trusted_boundary):
            return win.ensure_private_directory(path, trusted_boundary=trusted_boundary,
                    backend=self.fs, acl_backend=self.acl, paths_backend=self.fixture.backend)
        self.assertFalse(self.snapshot().as_dict()['state_dir'].exists)
        discovery.prepare_windows_runtime_directories(self.root, self.paths, None,
                                                       ensure_directory=ensure)
        anchor = locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        child = locks.acquire_lock(anchor.snapshot, 'lock', create=True, anchor=anchor)
        try:
            discovery.verify_held_instance_locks(anchor, child)
            anchor._windows_guard.revalidate(retain_on_failure=True)
            child._windows_guard.revalidate(retain_on_failure=True)
        finally:
            child.close()
            anchor.close()
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_legacy_provenance_creates_only_selected_directories_under_localappdata(self):
        local = r'C:\fixture\Local'
        self.fs.objects[local] = self.fs.obj(80, directory=True)
        raw = dict(DEFAULTS)
        raw['lock'] = LEGACY_DEFAULTS['lock']
        paths = parse_runtime_paths(raw, project_root=self.root, platform='windows',
                                    local_appdata=local).map_values(str)
        next_id = iter((81, 82))
        def mkdir(path, *, user_sid):
            self.fs.objects[path] = self.fs.obj(next(next_id), directory=True)
        self.fs.create_private_directory = mkdir
        calls = []
        def ensure(path, *, trusted_boundary):
            calls.append((path, trusted_boundary))
            return win.ensure_private_directory(path, trusted_boundary=trusted_boundary,
                backend=self.fs, acl_backend=self.acl, paths_backend=self.fixture.backend)
        discovery.prepare_windows_runtime_directories(self.root, paths, local, ensure_directory=ensure)
        self.assertEqual(calls, [(self.state, self.root),
                                 (local + r'\model_proxy', local),
                                 (local + r'\model_proxy\runtime', local)])
        snapshot = discovery.inspect_runtime_identities(self.root, paths)
        anchor = locks.acquire_lock(snapshot, 'anchor.lock', create=True)
        child = locks.acquire_lock(anchor.snapshot, 'lock', create=True, anchor=anchor)
        child.close()
        anchor.close()
        self.assertFalse(self.fs.handles)

    def test_external_direct_parent_boundary(self):
        parent = self.fixture.RUNTIME
        self.paths['lock'] = parent + r'\lock'
        captured = []
        original = win.acquire_guarded_path_lock
        def acquire(path, **kwargs):
            captured.append((path, kwargs['trusted_boundary']))
            return original(path, **kwargs)
        with patch.object(win, 'acquire_guarded_path_lock', side_effect=acquire):
            anchor = locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
            child = locks.acquire_lock(anchor.snapshot, 'lock', create=True, anchor=anchor)
            child.close()
            anchor.close()
        self.assertIn((self.paths['lock'], parent), captured)
        self.assertIn((self.state + r'\anchor.lock', self.root), captured)
        self.assertFalse(self.fs.handles)

    def test_guard_create_requires_direct_parent_add_file(self):
        original = self.acl.read_observation
        def deny_create(handle, kind, required):
            result = original(handle, kind, required)
            if self.fs.handles[handle][0] == self.state and required & 0x2:
                return replace(result, granted=0)
            return result
        self.acl.read_observation = deny_create
        target = self.state + r'\anchor.lock'
        with self.assertRaises(win.WindowsPathGuardError):
            win.acquire_guarded_path_lock(target, create=True, trusted_boundary=self.root,
                backend=self.fs, fd_backend=self.fixture.crt, acl_backend=self.acl)
        self.assertNotIn(target, self.fs.objects)
        self.assertFalse(self.fs.handles)

    def test_held_lock_detects_parent_replacement_above_boundary(self):
        anchor = locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.fs.objects[r'C:\fixture'] = self.fs.obj(999, directory=True)
        try:
            with self.assertRaises(win.WindowsPathGuardError):
                anchor._windows_guard.revalidate(retain_on_failure=True)
        finally:
            anchor.close()
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_guard_rejects_invalid_boundary_and_unsafe_direct_parent(self):
        target = self.state + r'\anchor.lock'
        for boundary in (r'C:\fixture\PROJECT', r'C:\other', target, 'C:\\'):
            with self.subTest(boundary=boundary), self.assertRaises(win.WindowsPathGuardError):
                win.acquire_guarded_path_lock(target, create=True, trusted_boundary=boundary,
                    backend=self.fs, fd_backend=self.fixture.crt, acl_backend=self.acl)
            self.assertNotIn(target, self.fs.objects)
            self.assertFalse(self.fs.handles)
        self.fs.objects[self.state]['unsafe_group'] = True
        with self.assertRaises(win.WindowsPathGuardError):
            win.acquire_guarded_path_lock(target, create=True, trusted_boundary=self.root,
                backend=self.fs, fd_backend=self.fixture.crt, acl_backend=self.acl)
        self.assertNotIn(target, self.fs.objects)
        self.assertFalse(self.fs.handles)

    def test_new_lock_uses_protected_candidate_and_checked_parent_sid(self):
        calls = []
        original = self.fs.open_private_candidate
        def create(path, *, user_sid):
            calls.append((path, user_sid))
            return original(path, user_sid=user_sid)
        self.fs.open_private_candidate = create
        target = self.state + r'\anchor.lock'
        guard = win.acquire_guarded_path_lock(target, create=True, trusted_boundary=self.root,
            backend=self.fs, fd_backend=self.fixture.crt, acl_backend=self.acl)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], target)
        self.assertEqual(calls[0][1], guard._parents[-1].acl.observation.user)
        self.assertTrue(guard.object_evidence.acl.observation.control & 0x1000)
        guard.close()
        self.assertFalse(self.fs.handles)

    def test_unprotected_created_lock_reports_partial_creation(self):
        original = self.fs.open_private_candidate
        def create(path, *, user_sid):
            handle = original(path, user_sid=user_sid)
            self.fs.handles[handle][1]['control'] = 0x8004
            return handle
        self.fs.open_private_candidate = create
        target = self.state + r'\anchor.lock'
        with self.assertRaises(win.WindowsPathGuardError) as exc:
            win.acquire_guarded_path_lock(target, create=True, trusted_boundary=self.root,
                backend=self.fs, fd_backend=self.fixture.crt, acl_backend=self.acl)
        self.assertEqual(exc.exception.code, 'protection_unverified')
        self.assertIs(exc.exception.created, True)
        self.assertIn(target, self.fs.objects)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_unknown_create_and_unknown_release_propagate_through_lock(self):
        for release, expected in ((False, 'target_create_unknown'),
                                  (True, 'lock_release_unknown')):
            with self.subTest(release=release):
                def fail(path, *, user_sid):
                    exc = OSError('fake')
                    exc.release_unknown = release
                    raise exc
                self.fs.open_private_candidate = fail
                with self.assertRaises(locks.LockError) as exc:
                    locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
                self.assertEqual(exc.exception.code, expected)
                self.assertIsNone(exc.exception.created)
                self.assertEqual(locks._windows_release_unknown, release)
                self.assertFalse(self.fs.handles)
                # The global is fixture-patched; restore between subcases only.
                locks._windows_release_unknown = False

    def test_interrupted_unknown_release_latches_and_preserves_interrupt(self):
        def interrupted(path, *, user_sid):
            raise KeyboardInterrupt()
        self.fs.open_private_candidate = interrupted
        with self.assertRaises(KeyboardInterrupt) as exc:
            locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.assertTrue(exc.exception.release_unknown)
        self.assertIsNone(exc.exception.created)
        self.assertTrue(locks._windows_release_unknown)
        self.assertFalse(self.fs.handles)

    def test_interrupted_raw_handle_handoff_closes_once_and_reports_outcome(self):
        original = win.WindowsFileHandle
        for close_fails in (False, True):
            with self.subTest(close_fails=close_fails):
                handles = []
                def wrap(handle, backend, *, created=False):
                    if created:
                        handles.append(handle)
                        if close_fails:
                            self.fs.close_fail.add(handle)
                        raise KeyboardInterrupt()
                    return original(handle, backend, created=created)
                with patch.object(win, 'WindowsFileHandle', side_effect=wrap):
                    with self.assertRaises(KeyboardInterrupt) as exc:
                        locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
                self.assertTrue(exc.exception.created)
                self.assertEqual(exc.exception.release_unknown, close_fails)
                self.assertEqual(self.fs.close_calls.count(handles[0]), 1)
                self.assertEqual(locks._windows_release_unknown, close_fails)
                # In-memory namespace only; never remove a real lock file.
                self.fs.objects.pop(self.state + r'\anchor.lock')
                locks._windows_release_unknown = False

    def test_held_lock_detects_direct_parent_acl_change(self):
        anchor = locks.acquire_lock(self.snapshot(), 'anchor.lock', create=True)
        self.fs.objects[self.state]['unsafe_group'] = True
        try:
            with self.assertRaises(win.WindowsPathGuardError):
                anchor._windows_guard.revalidate(retain_on_failure=True)
        finally:
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_unknown_close_remains_unknown_and_not_retried(self):
        target = self.state + r'\anchor.lock'
        guard = win.acquire_guarded_path_lock(target, create=True, trusted_boundary=self.root,
            backend=self.fs, fd_backend=self.fixture.crt, acl_backend=self.acl)
        handle = guard._parents[0].owner.handle
        self.fs.close_fail.add(handle)
        with self.assertRaises(win.WindowsPathGuardError) as exc:
            guard.close()
        self.assertEqual(exc.exception.code, 'release_unknown')
        guard.close()
        self.assertEqual(self.fs.close_calls.count(handle), 1)


if __name__ == '__main__':
    unittest.main()
