"""Finite-writer handle handoff tests with synthetic Win32 objects only."""
import contextlib
import ctypes
import os
import unittest
from unittest.mock import patch

from core import discovery, platform_windows as win, windows_acl as acl
from core import windows_secure_files as secure
from core.windows_paths import WindowsPathBackend
from core.windows_secure_native import ProductWriteBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_discovery import PathWinBridge
from tests.test_windows_secure_files_writer import WritingFS


class FakeFunction:
    def __init__(self, func):
        self.func = func
        self.argtypes = self.restype = None

    def __call__(self, *args):
        return self.func(*args)


class NativeCandidateHandoff(unittest.TestCase):
    """Exercise the actual Python API method; calls below are fake, not Win32."""
    def make_api(self, *, free_result=None, free_error=None, close_ok=True):
        calls = []
        class Kernel:
            pass
        kernel = Kernel()
        kernel.CreateFileW = FakeFunction(lambda *args: 4321)
        def free(_):
            calls.append('free')
            if free_error:
                raise free_error
            return free_result
        kernel.LocalFree = FakeFunction(free)
        def close(handle):
            calls.append(('close', handle))
            return int(close_ok)
        kernel.CloseHandle = FakeFunction(close)
        class Advapi:
            pass
        advapi = Advapi()
        def security(*args):
            ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 0
        advapi.GetSecurityInfo = FakeFunction(security)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(lambda *args: 0)
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        return api, advapi, calls

    def invoke(self, api, advapi):
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True):
            return api.open_private_candidate(r'C:\fixture\candidate', original_handle=7)

    def failure(self, api, advapi):
        try:
            self.invoke(api, advapi)
        except BaseException as exc:
            self.assertIsInstance(exc, OSError)
            return exc
        self.fail('candidate creation should have failed')

    def test_descriptor_release_interruption_closes_known_handle(self):
        api, advapi, calls = self.make_api(free_error=KeyboardInterrupt())
        error = self.failure(api, advapi)
        self.assertTrue(error.created)
        self.assertTrue(error.release_unknown)
        self.assertEqual(calls, ['free', ('close', 4321)])

    def test_descriptor_release_failure_closes_known_handle(self):
        api, advapi, calls = self.make_api(free_result=1234)
        with self.assertRaises(OSError) as caught:
            self.invoke(api, advapi)
        self.assertTrue(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(calls, ['free', ('close', 4321)])

    def test_descriptor_release_and_close_failure_are_unknown_without_retry(self):
        api, advapi, calls = self.make_api(free_error=KeyboardInterrupt(), close_ok=False)
        error = self.failure(api, advapi)
        self.assertTrue(error.created)
        self.assertTrue(error.release_unknown)
        self.assertEqual(calls, ['free', ('close', 4321)])

    def test_success_hands_off_handle_without_early_close(self):
        api, advapi, calls = self.make_api()
        handle = self.invoke(api, advapi)
        self.assertEqual(handle, 4321)
        self.assertEqual(calls, ['free'])
        api.close(handle)
        self.assertEqual(calls, ['free', ('close', 4321)])


class FiniteProductHandoff(unittest.TestCase):
    ROOT = r'C:\fixture\project'
    TARGET = ROOT + r'\client.json'

    def setUp(self):
        self.fs = WritingFS()
        self.fs.objects[self.TARGET] = dict(self.fs.obj(801), data=b'old-fake')
        self.acl = VirtualACL(self.fs)
        paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for item in (
            patch.object(os, 'name', 'nt'),
            patch.object(secure, '_windows_path_backend', return_value=paths),
            patch.object(discovery, '_windows_path_backend', return_value=paths),
            patch.object(win, '_api', side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, '_NativeBackend', return_value=self.acl),
        ):
            self.stack.enter_context(item)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.backend = ProductWriteBackend(self.snapshot, api=self.fs, acl_backend=self.acl)
        self.candidates = []
        def create(path, *, original_handle, user_sid):
            handle = self.fs.open_file(path, True)
            self.candidates.append(handle)
            self.fs.handles[handle][1]['index'] = 9000 + len(self.candidates)
            return handle
        self.fs.open_private_candidate = create
        self.fs.replace_existing = lambda target, stage: self.fs.objects.__setitem__(
            target, self.fs.objects.pop(stage))
        self.addCleanup(self.cleanup_fakes)

    def cleanup_fakes(self):
        self.fs.close_fail.clear()
        self.snapshot.close()
        for handle in list(self.fs.handles):
            self.fs.close(handle)

    def test_unknown_handoff_is_reported_without_guessing_handle(self):
        create = self.fs.open_private_candidate
        def interrupted(*args, **kwargs):
            create(*args, **kwargs)
            raise KeyboardInterrupt()
        self.fs.open_private_candidate = interrupted
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        error = caught.exception
        self.assertTrue(error.creation_unknown)
        self.assertTrue(error.release_unknown)
        self.assertEqual(error.target_state, 'NOT_REPLACED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')
        self.assertIn(self.candidates[0], self.fs.handles)
        self.assertNotIn(self.candidates[0], self.fs.close_calls)

    def test_empty_descriptor_failure_before_create_is_not_unknown(self):
        api, advapi, calls = NativeCandidateHandoff().make_api()
        advapi.GetSecurityInfo = FakeFunction(lambda *args: 5)
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, [])  # Neither CreateFileW nor LocalFree was called.
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')

    def test_empty_descriptor_exception_before_create_is_not_unknown(self):
        api, advapi, calls = NativeCandidateHandoff().make_api()
        def interrupted(*args):
            raise KeyboardInterrupt()
        advapi.GetSecurityInfo = FakeFunction(interrupted)
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, [])

    def _failed_security_query_with_descriptor(self, *, free_fails, interrupted=False):
        api, advapi, calls = NativeCandidateHandoff().make_api(
            free_result=1234 if free_fails else None)
        def failed_with_descriptor(*args):
            ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            if interrupted:
                raise KeyboardInterrupt()
            return 5
        advapi.GetSecurityInfo = FakeFunction(failed_with_descriptor)
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertEqual(caught.exception.release_unknown, free_fails)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, ['free'])

    def test_descriptor_on_failed_security_query_is_released(self):
        self._failed_security_query_with_descriptor(free_fails=False)

    def test_failed_security_query_with_failed_descriptor_release_is_unknown(self):
        self._failed_security_query_with_descriptor(free_fails=True)

    def test_interrupted_security_query_with_descriptor_releases_it(self):
        self._failed_security_query_with_descriptor(free_fails=False, interrupted=True)

    def test_dll_failure_before_create_is_not_unknown(self):
        api, _, calls = NativeCandidateHandoff().make_api()
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', side_effect=OSError('fake DLL failure'), create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, [])

    def test_invalid_sid_before_create_is_not_unknown(self):
        self.snapshot.close()
        self.fs.objects.pop(self.TARGET)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.backend = ProductWriteBackend(self.snapshot, api=self.fs, acl_backend=self.acl)
        self.backend.user = b'invalid'
        api, advapi, calls = NativeCandidateHandoff().make_api()
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, [])

    def _finite_conversion_failure(self, *, descriptor_returned, free_fails):
        self.snapshot.close()
        self.fs.objects.pop(self.TARGET)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        api, advapi, calls = NativeCandidateHandoff().make_api(
            free_result=1234 if free_fails else None)
        def convert(*args):
            if descriptor_returned:
                ctypes.cast(args[2], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 0
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(convert)
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b'new-fake', preflight=lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertEqual(caught.exception.release_unknown, free_fails)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, ['free'] if descriptor_returned else [])
        self.assertNotIn(self.TARGET, self.fs.objects)

    def test_empty_conversion_failure_before_finite_create_is_known(self):
        self._finite_conversion_failure(descriptor_returned=False, free_fails=False)

    def test_conversion_failure_before_finite_create_releases_descriptor(self):
        self._finite_conversion_failure(descriptor_returned=True, free_fails=False)

    def test_conversion_release_failure_before_finite_create_stays_unknown(self):
        self._finite_conversion_failure(descriptor_returned=True, free_fails=True)

    def test_create_call_interruption_remains_unknown(self):
        api, advapi, calls = NativeCandidateHandoff().make_api()
        def interrupted(*args):
            calls.append('create')
            raise KeyboardInterrupt()
        api._kernel.CreateFileW = FakeFunction(interrupted)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertTrue(caught.exception.creation_unknown)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, ['create', 'free'])

    def test_invalid_create_handle_keeps_creation_unknown(self):
        api, advapi, calls = NativeCandidateHandoff().make_api()
        api._kernel.CreateFileW = FakeFunction(
            lambda *args: calls.append('create') or win.INVALID_HANDLE_VALUE)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertTrue(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, ['create', 'free'])

    def test_security_attributes_failure_before_create_frees_descriptor(self):
        api, advapi, calls = NativeCandidateHandoff().make_api()
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        self.fs.open_private_candidate = lambda path, **kwargs: api.open_private_candidate(path, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(win, 'SECURITY_ATTRIBUTES', object()), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(calls, ['free'])

    def test_candidate_close_failure_is_unknown_and_not_retried(self):
        create = self.fs.open_private_candidate
        def create_bad_close(*args, **kwargs):
            handle = create(*args, **kwargs)
            self.fs.close_fail.add(handle)
            return handle
        self.fs.open_private_candidate = create_bad_close
        original = win.file_identity
        def identity(handle, **kwargs):
            if handle in self.candidates:
                raise OSError('fake failure before write')
            return original(handle, **kwargs)
        with patch.object(win, 'file_identity', side_effect=identity), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(caught.exception.creation_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(self.fs.close_calls.count(self.candidates[0]), 1)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')

    def test_normal_write_closes_candidates_and_keeps_backup(self):
        result = secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new-fake')
        self.assertEqual(self.fs.objects[result.backup]['data'], b'old-fake')
        self.assertEqual([self.fs.close_calls.count(h) for h in self.candidates], [1, 1])
        self.assertFalse(self.fs.handles)

    def test_commit_error_after_side_effect_remains_unknown(self):
        def replace_then_fail(target, stage):
            self.fs.objects[target] = self.fs.objects.pop(stage)
            raise OSError('fake commit failure after side effect')
        self.fs.replace_existing = replace_then_fail
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertEqual(caught.exception.target_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new-fake')
        self.assertEqual([self.fs.close_calls.count(h) for h in self.candidates], [1, 1])

    def test_target_change_after_backup_stops_before_stage(self):
        original_write = self.fs.write_all_and_flush
        def write_and_change_target(handle, data):
            original_write(handle, data)
            self.fs.objects[self.TARGET]['data'] = b'external-fake'
        self.fs.write_all_and_flush = write_and_change_target
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(len(self.candidates), 1)
        self.assertEqual(self.fs.close_calls.count(self.candidates[0]), 1)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'external-fake')
        commit.assert_not_called()

    def test_partial_stage_failure_keeps_verified_backup_and_never_commits(self):
        original_write = self.fs.write_all_and_flush
        def partial_stage(handle, data):
            if len(self.candidates) == 2:
                self.fs.handles[handle][1]['data'] = data[:3]
                raise OSError('fake partial stage write')
            original_write(handle, data)
        self.fs.write_all_and_flush = partial_stage
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        error = caught.exception
        self.assertEqual(error.target_state, 'NOT_REPLACED')
        self.assertFalse(error.creation_unknown)
        self.assertEqual(error.backup_status, 'VERIFIED')
        self.assertEqual(self.fs.objects[error.backup]['data'], b'old-fake')
        self.assertEqual(self.fs.objects[error.staging]['data'], b'new')
        self.assertEqual([self.fs.close_calls.count(h) for h in self.candidates], [1, 1])
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')
        commit.assert_not_called()

    def test_backup_readback_release_unknown_stops_without_retry(self):
        original_read = win.read_guarded_file
        def read(path, **kwargs):
            if '.model-proxy-backup-' in path:
                raise win.WindowsReadError('release_unknown')
            return original_read(path, **kwargs)
        with patch.object(win, 'read_guarded_file', side_effect=read), \
             patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(len(self.candidates), 1)
        self.assertEqual(self.fs.close_calls.count(self.candidates[0]), 1)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')
        commit.assert_not_called()

    def test_snapshot_close_unknown_is_not_retried_before_commit(self):
        lease = self.snapshot._target_lease
        original_close = lease.close
        def close_then_fail():
            original_close()
            raise OSError('fake close result unknown')
        with patch.object(lease, 'close', side_effect=close_then_fail) as close, \
             patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(caught.exception.backup_status, 'UNKNOWN')
        close.assert_called_once()
        commit.assert_not_called()

    def test_postcheck_release_unknown_reports_target_already_changed(self):
        original_replace = self.fs.replace_existing
        original_read = win.read_guarded_file
        committed = []
        def replace(target, stage):
            original_replace(target, stage)
            committed.append(True)
        def read(path, **kwargs):
            if path == self.TARGET and committed:
                raise win.WindowsReadError('release_unknown')
            return original_read(path, **kwargs)
        self.fs.replace_existing = replace
        with patch.object(win, 'read_guarded_file', side_effect=read), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertEqual(caught.exception.target_state, 'POSTCHECK_FAILED')
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.backup_status, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new-fake')
        self.assertEqual(len(committed), 1)

    def test_candidate_path_identity_mismatch_rejects_before_content(self):
        def change_candidate(typ, path, count):
            if typ == 'probe' and '.model-proxy-backup-' in path and path in self.fs.objects:
                self.fs.objects[path]['index'] += 1
        self.fs.on_open = change_candidate
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertIn(caught.exception.code, ('candidate_identity_unverified', 'path_changed'))
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertFalse(any(call[0] == 'write_flush' for call in self.fs.calls))
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')
        commit.assert_not_called()

    def test_candidate_same_identity_as_target_rejects_before_content(self):
        def create(path, *, original_handle, user_sid):
            handle = self.fs.open_file(path, True)
            self.candidates.append(handle)
            self.fs.handles[handle][1]['index'] = 801
            return handle
        self.fs.open_private_candidate = create
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertEqual(caught.exception.code, 'candidate_identity_unverified')
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertFalse(any(call[0] == 'write_flush' for call in self.fs.calls))
        self.assertEqual(self.fs.close_calls.count(self.candidates[0]), 1)
        commit.assert_not_called()

    def test_stage_same_identity_as_backup_rejects_before_stage_content(self):
        def create(path, *, original_handle, user_sid):
            handle = self.fs.open_file(path, True)
            self.candidates.append(handle)
            self.fs.handles[handle][1]['index'] = 9001
            return handle
        self.fs.open_private_candidate = create
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        error = caught.exception
        self.assertEqual(error.code, 'candidate_identity_unverified')
        self.assertEqual(error.target_state, 'NOT_REPLACED')
        self.assertEqual(error.backup_status, 'VERIFIED')
        self.assertEqual(self.fs.objects[error.backup]['data'], b'old-fake')
        self.assertEqual(self.fs.objects[error.staging].get('data', b''), b'')
        self.assertEqual([self.fs.close_calls.count(h) for h in self.candidates], [1, 1])
        commit.assert_not_called()

    def test_precommit_interruption_preserves_target_without_retry(self):
        checks = []
        def preflight():
            checks.append(True)
            if len(checks) == 2:
                raise KeyboardInterrupt()
            return True
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, preflight)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(caught.exception.backup_status, 'VERIFIED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')
        self.assertEqual([self.fs.close_calls.count(h) for h in self.candidates], [1, 1])
        self.assertEqual(len(checks), 2)
        commit.assert_not_called()

    def test_known_uncreated_candidate_failure_does_not_claim_unknown(self):
        from core.windows_secure_native import CandidateError
        def reject(*args, **kwargs):
            raise CandidateError('creation_policy_unverified', created=False)
        self.fs.open_private_candidate = reject
        with patch.object(self.fs, 'replace_existing') as commit, \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure._finite_write(self.snapshot, b'new-fake', self.backend, lambda: True)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertFalse(caught.exception.creation_unknown)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(self.candidates, [])
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old-fake')
        commit.assert_not_called()


class PrivateCreationPropagation(unittest.TestCase):
    ROOT = r'C:\fixture\project'

    def _assert_unknown_create_handoff(self, exception):
        fs = WritingFS()
        acl_backend = VirtualACL(fs)
        paths = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        api, advapi, calls = NativeCandidateHandoff().make_api()
        lost = []
        def descriptor(*args):
            ctypes.cast(args[2], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 1
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(descriptor)
        def interrupted(path, *args):
            calls.append('create')
            lost.append(fs.open_file(path, True))
            raise exception
        api._kernel.CreateFileW = FakeFunction(interrupted)
        fs.open_private_candidate = lambda path, *, user_sid: api.open_private_candidate(path, user_sid=user_sid)
        target = self.ROOT + r'\unknown-handoff'
        try:
            with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
                 self.assertRaises(win.WindowsPrivateCreationError) as caught:
                win.create_private_empty_file(target, backend=fs, acl_backend=acl_backend, paths_backend=paths)
            self.assertIsNone(caught.exception.created)
            self.assertTrue(caught.exception.release_unknown)
            self.assertEqual(caught.exception.code, 'release_unknown')
            self.assertEqual(calls, ['create', 'free'])
            self.assertIn(lost[0], fs.handles)
            self.assertNotIn(lost[0], fs.close_calls)
        finally:
            if lost and lost[0] in fs.handles:
                fs.close(lost[0])  # test-only cleanup of the fake handle
        self.assertFalse(fs.handles)

    def test_interrupted_create_handoff_is_unknown_without_guessing_close(self):
        self._assert_unknown_create_handoff(KeyboardInterrupt())

    def test_failed_create_handoff_is_unknown_without_guessing_close(self):
        self._assert_unknown_create_handoff(OSError('fake create outcome unknown'))

    def test_invalid_create_return_keeps_handle_release_known(self):
        fs = WritingFS()
        acl_backend = VirtualACL(fs)
        paths = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        api, advapi, calls = NativeCandidateHandoff().make_api()
        def descriptor(*args):
            ctypes.cast(args[2], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 1
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(descriptor)
        api._kernel.CreateFileW = FakeFunction(
            lambda *args: calls.append('create') or win.INVALID_HANDLE_VALUE)
        fs.open_private_candidate = lambda path, *, user_sid: api.open_private_candidate(path, user_sid=user_sid)
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(self.ROOT + r'\invalid-result', backend=fs,
                                          acl_backend=acl_backend, paths_backend=paths)
        self.assertIsNone(caught.exception.created)
        self.assertFalse(caught.exception.release_unknown)
        self.assertEqual(calls, ['create', 'free'])
        self.assertFalse(fs.handles)

    def _assert_conversion_failure(self, *, descriptor_returned, free_fails, interrupted=False):
        fs = WritingFS()
        acl_backend = VirtualACL(fs)
        paths = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        api, advapi, calls = NativeCandidateHandoff().make_api(
            free_result=1234 if free_fails else None)
        def convert(*args):
            if descriptor_returned:
                ctypes.cast(args[2], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            if interrupted:
                raise KeyboardInterrupt()
            return 0
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(convert)
        api._kernel.CreateFileW = FakeFunction(lambda *args: calls.append('create') or 4321)
        fs.open_private_candidate = lambda path, *, user_sid: api.open_private_candidate(path, user_sid=user_sid)
        target = self.ROOT + r'\convert-failure'
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(target, backend=fs, acl_backend=acl_backend, paths_backend=paths)
        self.assertIs(caught.exception.created, False)
        self.assertEqual(caught.exception.release_unknown, free_fails)
        if free_fails:
            self.assertEqual(caught.exception.code, 'release_unknown')
        self.assertEqual(calls, ['free'] if descriptor_returned else [])
        self.assertNotIn(target, fs.objects)
        self.assertFalse(fs.handles)

    def test_conversion_failure_with_empty_descriptor_is_known(self):
        self._assert_conversion_failure(descriptor_returned=False, free_fails=False)

    def test_conversion_failure_with_descriptor_releases_it(self):
        self._assert_conversion_failure(descriptor_returned=True, free_fails=False)

    def test_conversion_failure_with_unknown_release_stays_unknown(self):
        self._assert_conversion_failure(descriptor_returned=True, free_fails=True)

    def test_conversion_interruption_with_descriptor_releases_it(self):
        self._assert_conversion_failure(descriptor_returned=True, free_fails=False, interrupted=True)

    def _assert_created_file_with_unknown_release(self, *, close_fails):
        fs = WritingFS()
        acl_backend = VirtualACL(fs)
        paths = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        created_handles = []

        class Kernel:
            pass
        kernel = Kernel()
        def create(path, *args):
            handle = fs.open_file(path, True)
            created_handles.append(handle)
            if close_fails:
                fs.close_fail.add(handle)
            return handle
        kernel.CreateFileW = FakeFunction(create)
        kernel.LocalFree = FakeFunction(lambda _: 1234)  # failed descriptor release
        def close(handle):
            try:
                fs.close(handle)
                return 1
            except OSError:
                return 0
        kernel.CloseHandle = FakeFunction(close)

        class Advapi:
            pass
        advapi = Advapi()
        advapi.GetSecurityInfo = FakeFunction(lambda *args: 0)
        def descriptor(*args):
            ctypes.cast(args[2], ctypes.POINTER(ctypes.c_void_p))[0] = 1234
            return 1
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW = FakeFunction(descriptor)
        api = win._NativeAPI.__new__(win._NativeAPI)
        api._kernel = kernel
        fs.open_private_candidate = lambda path, *, user_sid: api.open_private_candidate(path, user_sid=user_sid)
        target = self.ROOT + r'\new-empty'
        with patch.object(ctypes, 'WinDLL', return_value=advapi, create=True), \
             patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(target, backend=fs, acl_backend=acl_backend, paths_backend=paths)
        self.assertIs(caught.exception.created, True)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.code, 'release_unknown')
        self.assertIn(target, fs.objects)
        self.assertEqual(fs.objects[target].get('data', b''), b'')
        self.assertEqual(fs.close_calls.count(created_handles[0]), 1)
        if close_fails:
            self.assertIn(created_handles[0], fs.handles)
            fs.close_fail.clear()
            fs.close(created_handles[0])  # clean up only this test's fake handle
        self.assertFalse(fs.handles)

    def test_created_file_and_unknown_descriptor_release_are_both_reported(self):
        self._assert_created_file_with_unknown_release(close_fails=False)

    def test_created_file_and_failed_handle_close_are_both_reported(self):
        self._assert_created_file_with_unknown_release(close_fails=True)

    def test_untyped_create_failure_keeps_creation_unknown(self):
        fs = WritingFS()
        acl_backend = VirtualACL(fs)
        paths = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        def unknown(*args, **kwargs):
            raise OSError('fake unknown creation outcome')
        fs.open_private_candidate = unknown
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(self.ROOT + r'\unknown', backend=fs,
                                          acl_backend=acl_backend, paths_backend=paths)
        self.assertIsNone(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(fs.handles)

    def test_explicit_unknown_create_and_release_remain_unknown(self):
        fs = WritingFS()
        acl_backend = VirtualACL(fs)
        paths = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        def unknown(*args, **kwargs):
            error = OSError('fake unknown outcome')
            error.created = None
            error.release_unknown = True
            error.code = 'release_unknown'
            raise error
        fs.open_private_candidate = unknown
        with self.assertRaises(win.WindowsPrivateCreationError) as caught:
            win.create_private_empty_file(self.ROOT + r'\unknown', backend=fs,
                                          acl_backend=acl_backend, paths_backend=paths)
        self.assertIsNone(caught.exception.created)
        self.assertTrue(caught.exception.release_unknown)
        self.assertFalse(fs.handles)


if __name__ == '__main__':
    unittest.main()
