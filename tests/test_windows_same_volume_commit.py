"""Synthetic Win32 only; no production files or native Windows claims."""
import os
import traceback
import unittest
from unittest.mock import patch

from core import discovery, platform_windows as win, windows_acl as acl
from core import windows_secure_files as secure
from core.windows_paths import PathLease, WindowsPathBackend
from core.windows_secure_native import ProductWriteBackend, _CommitEvidence
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_discovery import PathWinBridge
from tests.test_windows_secure_files_writer import WritingFS


class MultiVolumeFS(WritingFS):
    def __init__(self):
        super().__init__()
        self.objects['D:\\'] = dict(self.obj(101, directory=True), volume=18)
        self.objects[r'D:\backups'] = dict(self.obj(102, directory=True), volume=18)
        self.commits = []

    def volume_info(self, root):
        if root.upper().startswith('D:'):
            return (3, 'NTFS', 18, r'\Device\HarddiskVolume2')
        return super().volume_info(root)

    def _open(self, path, typ, create=False):
        handle = super()._open(path, typ, create)
        if create and path.upper().startswith('D:'):
            self.handles[handle][1]['volume'] = 18
        return handle

    def open_private_candidate(self, path, *, original_handle, user_sid):
        return self.open_file(path, True)

    def replace_existing(self, target, source):
        self.commits.append(('replace', target, source))
        self.objects[target] = self.objects.pop(source)

    def move_new(self, source, target):
        self.commits.append(('move', target, source))
        if target in self.objects:
            raise FileExistsError('fake conflict')
        self.objects[target] = self.objects.pop(source)


