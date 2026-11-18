"""Native Mac tests using only caller-created fake files and directories."""
import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.mac_config_path import MacConfigError, capture_config, open_target_parent, verify_config
from core.mac_config_writer import (
    MacWriteError, WriteAuthorization, _check_created, _open_private, replace_config,
    validate_write_authorization,
)
from core.secure_files import (
    RuntimeWriteLocations, SecureSnapshotError, SecureWriteError,
    capture_private_directory, capture_target, mac_private_protection,
    secure_replace_runtime,
)


class MacRuntimeStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['MP_TEST_ROOT'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config'
        self.runtime = self.root / '.runtime'
        self.stage = self.runtime / 'tmp'
        self.backups = self.runtime / 'backups'
        for directory in (self.config, self.runtime, self.stage, self.backups):
            directory.mkdir(mode=0o700)
        self.target = self.config / 'config.json'
        self.target.write_bytes(b'{"secret":"fake-old"}\n')
        self.target.chmod(0o600)
        self.alias = self.root / 'config-link'
        self.alias.symlink_to('config/config.json')

    def locations(self, *, target_staging=False, backup=True):
        return RuntimeWriteLocations(
            capture_private_directory(self.stage, purpose='staging'),
            capture_private_directory(self.backups, purpose='backup') if backup else None,
            target_staging,
        )

    def config_snapshot(self, path=None, *, external_confirmed=False):
        snapshot = capture_config(str(path or self.alias))
        with open_target_parent(snapshot) as (fd, _):
            info = os.fstat(fd)
            authorization = WriteAuthorization(
                snapshot.target_path, (info.st_dev, info.st_ino),
                snapshot.final_identity[:2], external_confirmed=external_confirmed)
        return snapshot, authorization

    def fake_external_config(self):
        external = self.root / 'external'
        external.mkdir(mode=0o700)
        target = external / 'business.json'
        target.write_bytes(b'fake external original')
        target.chmod(0o600)
        link = self.root / 'external-config-link'
        link.symlink_to(target)
        return external, target, link

    def config_write(self, snapshot, authorization, locations, before=lambda: None):
        return replace_config(snapshot, b'{"new":true}\n', authorization,
                              preflight=lambda _: True, before_replace=before,
                              locations=locations)

    def target_snapshot(self):
        return capture_target(str(self.target), authorized_root=str(self.root),
                              protection_reader=mac_private_protection, max_bytes=1024)

    def assert_no_stage(self):
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(list(self.config.glob('.model-proxy-*')), [])

    def test_config_same_volume_separate_directories_and_private_backup(self):
        old_backup = self.config / 'config.json.bak.existing'
        old_backup.write_bytes(b'old fake backup')
        old_backup.chmod(0o600)
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        validate_write_authorization(snapshot, auth, locations=locations)
        old_link = os.lstat(self.alias).st_ino
        real_replace = os.replace

        def inspect(src, dst, *, src_dir_fd, dst_dir_fd):
            self.assertEqual(os.fstat(src_dir_fd).st_ino, self.stage.stat().st_ino)
            self.assertEqual(os.fstat(dst_dir_fd).st_ino, self.config.stat().st_ino)
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with patch('core.mac_config_writer.os.replace', side_effect=inspect):
            outcome = self.config_write(snapshot, auth, locations)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual(os.lstat(self.alias).st_ino, old_link)
        backup = Path(outcome.backup_name)
        self.assertEqual(backup.parent, self.backups)
        self.assertEqual(backup.read_bytes(), snapshot.data)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(old_backup.read_bytes(), b'old fake backup')
        self.assert_no_stage()

    def test_no_authority_is_inferred_from_read_or_other_location(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        with self.assertRaises(MacWriteError):
            validate_write_authorization(snapshot, auth)  # legacy grant absent
        bad = WriteAuthorization(snapshot.target_path + '-other',
                                 auth.parent_identity, auth.target_identity)
        with self.assertRaises(MacWriteError):
            self.config_write(snapshot, bad, locations)
        swapped = RuntimeWriteLocations(locations.backup, locations.staging)
        with self.assertRaises(MacWriteError):
            self.config_write(snapshot, auth, swapped)
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_cross_volume_requires_explicit_exception_and_stages_by_target(self):
        # Only the volume check is mocked; all targets and writes are disposable.
        external, target, link = self.fake_external_config()
        snapshot, no_confirmation = self.config_snapshot(link)
        _, confirmed = self.config_snapshot(link, external_confirmed=True)
        calls = []
        real_replace = os.replace

        def replace(src, dst, *, src_dir_fd, dst_dir_fd):
            calls.append((os.fstat(src_dir_fd).st_ino, os.fstat(dst_dir_fd).st_ino,
                          external.stat().st_ino))
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with patch('core.mac_config_writer._same_volume', return_value=False):
            for auth, locations in (
                    (no_confirmation, self.locations(target_staging=True)),
                    (confirmed, self.locations(target_staging=False))):
                with self.subTest(external_confirmed=auth.external_confirmed,
                                  target_staging=locations.allow_target_staging):
                    with self.assertRaises(MacWriteError) as caught:
                        validate_write_authorization(snapshot, auth, locations=locations)
                    self.assertEqual(caught.exception.code, 'cross_volume_staging_not_authorized')
                    with self.assertRaises(MacWriteError) as caught:
                        self.config_write(snapshot, auth, locations)
                    self.assertEqual(caught.exception.code, 'cross_volume_staging_not_authorized')
                    self.assertEqual(caught.exception.state, 'NOT_REPLACED')
                    self.assertEqual(target.read_bytes(), snapshot.data)
                    self.assertEqual(list(self.backups.iterdir()), [])
                    self.assertEqual(list(external.glob('.model-proxy-*')), [])
            locations = self.locations(target_staging=True)
            validate_write_authorization(snapshot, confirmed, locations=locations)
            with patch('core.mac_config_writer.os.replace', side_effect=replace):
                result = self.config_write(snapshot, confirmed, locations)
                self.assertEqual(result.state, 'VERIFIED')
                backup = Path(result.backup_name)
                self.assertEqual(backup.parent, self.backups)
                self.assertEqual(backup.read_bytes(), snapshot.data)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                self.assertEqual(target.read_bytes(), b'{"new":true}\n')
                after, restore_auth = self.config_snapshot(link, external_confirmed=True)
                restored = replace_config(after, backup.read_bytes(), restore_auth,
                                          preflight=lambda _: True, before_replace=lambda: None,
                                          locations=locations)
        self.assertEqual(restored.state, 'VERIFIED')
        self.assertEqual(target.read_bytes(), snapshot.data)
        self.assertEqual(backup.read_bytes(), snapshot.data)
        self.assertTrue(link.is_symlink())
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(src == dst == parent for src, dst, parent in calls))
        self.assertEqual(list(external.glob('.model-proxy-*')), [])
        self.assert_no_stage()

    def test_mock_cross_volume_failures_reach_injection_without_commit(self):
        external, target, link = self.fake_external_config()
        snapshot, auth = self.config_snapshot(link, external_confirmed=True)
        locations = self.locations(target_staging=True)
        with patch('core.mac_config_writer._same_volume', return_value=False):
            with patch('core.secure_files.os.write',
                       side_effect=OSError(errno.ENOSPC, 'fake full')) as failing_write:
                with self.assertRaises(MacWriteError) as caught:
                    self.config_write(snapshot, auth, locations)
            self.assertGreater(failing_write.call_count, 0)
            self.assertEqual(caught.exception.code, 'filesystem_or_backend_error')
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertIsNone(caught.exception.backup_name)
            self.assertEqual(target.read_bytes(), snapshot.data)
            self.assertEqual(list(self.backups.iterdir()), [])

            def fail_staging(fd, name, *args):
                if name.startswith('.model-proxy-config-'):
                    raise OSError(errno.ENOSPC, 'fake staging full')
                return _open_private(fd, name, *args)

            with patch('core.mac_config_writer._open_private',
                       side_effect=fail_staging) as failing_stage:
                with self.assertRaises(MacWriteError) as caught:
                    self.config_write(snapshot, auth, locations)
            self.assertEqual(failing_stage.call_count, 2)
            self.assertTrue(failing_stage.call_args.args[1].startswith('.model-proxy-config-'))
            self.assertEqual(caught.exception.code, 'filesystem_or_backend_error')
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertEqual(Path(caught.exception.backup_name).read_bytes(), snapshot.data)
            self.assertEqual(target.read_bytes(), snapshot.data)
        self.assertEqual(list(external.glob('.model-proxy-*')), [])
        self.assert_no_stage()

    def test_real_cross_volume_backup_and_restore(self):
        volume = os.environ.get('MP_TEST_CROSS_VOLUME_ROOT')
        if not volume:
            self.skipTest('requires a caller-created disposable APFS volume')
        if not os.path.isdir(volume) or not os.access(volume, os.W_OK):
            self.skipTest('disposable cross-volume root is not an available writable directory')
        if os.stat(volume).st_dev == self.runtime.stat().st_dev:
            self.skipTest('provided disposable root is not on a second volume')
        with tempfile.TemporaryDirectory(dir=volume, prefix='fake-config-') as external:
            external = Path(external)
            self.assertNotEqual(external.stat().st_dev, self.runtime.stat().st_dev)
            target = external / 'business.json'
            target.write_bytes(b'fake external original')
            target.chmod(0o600)
            link = self.root / 'external-config-link'
            link.symlink_to(target)
            snapshot, auth = self.config_snapshot(link, external_confirmed=True)
            with self.assertRaises(MacWriteError) as caught:
                self.config_write(snapshot, auth, self.locations())
            self.assertEqual(caught.exception.code, 'cross_volume_staging_not_authorized')
            with patch('core.secure_files.os.write',
                       side_effect=OSError(errno.ENOSPC, 'fake full')) as failing_write:
                with self.assertRaises(MacWriteError) as caught:
                    self.config_write(snapshot, auth, self.locations(target_staging=True))
            self.assertGreater(failing_write.call_count, 0)
            self.assertEqual(caught.exception.code, 'filesystem_or_backend_error')
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertIsNone(caught.exception.backup_name)
            self.assertEqual(target.read_bytes(), snapshot.data)
            self.assertEqual(list(self.backups.iterdir()), [])
            self.assertEqual(list(external.glob('.model-proxy-*')), [])
            result = self.config_write(snapshot, auth, self.locations(target_staging=True))
            backup = Path(result.backup_name)
            self.assertNotEqual(target.stat().st_dev, backup.stat().st_dev)
            self.assertEqual(backup.read_bytes(), b'fake external original')
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(target.read_bytes(), b'{"new":true}\n')
            after, restore_auth = self.config_snapshot(link, external_confirmed=True)
            restored = replace_config(after, backup.read_bytes(), restore_auth,
                                      preflight=lambda _: True, before_replace=lambda: None,
                                      locations=self.locations(target_staging=True))
            self.assertEqual(restored.state, 'VERIFIED')
            self.assertEqual(target.read_bytes(), snapshot.data)
            self.assertEqual(backup.read_bytes(), snapshot.data)
            self.assertTrue(link.is_symlink())
            self.assertEqual(list(external.glob('.model-proxy-*')), [])
            secure_snapshot = capture_target(target, authorized_root=external,
                                             protection_reader=mac_private_protection,
                                             max_bytes=1024, trusted_boundary=external)
            secure_result = secure_replace_runtime(
                secure_snapshot, b'fake sidecar C', max_bytes=1024,
                locations=self.locations(target_staging=True))
            secure_backup = Path(secure_result.backup)
            self.assertNotEqual(target.stat().st_dev, secure_backup.stat().st_dev)
            self.assertEqual(secure_backup.read_bytes(), snapshot.data)
            self.assertEqual(stat.S_IMODE(secure_backup.stat().st_mode), 0o600)
            restored_secure = secure_replace_runtime(
                secure_result.snapshot, secure_backup.read_bytes(), max_bytes=1024,
                locations=self.locations(target_staging=True))
            self.assertEqual(restored_secure.snapshot.contents, snapshot.data)
            self.assertEqual(secure_backup.read_bytes(), snapshot.data)
            self.assertEqual(list(external.glob('.model-proxy-*')), [])
            self.assert_no_stage()

    def test_backup_unprotected_or_replaced_is_rejected_before_commit(self):
        snapshot, auth = self.config_snapshot()
        self.backups.chmod(0o755)
        with self.assertRaises(SecureSnapshotError):
            self.locations()
        self.backups.chmod(0o700)
        locations = self.locations()
        moved = self.runtime / 'old-backups'
        self.backups.rename(moved)
        self.backups.mkdir(mode=0o700)
        with self.assertRaises(MacWriteError):
            self.config_write(snapshot, auth, locations)
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        self.assertEqual(list(moved.iterdir()), [])
        self.assert_no_stage()

    def test_staging_parent_swap_is_detected_and_owned_file_cleaned(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        moved = self.runtime / 'old-tmp'

        def change():
            self.stage.rename(moved)
            self.stage.mkdir(mode=0o700)

        with self.assertRaises(MacWriteError) as caught:
            self.config_write(snapshot, auth, locations, before=change)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        self.assertEqual(list(moved.iterdir()), [])
        self.assert_no_stage()

    def test_last_boundary_guard_cannot_swap_staged_file(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 3:
                staged = next(self.stage.iterdir())
                staged.unlink()
                staged.write_bytes(b'fake attacker')
                staged.chmod(0o600)

        with self.assertRaises(MacWriteError) as caught:
            replace_config(snapshot, b'new', auth, preflight=lambda _: True,
                           before_replace=lambda: None, boundary_guard=guard,
                           locations=locations)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.cleanup_unknown)
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        # An identity-mismatched object is not deleted as if it were ours.
        Path(caught.exception.staging_name).unlink()

    def test_backup_copy_failure_does_not_commit_or_stage(self):
        snapshot, auth = self.config_snapshot()
        with patch('core.mac_config_writer._open_private', side_effect=OSError('fake backup failure')):
            with self.assertRaises(MacWriteError) as caught:
                self.config_write(snapshot, auth, self.locations())
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertIsNone(caught.exception.backup_name)
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        self.assert_no_stage()

    def test_changed_backup_before_commit_blocks_replacement(self):
        snapshot, auth = self.config_snapshot()

        def change_backup():
            next(self.backups.iterdir()).write_bytes(b'tampered fake backup')

        with self.assertRaises(MacWriteError) as caught:
            self.config_write(snapshot, auth, self.locations(), before=change_backup)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        self.assert_no_stage()

    def test_check_created_detects_same_inode_mutation_after_read(self):
        snapshot, _ = self.config_snapshot()
        created = self.stage / 'fake-created'
        created.write_bytes(b'original')
        created.chmod(0o600)
        identity = (created.stat().st_dev, created.stat().st_ino)
        actual_read = os.read
        changed = False

        def read_then_modify(fd, size):
            nonlocal changed
            data = actual_read(fd, size)
            info = os.fstat(fd)
            if data and (info.st_dev, info.st_ino) == identity and not changed:
                changed = True
                created.write_bytes(b'modified')
            return data

        directory_fd = os.open(self.stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch('core.mac_config_writer.os.read', side_effect=read_then_modify):
                with self.assertRaises(MacWriteError) as caught:
                    _check_created(directory_fd, created.name, identity,
                                   b'original', snapshot.protection[1])
        finally:
            os.close(directory_fd)
        self.assertTrue(changed)
        self.assertEqual(caught.exception.code, 'staging_changed')

    def test_bounded_parent_policy_is_explicit_and_rechecked(self):
        high = self.root / 'unsafe-high'
        direct = high / 'approved-parent'
        high.mkdir(mode=0o700)
        direct.mkdir(mode=0o700)
        high.chmod(0o777)
        target = direct / 'sidecar.json'
        target.write_bytes(b'fake B')
        target.chmod(0o600)
        args = dict(authorized_root=direct, protection_reader=mac_private_protection,
                    max_bytes=1024)
        with self.assertRaises(SecureSnapshotError):
            capture_target(target, **args)
        snapshot = capture_target(target, trusted_boundary=direct, **args)
        self.assertEqual(snapshot.contents, b'fake B')
        self.assertIsNone(next(entry[2] for entry in snapshot.parents if entry[0] == str(high)))
        outcome = secure_replace_runtime(snapshot, b'fake C', max_bytes=1024,
                                        locations=self.locations(backup=False), make_backup=False)
        self.assertEqual(outcome.snapshot.trusted_boundary, str(direct))
        self.assertTrue(outcome.candidate.matches(outcome.snapshot))
        high.chmod(0o755)
        from core.secure_files import target_unchanged
        self.assertFalse(target_unchanged(outcome.snapshot,
                                          protection_reader=mac_private_protection, max_bytes=1024))

    def test_changed_link_chain_is_rejected_without_changing_targets(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        other = self.config / 'other.json'
        other.write_bytes(b'fake other')
        other.chmod(0o600)

        def change():
            self.alias.unlink()
            self.alias.symlink_to('config/other.json')

        with self.assertRaises(MacWriteError):
            self.config_write(snapshot, auth, locations, before=change)
        self.assertEqual(self.target.read_bytes(), snapshot.data)
        self.assertEqual(other.read_bytes(), b'fake other')
        self.assert_no_stage()

    def test_directory_link_dotdot_is_preserved(self):
        inner = self.config / 'inner'
        inner.mkdir(mode=0o700)
        directory_link = self.root / 'directory-link'
        directory_link.symlink_to('config/inner')
        source = str(directory_link) + '/../config.json'
        snapshot, auth = self.config_snapshot(source)
        result = self.config_write(snapshot, auth, self.locations())
        self.assertEqual(result.state, 'VERIFIED')
        self.assertTrue(directory_link.is_symlink())
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')

    def test_unknown_after_actual_replace_does_not_claim_failure_or_rollback(self):
        snapshot, auth = self.config_snapshot()
        locations = self.locations()
        real_replace = os.replace

        def unknown(*args, **kwargs):
            real_replace(*args, **kwargs)
            raise OSError('fake interrupted syscall')

        with patch('core.mac_config_writer.os.replace', side_effect=unknown):
            with self.assertRaises(MacWriteError) as caught:
                self.config_write(snapshot, auth, locations)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual(Path(caught.exception.backup_name).read_bytes(), snapshot.data)
        self.assert_no_stage()

    def test_unknown_cleanup_failure_reports_exact_owned_staging_path(self):
        snapshot, auth = self.config_snapshot()
        real_unlink = os.unlink

        def refuse(path, *args, **kwargs):
            if str(path).startswith('.model-proxy-config-'):
                raise OSError('fake cleanup failure')
            return real_unlink(path, *args, **kwargs)

        with patch('core.mac_config_writer.os.replace', side_effect=OSError('fake')), \
             patch('core.mac_config_writer.os.unlink', side_effect=refuse):
            with self.assertRaises(MacWriteError) as caught:
                self.config_write(snapshot, auth, self.locations())
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertTrue(caught.exception.cleanup_unknown)
        staging = Path(caught.exception.staging_name)
        self.assertEqual(staging.parent, self.stage)
        self.assertTrue(staging.exists())
        staging.unlink()
        self.assertEqual(self.target.read_bytes(), snapshot.data)

    def test_postcheck_failure_is_not_success(self):
        snapshot, auth = self.config_snapshot()
        with patch('core.mac_config_writer.capture_config', side_effect=MacConfigError('fake')):
            with self.assertRaises(MacWriteError) as caught:
                self.config_write(snapshot, auth, self.locations())
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assert_no_stage()

    def test_secure_target_backup_and_same_volume(self):
        snapshot = self.target_snapshot()
        result = secure_replace_runtime(snapshot, b'fake new', max_bytes=1024,
                                        locations=self.locations())
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(Path(result.backup).read_bytes(), snapshot.contents)
        self.assertEqual(self.target.read_bytes(), b'fake new')
        self.assert_no_stage()

    def test_secure_missing_target_no_backup_no_overwrite(self):
        self.target.unlink()
        snapshot = self.target_snapshot()
        result = secure_replace_runtime(snapshot, b'fake new', max_bytes=1024,
                                        locations=self.locations(backup=False), make_backup=False)
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), b'fake new')
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assert_no_stage()

    def test_secure_missing_target_appearing_at_link_is_not_overwritten(self):
        self.target.unlink()
        snapshot = self.target_snapshot()
        real_link = os.link

        def race(src, dst, **kwargs):
            self.target.write_bytes(b'fake concurrently created')
            self.target.chmod(0o600)
            return real_link(src, dst, **kwargs)

        with patch('core.secure_files.os.link', side_effect=race):
            with self.assertRaises(SecureWriteError) as caught:
                secure_replace_runtime(snapshot, b'fake new', max_bytes=1024,
                                       locations=self.locations(backup=False), make_backup=False)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertEqual(self.target.read_bytes(), b'fake concurrently created')
        self.assert_no_stage()

    def test_secure_rejects_insecure_staging_or_unauthorized_backup(self):
        snapshot = self.target_snapshot()
        loc = self.locations()
        self.stage.chmod(0o755)
        with self.assertRaises(SecureWriteError) as caught:
            secure_replace_runtime(snapshot, b'fake new', max_bytes=1024, locations=loc)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.stage.chmod(0o700)
        no_backup = self.locations(backup=False)
        with self.assertRaises(SecureWriteError):
            secure_replace_runtime(snapshot, b'fake new', max_bytes=1024, locations=no_backup)
        self.assertEqual(self.target.read_bytes(), snapshot.contents)
        self.assert_no_stage()

    def test_secure_cross_volume_and_unknown(self):
        snapshot = self.target_snapshot()
        with patch('core.secure_files._same_volume', return_value=False):
            with self.assertRaises(SecureWriteError) as caught:
                secure_replace_runtime(snapshot, b'fake new', max_bytes=1024,
                                       locations=self.locations())
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            real_replace = os.replace

            def unknown(*args, **kwargs):
                self.assertEqual(os.fstat(kwargs['src_dir_fd']).st_ino,
                                 os.fstat(kwargs['dst_dir_fd']).st_ino)
                real_replace(*args, **kwargs)
                raise OSError('fake')

            with patch('core.secure_files.os.replace', side_effect=unknown):
                with self.assertRaises(SecureWriteError) as caught:
                    secure_replace_runtime(snapshot, b'fake new', max_bytes=1024,
                                           locations=self.locations(target_staging=True))
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertEqual(self.target.read_bytes(), b'fake new')
        self.assertEqual(Path(caught.exception.backup).read_bytes(), snapshot.contents)
        candidate = caught.exception.candidate
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate.matches(snapshot))  # B
        verified_c = self.target_snapshot()
        self.assertEqual(candidate.comparable_identity, verified_c.comparable_identity)
        self.assertTrue(candidate.matches(verified_c))  # verified C
        self.target.write_bytes(b'fake unrelated third state')
        self.assertFalse(candidate.matches(self.target_snapshot()))
        self.assert_no_stage()

    def test_secure_unknown_before_replace_retains_B_and_C_evidence(self):
        snapshot = self.target_snapshot()
        with patch('core.secure_files.os.replace', side_effect=OSError('fake')):
            with self.assertRaises(SecureWriteError) as caught:
                secure_replace_runtime(snapshot, b'fake candidate', max_bytes=1024,
                                       locations=self.locations(backup=False), make_backup=False)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertIsNotNone(caught.exception.candidate)
        self.assertEqual(self.target_snapshot(), snapshot)
        self.assertFalse(caught.exception.candidate.matches(self.target_snapshot()))
        self.assert_no_stage()

    def test_secure_replace_and_cleanup_unknown_reports_owned_candidate(self):
        snapshot = self.target_snapshot()
        actual_unlink = os.unlink

        def fail_stage_unlink(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith('.model-proxy-runtime-'):
                raise OSError('fake cleanup failure')
            return actual_unlink(path, *args, **kwargs)

        with patch('core.secure_files.os.replace', side_effect=OSError('fake replace failure')):
            with patch('core.secure_files.os.unlink', side_effect=fail_stage_unlink):
                with self.assertRaises(SecureWriteError) as caught:
                    secure_replace_runtime(snapshot, b'fake candidate', max_bytes=1024,
                                           locations=self.locations(backup=False), make_backup=False)
        error = caught.exception
        self.assertEqual(error.state, 'UNKNOWN')
        self.assertTrue(error.cleanup_unknown)
        self.assertIsNotNone(error.candidate)
        self.assertEqual(self.target_snapshot(), snapshot)
        self.assertEqual(Path(error.staging).parent, self.stage)
        self.assertEqual(Path(error.staging).read_bytes(), b'fake candidate')
        Path(error.staging).unlink()
        self.assert_no_stage()

    def test_postcommit_guard_failure_returns_comparable_candidate(self):
        snapshot = self.target_snapshot()
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError('fake path evidence failure')

        with self.assertRaises(SecureWriteError) as caught:
            secure_replace_runtime(snapshot, b'fake candidate', max_bytes=1024,
                                   locations=self.locations(backup=False), make_backup=False,
                                   boundary_guard=guard)
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertTrue(caught.exception.candidate.matches(self.target_snapshot()))
        self.assert_no_stage()

    def test_sidecar_original_parent_link_can_be_guarded(self):
        directory_link = self.root / 'original-config-parent'
        directory_link.symlink_to('config')
        config = capture_config(str(directory_link / 'config.json'))
        sidecar = self.config / 'session_overrides.json'
        sidecar.write_bytes(b'fake B')
        sidecar.chmod(0o600)
        source_parent = str(directory_link)
        resolved_parent = os.path.realpath(source_parent)

        def guard():
            verify_config(config)
            if os.path.realpath(source_parent) != resolved_parent:
                raise RuntimeError('link changed')

        guard()
        snapshot = capture_target(str(sidecar), authorized_root=resolved_parent,
                                  protection_reader=mac_private_protection, max_bytes=1024)
        guard()
        result = secure_replace_runtime(snapshot, b'fake C', max_bytes=1024,
                                        locations=self.locations(backup=False), make_backup=False,
                                        boundary_guard=guard)
        self.assertEqual(result.snapshot.contents, b'fake C')
        self.assertTrue(result.candidate.matches(result.snapshot))
        self.assertTrue(directory_link.is_symlink())
        self.assert_no_stage()

    def test_sidecar_parent_link_swap_during_commit_is_rejected(self):
        directory_link = self.root / 'original-config-parent'
        directory_link.symlink_to('config')
        config = capture_config(str(directory_link / 'config.json'))
        sidecar = self.config / 'session_overrides.json'
        sidecar.write_bytes(b'fake B')
        sidecar.chmod(0o600)
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        (other / 'config.json').write_bytes(config.data)
        (other / 'config.json').chmod(0o600)
        snapshot = capture_target(str(sidecar), authorized_root=str(self.config),
                                  protection_reader=mac_private_protection, max_bytes=1024)
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 3:
                directory_link.unlink()
                directory_link.symlink_to('other')
            verify_config(config)

        with self.assertRaises(SecureWriteError) as caught:
            secure_replace_runtime(snapshot, b'fake C', max_bytes=1024,
                                   locations=self.locations(backup=False), make_backup=False,
                                   boundary_guard=guard)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertIsNone(caught.exception.candidate)
        self.assertEqual(sidecar.read_bytes(), b'fake B')
        self.assert_no_stage()
