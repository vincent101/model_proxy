"""Finite writer sequencing tests; fake backend is not Windows ACL evidence."""
import unittest
from types import SimpleNamespace

from core.windows_secure_files import SecureWriteError, _finite_write


def observed(data, identity):
    return SimpleNamespace(data=data, identity=identity, protection=(b'private',))


class Backend:
    def __init__(self, exists=True, fail=None):
        self.exists = exists
        self.fail = fail
        self.events = []
        self.files = {}

    def step(self, name):
        self.events.append(name)
        if self.fail == name:
            raise RuntimeError(name)

    def reserve(self, role):
        self.step('reserve-' + role)
        return role

    def revalidate(self): self.step('revalidate')
    def create_and_write(self, path, data):
        self.step('create-' + path)
        value = observed(data, path)
        self.files[path] = value
        return value
    def inspect(self, path):
        self.step('inspect-' + path)
        return self.files[path]
    def release_precommit(self): self.step('release')
    def commit(self, stage): self.step('commit')
    def postcheck(self):
        self.step('postcheck')
        return self.files['stage']
    def finish(self): self.step('finish')


class FiniteTests(unittest.TestCase):
    def run_write(self, backend, exists=True, preflight=lambda: True):
        return _finite_write(SimpleNamespace(exists=exists, contents=b'old' if exists else None),
                             b'new', backend, preflight)

    def test_existing_and_new(self):
        for exists in (True, False):
            with self.subTest(exists=exists):
                backend = Backend(exists)
                out = self.run_write(backend, exists)
                self.assertEqual(out.state, 'VERIFIED')
                self.assertEqual(out.backup_status, 'VERIFIED' if exists else 'NONE')
                self.assertEqual(backend.events[-5:] if exists else backend.events[-4:],
                                 ['release', 'commit', 'postcheck', 'inspect-backup', 'finish'] if exists
                                 else ['release', 'commit', 'postcheck', 'finish'])
                self.assertEqual('create-backup' in backend.events, exists)

    def test_failures_report_phase_and_do_not_retry(self):
        for fail, state, backup, unknown in (
                ('create-backup', 'NOT_REPLACED', 'UNVERIFIED', True),
                ('create-stage', 'NOT_REPLACED', 'VERIFIED', True),
                ('commit', 'UNKNOWN', 'VERIFIED', False),
                ('postcheck', 'POSTCHECK_FAILED', 'VERIFIED', False),
                # An exception in finish means resource release is unknown,
                # even though the target postcheck already completed.
                ('finish', 'UNKNOWN', 'UNKNOWN', False)):
            with self.subTest(fail=fail):
                backend = Backend(fail=fail)
                with self.assertRaises(SecureWriteError) as caught:
                    self.run_write(backend)
                error = caught.exception
                self.assertEqual((error.target_state, error.backup_status, error.creation_unknown),
                                 (state, backup, unknown))
                self.assertEqual(error.state, state)
                self.assertNotEqual(error.state, 'VERIFIED')
                self.assertNotEqual(error.target_state, 'VERIFIED')
                self.assertEqual(error.target_was_verified, fail == 'finish')
                self.assertEqual(error.backup_was_verified, fail != 'create-backup')
                self.assertEqual(error.backup_verified, backup == 'VERIFIED')
                self.assertEqual(backend.events.count('commit'), int(fail in ('commit', 'postcheck', 'finish')))
                self.assertEqual(error.release_unknown, fail == 'finish')
                if fail == 'finish':
                    self.assertEqual(error.code, 'release_unknown')
                    self.assertEqual(backend.events[-3:], ['postcheck', 'inspect-backup', 'finish'])

    def test_backup_changed_after_target_observation_is_not_success(self):
        backend = Backend()
        def commit(stage):
            backend.step('commit')
            backend.files['backup'] = observed(b'changed', 'backup')
        backend.commit = commit
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(backend)
        error = caught.exception
        self.assertEqual((error.state, error.target_state), ('UNKNOWN', 'UNKNOWN'))
        self.assertNotEqual(error.target_state, 'VERIFIED')
        self.assertTrue(error.target_was_verified)
        self.assertEqual(error.code, 'backup_changed')
        self.assertEqual(error.backup_status, 'CHANGED')
        self.assertTrue(error.backup_was_verified)
        self.assertFalse(error.backup_verified)
        self.assertFalse(error.release_unknown)
        self.assertEqual(backend.events[-3:], ['postcheck', 'inspect-backup', 'finish'])

    def test_backup_unknown_after_target_observation_is_not_success(self):
        backend = Backend()
        inspect = backend.inspect
        def checked(path):
            if path == 'backup' and 'commit' in backend.events:
                raise RuntimeError('unknown')
            return inspect(path)
        backend.inspect = checked
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(backend)
        error = caught.exception
        self.assertEqual((error.state, error.target_state), ('UNKNOWN', 'UNKNOWN'))
        self.assertNotEqual(error.target_state, 'VERIFIED')
        self.assertTrue(error.target_was_verified)
        self.assertEqual(error.code, 'backup_unverified')
        self.assertEqual(error.backup_status, 'UNKNOWN')
        self.assertTrue(error.backup_was_verified)
        self.assertFalse(error.backup_verified)
        self.assertFalse(error.release_unknown)
        self.assertEqual(backend.events[-2:], ['postcheck', 'finish'])

    def test_preflight_rechecked_before_commit(self):
        checks = iter((True, False))
        backend = Backend()
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(backend, preflight=lambda: next(checks))
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertNotIn('commit', backend.events)

    def test_changed_backup_aborts_before_commit(self):
        backend = Backend()
        real_inspect = backend.inspect
        def inspect(path):
            value = real_inspect(path)
            return observed(b'different', path) if path == 'backup' else value
        backend.inspect = inspect
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(backend)
        self.assertEqual(caught.exception.backup_status, 'CHANGED')
        self.assertNotIn('commit', backend.events)

    def test_observed_backup_change_is_not_laundered_by_a_later_read(self):
        backend = Backend()
        inspect = backend.inspect
        backup_reads = []
        def changed_then_restored(path):
            if path == 'backup':
                backup_reads.append(path)
                if len(backup_reads) == 1:
                    return observed(b'changed', 'backup')
            return inspect(path)
        backend.inspect = changed_then_restored
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(backend)
        self.assertTrue(caught.exception.backup_was_verified)
        self.assertEqual(caught.exception.backup_status, 'CHANGED')
        self.assertEqual(backup_reads, ['backup'])
        self.assertNotIn('commit', backend.events)

    def test_unknown_backup_recheck_is_not_retried_as_verified(self):
        backend = Backend()
        inspect = backend.inspect
        backup_reads = []
        def unavailable_then_restored(path):
            if path == 'backup':
                backup_reads.append(path)
                if len(backup_reads) == 1:
                    raise OSError('fake temporary read failure')
            return inspect(path)
        backend.inspect = unavailable_then_restored
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(backend)
        self.assertTrue(caught.exception.backup_was_verified)
        self.assertEqual(caught.exception.backup_status, 'UNKNOWN')
        self.assertEqual(backup_reads, ['backup'])
        self.assertNotIn('commit', backend.events)

    def test_postcheck_detects_each_kind_of_drift(self):
        for field, value in (('data', b'other'), ('identity', 'other'),
                             ('protection', (b'public',))):
            with self.subTest(field=field):
                backend = Backend()
                def postcheck():
                    backend.step('postcheck')
                    result = observed(b'new', 'stage')
                    setattr(result, field, value)
                    return result
                backend.postcheck = postcheck
                with self.assertRaises(SecureWriteError) as caught:
                    self.run_write(backend)
                self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
                self.assertEqual(backend.events.count('commit'), 1)

    def test_unknown_commit_does_not_freeze_later_independent_call(self):
        failing = Backend(fail='commit')
        with self.assertRaises(SecureWriteError) as caught:
            self.run_write(failing)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        fresh = Backend()
        self.assertEqual(self.run_write(fresh).state, 'VERIFIED')

    def test_product_result_reports_backup_without_exposing_path(self):
        from core.windows_secure_files import WriteOutcome
        result = WriteOutcome('VERIFIED', 'fake-private-backup', True, 'VERIFIED')
        self.assertTrue(result.backup_verified)
        self.assertTrue(result.backup_was_verified)
        self.assertNotIn('fake-private-backup', repr(result))


if __name__ == '__main__':
    unittest.main()