class SameVolumeCommitTests(unittest.TestCase):
    ROOT = r'C:\fixture\project'
    TARGET = ROOT + r'\target.json'
    STAGE = r'C:\fixture\runtime'
    BACKUP = r'D:\backups'

    def setUp(self):
        self.fs = MultiVolumeFS()
        self.fs.objects[self.TARGET] = dict(self.fs.obj(801), data=b'old')
        self.acl = VirtualACL(self.fs)
        paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        for item in (
            patch.object(os, 'name', 'nt'),
            patch.object(secure, '_windows_path_backend', return_value=paths),
            patch.object(discovery, '_windows_path_backend', return_value=paths),
            patch.object(win, '_api', side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, '_NativeBackend', return_value=self.acl),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.fs.close_fail.clear()
        self.snapshot.close()
        for handle in list(self.fs.handles):
            self.fs.close(handle)

    def backend(self, stage=None, backup=None):
        return ProductWriteBackend(self.snapshot, api=self.fs, acl_backend=self.acl,
                                   stage_directory=stage or self.STAGE,
                                   backup_directory=backup or self.BACKUP)

    def write(self, backend=None, preflight=lambda: True, make_backup=True):
        return secure._finite_write(self.snapshot, b'new', backend or self.backend(), preflight,
                                    make_backup=make_backup)

    def reject_after_stage(self, mutate, *, exists=True):
        if not exists:
            self.snapshot.close()
            del self.fs.objects[self.TARGET]
            self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        count = 0
        def preflight():
            nonlocal count
            count += 1
            if count == 2:
                mutate()
            return True
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(preflight=preflight)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(self.fs.commits, [])
        return caught.exception

    def stage_path(self):
        return next(path for path in self.fs.objects if path.startswith(self.STAGE + r'\.model-proxy-stage-'))

    def test_same_volume_different_directories_replace_and_cross_volume_backup(self):
        result = self.write()
        self.assertEqual(result.state, 'VERIFIED')
        self.assertTrue(result.backup.startswith(self.BACKUP + '\\'))
        self.assertEqual(self.fs.objects[result.backup]['data'], b'old')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')
        self.assertEqual(self.fs.commits[0][0], 'replace')
        self.assertFalse(self.fs.handles)

    def test_same_volume_new_target_no_overwrite_and_no_backup(self):
        self.snapshot.close()
        del self.fs.objects[self.TARGET]
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        absent = self.snapshot.identity_observation
        self.assertFalse(absent.exists)
        self.assertIsNone(absent.file_identity)
        self.assertIsNone(absent.marker)
        result = self.write()
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(result.backup_status, 'NONE')
        self.assertEqual(self.fs.commits[0][0], 'move')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')

    def test_public_api_explicit_directories_and_optional_no_backup(self):
        checks = []
        out = secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                  stage_directory=self.STAGE, backup_directory=self.BACKUP,
                                  make_backup=False,
                                  artifact_preflight=lambda role, path: checks.append((role, path)) or True)
        self.assertEqual(out.backup_status, 'NONE')
        self.assertEqual(out.state, 'VERIFIED')
        self.assertEqual([item[0] for item in checks], ['stage', 'stage'])
        self.assertEqual(checks[0], checks[1])

    def test_project_boundary_propagates_through_target_stage_backup_and_postcheck(self):
        self.snapshot.close()
        self.fs.objects['C:\\']['unsafe_group'] = True
        self.fs.objects[r'C:\fixture']['unsafe_group'] = True
        runtime = self.ROOT + r'\.runtime'
        stage = runtime + r'\tmp'
        backup = runtime + r'\backups'
        for index, path in enumerate((runtime, stage, backup), 4000):
            self.fs.objects[path] = self.fs.obj(index, directory=True)
        with self.assertRaises(secure.SecureSnapshotError):
            secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT,
                                              max_bytes=1024, trusted_boundary=self.ROOT)
        outcome = secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                      stage_directory=stage, backup_directory=backup,
                                      stage_boundary=self.ROOT, backup_boundary=self.ROOT,
                                      artifact_preflight=lambda role, path: True)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')
        self.assertEqual(self.fs.objects[outcome.backup]['data'], b'old')

    def test_external_target_and_independent_artifact_boundaries(self):
        self.snapshot.close()
        self.fs.objects['C:\\']['unsafe_group'] = True
        self.fs.objects[r'C:\fixture']['unsafe_group'] = True
        self.fs.objects['D:\\']['unsafe_group'] = True
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT,
                                              max_bytes=1024, trusted_boundary=self.ROOT)
        outcome = secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                      stage_directory=self.STAGE, stage_boundary=self.STAGE,
                                      backup_directory=self.BACKUP, backup_boundary=self.BACKUP,
                                      artifact_preflight=lambda role, path: True)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')
        self.assertEqual(self.fs.objects[outcome.backup]['data'], b'old')

    def test_boundary_does_not_authorize_unsafe_target_or_parent(self):
        self.snapshot.close()
        self.fs.objects[self.TARGET]['unsafe_group'] = True
        with self.assertRaises(secure.SecureSnapshotError):
            secure.capture_target(self.TARGET, authorized_root=self.ROOT,
                                  max_bytes=1024, trusted_boundary=self.ROOT)
        self.fs.objects[self.TARGET]['unsafe_group'] = False
        self.fs.objects[self.ROOT]['unsafe_group'] = True
        with self.assertRaises(secure.SecureSnapshotError):
            secure.capture_target(self.TARGET, authorized_root=self.ROOT,
                                  max_bytes=1024, trusted_boundary=self.ROOT)

    def test_artifact_role_conflict_stops_before_stage_creation(self):
        checks = []
        def conflicts(role, path):
            checks.append((role, path))
            return role != 'stage'
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                stage_directory=self.STAGE, backup_directory=self.BACKUP,
                                artifact_preflight=conflicts)
        self.assertEqual(caught.exception.code, 'artifact_preflight_failed')
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual([role for role, _ in checks], ['backup', 'stage'])
        self.assertNotIn(checks[-1][1], self.fs.objects)
        self.assertFalse(self.fs.commits)

    def test_artifact_role_conflict_after_creation_stops_before_commit(self):
        checks = []
        def conflicts(role, path):
            checks.append((role, path))
            return len(checks) < 4
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                stage_directory=self.STAGE, backup_directory=self.BACKUP,
                                artifact_preflight=conflicts)
        self.assertEqual(caught.exception.code, 'artifact_preflight_failed')
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual([role for role, _ in checks], ['backup', 'stage', 'backup', 'stage'])
        self.assertFalse(self.fs.commits)

    def test_absent_protected_role_collision_requires_authorization_before_creation(self):
        protected = self.BACKUP + r'\.model-proxy-backup-' + 'a' * 24
        read_paths = []
        self.fs.on_read = lambda handle, obj: read_paths.append(self.fs.handles[handle][0])
        for directory_options in (
                {'stage_directory': self.STAGE},
                {'backup_directory': self.BACKUP},
                {'stage_directory': self.STAGE, 'backup_directory': self.BACKUP}):
            with self.subTest(directory_options=directory_options):
                with patch('secrets.token_hex', return_value='a' * 24), \
                     patch('core.windows_secure_native.ProductWriteBackend') as factory, \
                     self.assertRaises(secure.SecureWriteError) as caught:
                    secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                        **directory_options)
                self.assertEqual(caught.exception.code, 'artifact_preflight_required')
                factory.assert_not_called()
                self.assertNotIn(protected, self.fs.objects)
                self.assertFalse(any(call[0] == 'write_flush' for call in self.fs.calls))
                self.assertFalse(self.fs.commits)

        checks = []
        def deny_collision(role, path):
            checks.append((role, path))
            return path != protected
        with patch('secrets.token_hex', return_value='a' * 24), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                stage_directory=self.STAGE, backup_directory=self.BACKUP,
                                artifact_preflight=deny_collision)
        self.assertEqual(caught.exception.code, 'artifact_preflight_failed')
        self.assertEqual(checks, [('backup', protected)])
        self.assertNotIn(protected, self.fs.objects)
        self.assertNotIn(protected, read_paths)
        self.assertNotIn('old', str(caught.exception))
        self.assertFalse(any(call[0] == 'write_flush' for call in self.fs.calls))
        self.assertFalse(self.fs.commits)

    def test_artifact_callback_exception_is_sanitized_in_both_phases(self):
        class SensitiveError(Exception):
            code = 'synthetic-secret-code'
        for fail_call in (1, 2, 3, 4):
            with self.subTest(fail_call=fail_call):
                checks = []
                def authorize(role, path):
                    checks.append((role, path))
                    if len(checks) == fail_call:
                        raise SensitiveError('synthetic-secret-message')
                    return True
                with self.assertRaises(secure.SecureWriteError) as caught:
                    secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                        stage_directory=self.STAGE, backup_directory=self.BACKUP,
                                        artifact_preflight=authorize)
                error = caught.exception
                rendered = ''.join(traceback.format_exception(error))
                self.assertEqual(error.code, 'artifact_preflight_failed')
                self.assertEqual(error.state, 'NOT_REPLACED')
                self.assertNotIn('synthetic-secret', rendered)
                self.assertIsNone(error.__context__)
                self.assertFalse(self.fs.commits)
                if fail_call == 1:
                    self.assertNotIn(checks[0][1], self.fs.objects)
                # Each failed transaction consumes the snapshot; recreate only
                # the synthetic fixture for the next phase.
                self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT,
                                                      max_bytes=1024)

    def test_cross_volume_stage_is_rejected_without_copy_or_commit(self):
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(self.backend(stage=self.BACKUP, backup=self.STAGE))
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(caught.exception.code, 'commit_volume_or_identity_unverified')
        self.assertEqual(self.fs.commits, [])
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old')
        self.assertFalse(any(path.startswith(self.BACKUP + r'\.model-proxy-stage-')
                             for path in self.fs.objects))

    def test_same_serial_but_different_device_is_not_same_volume(self):
        volume_info = self.fs.volume_info
        self.fs.volume_info = lambda root: ((3, 'NTFS', 17, r'\Device\HarddiskVolume2')
                                            if root.upper().startswith('D:') else volume_info(root))
        for path in ('D:\\', self.BACKUP):
            self.fs.objects[path]['volume'] = 17
        original_open = self.fs._open
        def create(path, typ, create=False):
            handle = original_open(path, typ, create)
            if create and path.upper().startswith('D:'):
                self.fs.handles[handle][1]['volume'] = 17
            return handle
        self.fs._open = create
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(self.backend(stage=self.BACKUP, backup=self.STAGE))
        self.assertEqual(caught.exception.code, 'commit_volume_or_identity_unverified')
        self.assertFalse(self.fs.commits)

    def test_different_drive_spellings_same_device_are_not_automatically_rejected(self):
        volume_info = self.fs.volume_info
        self.fs.volume_info = lambda root: (volume_info('C:\\')
                                            if root.upper().startswith('D:') else volume_info(root))
        for path in ('D:\\', self.BACKUP):
            self.fs.objects[path]['volume'] = 17
        original_open = self.fs._open
        def create(path, typ, create=False):
            handle = original_open(path, typ, create)
            if create and path.upper().startswith('D:'):
                self.fs.handles[handle][1]['volume'] = 17
            return handle
        self.fs._open = create
        result = self.write(self.backend(stage=self.BACKUP, backup=self.STAGE))
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')

    def test_missing_target_alias_is_rejected_before_create(self):
        for name in ('.model-proxy-stage-' + 'b' * 24, 'MODEL~1'):
            with self.subTest(name=name):
                self.snapshot.close()
                self.TARGET = self.ROOT + '\\' + name
                self.fs.objects.pop(self.TARGET, None)
                self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
                with patch('secrets.token_hex', return_value='b' * 24), \
                     self.assertRaises(secure.SecureWriteError) as caught:
                    self.write(self.backend(stage=self.ROOT))
                self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
                self.assertFalse(caught.exception.creation_unknown)
                self.assertNotIn(self.TARGET, self.fs.objects)
                self.assertFalse(self.fs.commits)

    def test_stage_parent_replacement_rejected(self):
        self.reject_after_stage(lambda: self.fs.objects.__setitem__(
            self.STAGE, dict(self.fs.obj(2500, directory=True))))

    def test_valid_looking_forged_volume_rejected_after_handoff(self):
        backend = self.backend()
        release = backend.release_precommit
        def forged():
            release()
            evidence = backend._commit_evidence
            backend._commit_evidence = _CommitEvidence(
                evidence.target, evidence.source,
                (3, 'NTFS', 18, r'\Device\HarddiskVolume2'), evidence.exists)
        backend.release_precommit = forged
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(backend)
        self.assertNotEqual(caught.exception.target_state, 'VERIFIED')
        self.assertEqual(caught.exception.code, 'commit_volume_or_identity_unverified')
        self.assertFalse(self.fs.commits)

    def test_stage_replaced_after_handle_handoff_is_reobserved(self):
        backend = self.backend()
        release = backend.release_precommit
        def swap():
            release()
            self.fs.objects[self.stage_path()] = dict(self.fs.obj(2502), data=b'new')
        backend.release_precommit = swap
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(backend)
        self.assertNotEqual(caught.exception.target_state, 'VERIFIED')
        self.assertFalse(self.fs.commits)

    def test_target_parent_replacement_rejected(self):
        self.reject_after_stage(lambda: self.fs.objects.__setitem__(
            self.ROOT, dict(self.fs.obj(2501, directory=True))))

    def test_stage_file_replacement_rejected(self):
        def change():
            path = self.stage_path()
            self.fs.objects[path] = dict(self.fs.obj(2502), data=b'new')
        self.reject_after_stage(change)

    def test_target_file_replacement_rejected(self):
        self.reject_after_stage(lambda: self.fs.objects.__setitem__(
            self.TARGET, dict(self.fs.obj(2504), data=b'old')))

    def test_stage_permissions_changed_rejected(self):
        self.reject_after_stage(lambda: self.fs.objects[self.STAGE].__setitem__('unsafe_group', True))

    def test_stage_directory_create_access_denied(self):
        read = self.acl.read_observation
        def denied(handle, kind, required):
            if self.fs.handles[handle][0] == self.STAGE and required & 0x2:
                raise OSError('fake create access denied')
            return read(handle, kind, required)
        self.acl.read_observation = denied
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write()
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertFalse(self.fs.commits)

    def test_target_permissions_changed_rejected(self):
        self.reject_after_stage(lambda: self.fs.objects[self.TARGET].__setitem__('unsafe_group', True))

    def test_target_write_authorization_is_independent_of_stage(self):
        read = acl.read_stable_evidence
        target_handle = self.snapshot._target_lease._handles[-1].raw
        def denied(handle, *, kind, required_access, policy='private', backend=None):
            if handle == target_handle and kind == 'file' and required_access & 0x10000:
                raise acl.ACLVerificationError('access_unverified')
            return read(handle, kind=kind, required_access=required_access,
                        policy=policy, backend=backend)
        with patch.object(acl, 'read_stable_evidence', side_effect=denied), \
             self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(self.snapshot, b'new', preflight=lambda: True,
                                stage_directory=self.STAGE, backup_directory=self.BACKUP,
                                artifact_preflight=lambda role, path: True)
        self.assertEqual(caught.exception.code, 'access_unverified')
        self.assertFalse(self.fs.commits)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'old')

    def test_missing_target_appears_before_commit(self):
        self.reject_after_stage(lambda: self.fs.objects.__setitem__(
            self.TARGET, dict(self.fs.obj(2503), data=b'other')), exists=False)
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'other')

    def test_stage_hardlink_rejected(self):
        self.reject_after_stage(lambda: self.fs.objects[self.stage_path()].__setitem__('links', 2))

    def test_native_call_after_side_effect_is_unknown(self):
        previous = self.snapshot.identity_observation
        def partial(target, source):
            self.fs.commits.append(('replace', target, source))
            self.fs.objects[target] = self.fs.objects.pop(source)
            raise OSError('fake uncertain result')
        self.fs.replace_existing = partial
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write()
        self.assertEqual(caught.exception.target_state, 'UNKNOWN')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')
        self.assertEqual(len(self.fs.commits), 1)
        candidate = caught.exception.candidate
        self.assertTrue(candidate.exists)
        self.assertEqual(candidate.parent, previous.parent)
        self.assertNotEqual(candidate.file_identity, previous.file_identity)
        with secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024) as current:
            observed = current.identity_observation
            self.assertEqual(current.contents, b'new')
            self.assertEqual((observed.parent, observed.file_identity, observed.file_protection),
                             (candidate.parent, candidate.file_identity, candidate.file_protection))
        self.assertNotIn('new', repr(candidate))

    def test_missing_target_unknown_retains_candidate_for_reconciliation(self):
        self.snapshot.close()
        del self.fs.objects[self.TARGET]
        self.snapshot = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        before = self.snapshot.identity_observation
        def partial(source, target):
            self.fs.commits.append(('move', target, source))
            self.fs.objects[target] = self.fs.objects.pop(source)
            raise OSError('fake unknown move result')
        self.fs.move_new = partial
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write()
        self.assertEqual(caught.exception.target_state, 'UNKNOWN')
        candidate = caught.exception.candidate
        self.assertTrue(candidate.exists)
        self.assertFalse(before.exists)
        self.assertEqual(candidate.parent, before.parent)
        with secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024) as current:
            observed = current.identity_observation
            self.assertEqual(current.contents, b'new')
            self.assertEqual((candidate.parent, candidate.file_identity, candidate.file_protection),
                             (observed.parent, observed.file_identity, observed.file_protection))

    def test_postcheck_failure_is_not_reported_as_success(self):
        backend = self.backend()
        backend.postcheck = lambda: (_ for _ in ()).throw(OSError('fake read failure'))
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write(backend)
        self.assertEqual(caught.exception.target_state, 'POSTCHECK_FAILED')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')
        self.assertTrue(caught.exception.candidate.exists)

    def test_release_failure_before_call_is_unknown_and_no_commit(self):
        backend = self.backend()
        original = PathLease.close
        failures = []
        def close(lease):
            original(lease)
            if backend._stage_lease is lease and not failures:
                failures.append(True)
                raise OSError('fake uncertain close')
        with patch.object(PathLease, 'close', close), \
             self.assertRaises(secure.SecureWriteError) as caught:
            self.write(backend)
        self.assertTrue(caught.exception.release_unknown)
        self.assertEqual(caught.exception.target_state, 'NOT_REPLACED')
        self.assertEqual(self.fs.commits, [])
        self.assertEqual(failures, [True])

    def test_parent_close_failure_after_verified_target_is_unknown_without_backup(self):
        backend = self.backend()
        release = backend.release_precommit
        parent = []
        def retain_parent():
            release()
            parent.append(backend._target_parent_lease)
        backend.release_precommit = retain_parent
        close = PathLease.close
        failures = []
        def fail_close(lease):
            close(lease)
            if parent and lease is parent[0] and not failures:
                failures.append(True)
                raise OSError('synthetic uncertain close')
        with patch.object(PathLease, 'close', fail_close), \
             self.assertRaises(secure.SecureWriteError) as caught:
            self.write(backend, make_backup=False)
        error = caught.exception
        self.assertEqual((error.state, error.target_state), ('UNKNOWN', 'UNKNOWN'))
        self.assertTrue(error.release_unknown)
        self.assertTrue(error.target_was_verified)
        self.assertEqual(error.backup_status, 'NONE')
        self.assertEqual(self.fs.objects[self.TARGET]['data'], b'new')
        self.assertEqual(len(self.fs.commits), 1)
        self.assertEqual(failures, [True])

    def test_backup_parent_changed_during_commit_is_not_verified(self):
        replace = self.fs.replace_existing
        def replace_and_swap_parent(target, source):
            replace(target, source)
            self.fs.objects[self.BACKUP] = dict(self.fs.obj(2200, directory=True), volume=18)
        self.fs.replace_existing = replace_and_swap_parent
        with self.assertRaises(secure.SecureWriteError) as caught:
            self.write()
        self.assertEqual(caught.exception.target_state, 'UNKNOWN')
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertTrue(caught.exception.target_was_verified)
        self.assertEqual(caught.exception.backup_status, 'UNKNOWN')
        self.assertFalse(caught.exception.backup_verified)


if __name__ == '__main__':
    unittest.main()
