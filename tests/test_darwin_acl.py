"""Darwin ACL checks; run only in a copied project with a fake HOME."""
import contextlib
import ctypes
import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (ROOT != SANDBOX / 'project' or not (SANDBOX / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).is_relative_to(SANDBOX)):
    raise RuntimeError('isolated copied project and fake HOME required')

from core import darwin_acl as acl
from core import discovery, secure_files, platform_lock


class FakeACL:
    """Pure ABI fake for errors that cannot safely be put on a filesystem."""
    def __init__(self, tags=(2,), *, valid=0, size=5, short_copy=False,
                 first_errno=None, next_errno=errno.EINVAL, tag_error=False,
                 permission_mask=16, permission_error=False):
        self.tags = list(tags)
        self.valid = valid
        self.size = size
        self.short_copy = short_copy
        self.first_errno = first_errno
        self.next_errno = next_errno
        self.tag_error = tag_error
        self.permission_mask = permission_mask
        self.permission_error = permission_error
        self.index = 0
        self.entry_queries = []
        self.free_count = 0

    def acl_dup(self, obj): return 100
    def acl_valid(self, obj): return self.valid
    def acl_free(self, obj): self.free_count += 1; return 0
    def acl_size(self, obj): return self.size
    def acl_copy_ext(self, buf, obj, size):
        if size > 0:
            ctypes.memmove(buf, b'x' * size, size)
        return size - 1 if self.short_copy else size
    def acl_get_entry(self, obj, which, out):
        self.entry_queries.append(which)
        if which == acl.ACL_FIRST_ENTRY:
            self.index = 0
            if self.first_errno is not None:
                ctypes.set_errno(self.first_errno)
                return -1
        elif which >= 0:
            self.index = which
        elif which != acl.ACL_NEXT_ENTRY:
            raise AssertionError('unexpected entry id')
        if self.index >= len(self.tags):
            ctypes.set_errno(self.next_errno)
            return -1
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = self.index + 1
        self.index += 1
        return 0
    def acl_get_tag_type(self, entry, out):
        if self.tag_error:
            return -1
        index = entry.value - 1
        ctypes.cast(out, ctypes.POINTER(ctypes.c_int))[0] = self.tags[index]
        return 0
    def acl_get_permset_mask_np(self, entry, out):
        if self.permission_error:
            return -1
        ctypes.cast(out, ctypes.POINTER(ctypes.c_uint64))[0] = self.permission_mask
        return 0


class ACLParserTests(unittest.TestCase):
    def test_deny_only_and_exact_128_entry_limit(self):
        for count in (1, 2, 128):
            with self.subTest(count=count):
                fake = FakeACL([2] * count)
                self.assertEqual(acl._checked_deny_acl(fake, 1), b'xxxxx')
                self.assertEqual(fake.free_count, 1)

    def test_allow_unknown_mixed_invalid_and_overflow_fail_closed(self):
        cases = [FakeACL([1]), FakeACL([2, 1]), FakeACL([0]), FakeACL([3]),
                 FakeACL([2] * 129), FakeACL([2], valid=-1), FakeACL([2], size=-1),
                 FakeACL([2], size=65537), FakeACL([2], short_copy=True),
                 FakeACL([2], first_errno=errno.EINVAL), FakeACL([2], next_errno=errno.EACCES),
                 FakeACL([2], tag_error=True), FakeACL([2], permission_mask=1 << 32),
                 FakeACL([2], permission_error=True)]
        for fake in cases:
            with self.subTest(tags=fake.tags[:3], size=fake.size, valid=fake.valid):
                with self.assertRaises(acl.ACLVerificationError):
                    acl._checked_deny_acl(fake, 1)

    def test_early_next_einval_with_later_allow_is_rejected_by_index(self):
        class EarlyEnd(FakeACL):
            def acl_get_entry(self, obj, which, out):
                if which == acl.ACL_NEXT_ENTRY and self.index == 1:
                    self.entry_queries.append(which)
                    ctypes.set_errno(errno.EINVAL)
                    return -1
                return super().acl_get_entry(obj, which, out)
        fake = EarlyEnd([acl.ACL_EXTENDED_DENY, 1])  # second entry is ALLOW
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl._checked_deny_acl(fake, 1)
        self.assertEqual(caught.exception.code, 'acl_index_inconsistent')
        self.assertEqual(fake.entry_queries, [acl.ACL_FIRST_ENTRY, acl.ACL_NEXT_ENTRY, 1])

    def test_index_error_other_than_documented_end_is_rejected(self):
        class BadIndex(FakeACL):
            def acl_get_entry(self, obj, which, out):
                if which == 1:
                    ctypes.set_errno(errno.EACCES)
                    return -1
                return super().acl_get_entry(obj, which, out)
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl._checked_deny_acl(BadIndex([2]), 1)
        self.assertEqual(caught.exception.code, 'acl_index_inconsistent')

    def test_acl_copy_change_and_free_failure_fail_closed(self):
        fake = FakeACL([2])
        calls = 0
        def changing_copy(buf, obj, size):
            nonlocal calls
            calls += 1
            ctypes.memmove(buf, (b'x' if calls == 1 else b'y') * size, size)
            return size
        fake.acl_copy_ext = changing_copy
        with self.assertRaises(acl.ACLVerificationError):
            acl._checked_deny_acl(fake, 1)
        fake = FakeACL([2])
        fake.acl_free = lambda obj: -1
        with self.assertRaises(acl.ACLVerificationError):
            acl._checked_deny_acl(fake, 1)

    def test_get_fd_errors_and_file_acl_are_fail_closed(self):
        class Getter:
            def __init__(self, err, result): self.err, self.result, self.freed = err, result, 0
            def acl_get_fd_np(self, fd, kind): ctypes.set_errno(self.err); return self.result
            def acl_free(self, obj): self.freed += 1; return 0
        self.assertIsNone(acl._read_once(123, Getter(errno.ENOENT, 0), directory=True))
        for err in (errno.EACCES, errno.EINVAL, errno.EIO):
            with self.subTest(err=err), self.assertRaises(acl.ACLVerificationError):
                acl._read_once(123, Getter(err, 0), directory=True)
        present = Getter(0, 1234)
        with self.assertRaises(acl.ACLVerificationError) as caught:
            acl._read_once(123, present, directory=False)
        self.assertEqual(caught.exception.code, 'file_acl_present')
        self.assertEqual(present.freed, 1)

    def test_enumeration_positive_success_code_is_not_accepted(self):
        fake = FakeACL([2])
        fake.acl_get_entry = lambda *args: 1
        with self.assertRaises(acl.ACLVerificationError):
            acl._checked_deny_acl(fake, 1)

    def test_record_read_compares_acl_before_and_after_record_access(self):
        state = SANDBOX / 'cases' / 'fake-state'
        calls = 0
        def verify(path, *, _capture):
            nonlocal calls
            calls += 1
            _capture.append((str(path), (1, 2, 0o700, 1, 1, 5), b'A' if calls == 1 else b'B'))
            return (1, 2)
        with mock.patch.object(discovery, 'verify_record_directory', side_effect=verify), \
             mock.patch.object(discovery, '_check_record_target', return_value=None):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.read_instance_record(state, 'active.json')
        self.assertEqual(caught.exception.code, 'protection_changed')

    def test_record_write_compares_full_directory_protection_including_flags(self):
        state = SANDBOX / 'cases' / '.model_proxy_runtime'
        parent = str(state.parent)
        name = str(state)
        before = [(parent, (1, 2, 0o700, 3, 4, 0), b'parent-deny'),
                  (name, (1, 3, 0o700, 3, 4, 0), b'state-deny')]
        self.assertTrue(discovery._same_record_directory_protection(before, list(before), state))
        for changed in (
            [(parent, (1, 2, 0o700, 3, 4, 0), b'changed'), before[1]],
            [before[0], (name, (1, 3, 0o700, 3, 4, 0), b'changed')],
            [before[0], (name, (1, 3, 0o755, 3, 4, 0), b'state-deny')],
            [(parent, (1, 2, 0o700, 3, 4, 1), b'parent-deny'), before[1]],
            [before[0], (name, (1, 3, 0o700, 3, 4, 1), b'state-deny')],
        ):
            with self.subTest(changed=changed):
                self.assertFalse(discovery._same_record_directory_protection(before, changed, state))
        for row in range(2):
            for field in range(6):
                changed = list(before)
                path, marker, evidence = changed[row]
                replacement = list(marker)
                replacement[field] += 1
                changed[row] = (path, tuple(replacement), evidence)
                with self.subTest(row=row, field=field):
                    self.assertFalse(discovery._same_record_directory_protection(before, changed, state))

    def test_two_fd_snapshots_must_match_even_if_stat_does_not_change(self):
        with tempfile.TemporaryDirectory(dir=SANDBOX / 'cases') as directory:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(acl, '_library', return_value=object()), \
                     mock.patch.object(acl, '_read_once', side_effect=[b'A', b'B']) as read:
                    with self.assertRaises(acl.ACLVerificationError) as caught:
                        acl.read_acl_snapshot(fd)
                self.assertEqual(caught.exception.code, 'acl_changed')
                self.assertEqual([call.args[0] for call in read.call_args_list], [fd, fd])
            finally:
                os.close(fd)


@unittest.skipUnless(sys.platform == 'darwin', 'real Darwin ACLs required')
class RealACLTests(unittest.TestCase):
    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix='acl-', dir=SANDBOX / 'cases')).resolve()
        self.targets = []

    def tearDown(self):
        # Remove ACL only from paths created and recorded by this test.
        for path in reversed(self.targets):
            if path.exists() and not path.is_symlink() and path.is_relative_to(self.case):
                subprocess.run(['/bin/chmod', '-N', str(path)], capture_output=True, check=False)
        shutil.rmtree(self.case)

    def target(self, name, *, directory=True):
        path = self.case / name
        if directory:
            path.mkdir(mode=0o700)
        else:
            path.write_text('FAKE', encoding='utf-8')
            path.chmod(0o600)
        self.targets.append(path)
        return path

    def add(self, path, rule):
        result = subprocess.run(['/bin/chmod', '+a', rule, str(path)], capture_output=True, text=True)
        if result.returncode:
            self.fail('cannot create test ACL: ' + result.stderr)

    def fd(self, path):
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)

    def test_no_acl_directory_and_file(self):
        for path in (self.target('directory'), self.target('file', directory=False)):
            fd = self.fd(path)
            try:
                self.assertIsNone(acl.read_acl_snapshot(fd))
                self.assertIsNone(discovery.verify_no_extended_acl(fd))
            finally:
                os.close(fd)

    def test_real_darwin_explicit_index_cross_checks_end(self):
        path = self.target('index')
        self.add(path, 'group:everyone deny delete')
        fd = self.fd(path)
        lib = acl._library()
        pointer = None
        try:
            pointer = lib.acl_get_fd_np(fd, acl.ACL_TYPE_EXTENDED)
            self.assertTrue(pointer)
            entry = ctypes.c_void_p()
            self.assertEqual(lib.acl_get_entry(pointer, acl.ACL_FIRST_ENTRY, ctypes.byref(entry)), 0)
            self.assertTrue(entry.value)
            ctypes.set_errno(0)
            self.assertEqual(lib.acl_get_entry(pointer, acl.ACL_NEXT_ENTRY, ctypes.byref(entry)), -1)
            self.assertEqual(ctypes.get_errno(), errno.EINVAL)
            ctypes.set_errno(0)
            self.assertEqual(lib.acl_get_entry(pointer, 1, ctypes.byref(entry)), -1)
            self.assertEqual(ctypes.get_errno(), errno.EINVAL)
            self.assertTrue(acl._checked_deny_acl(lib, pointer))
        finally:
            if pointer:
                lib.acl_free(pointer)
            os.close(fd)

    def test_real_deny_only_directory_accepted_and_serialized(self):
        path = self.target('deny')
        self.add(path, 'group:everyone deny delete')
        fd = self.fd(path)
        try:
            first = acl.read_acl_snapshot(fd)
            self.assertIsInstance(first, bytes)
            self.assertTrue(first)
            self.assertEqual(first, discovery.verify_no_extended_acl(fd))
        finally:
            os.close(fd)
        proof = secure_files.mac_private_protection(str(path), path.lstat())
        self.assertEqual(proof.token[3], first)
        self.assertEqual(discovery._verify_directory(path, private=True).st_ino, path.stat().st_ino)

    def test_real_allow_and_mixed_directory_acl_rejected(self):
        allow = self.target('allow')
        self.add(allow, 'group:everyone allow list')
        mixed = self.target('mixed')
        self.add(mixed, 'group:everyone deny delete')
        self.add(mixed, 'group:everyone allow list')
        for path in (allow, mixed):
            with self.subTest(path=path.name):
                fd = self.fd(path)
                try:
                    with self.assertRaises(acl.ACLVerificationError): acl.read_acl_snapshot(fd)
                    with self.assertRaises(discovery.PreflightError): discovery.verify_no_extended_acl(fd)
                finally:
                    os.close(fd)
                with self.assertRaises(secure_files.SecureSnapshotError):
                    secure_files.mac_private_protection(str(path), path.lstat())
                with self.assertRaises(discovery.PreflightError):
                    discovery._verify_directory(path, private=True)

    def test_lock_shared_verifier_accepts_deny_directory_not_deny_file(self):
        directory = self.target('lock-directory')
        self.add(directory, 'group:everyone deny delete')
        self.assertTrue(platform_lock.verify_local_access(directory, 'directory'))
        path = self.target('lock-file', directory=False)
        self.add(path, 'group:everyone deny delete')
        self.assertFalse(platform_lock.verify_local_access(path, 'file'))

    def test_record_directory_checks_fake_deny_ancestors_twice(self):
        parent = self.target('parent-chain')
        self.add(parent, 'group:everyone deny delete')
        project = parent / 'project'
        project.mkdir(mode=0o700)
        self.targets.append(project)
        self.add(project, 'group:everyone deny delete')
        state = project / '.model_proxy_runtime'
        state.mkdir(mode=0o700)
        original = discovery._verify_directory
        synthetic = self.case.stat()
        def verifier(path, *, private, _capture=None):
            path = Path(path)
            if path == self.case or path.is_relative_to(self.case):
                return original(path, private=private, _capture=_capture)
            # No actual ACL access outside the test-owned directory.
            if _capture is not None:
                _capture.append((str(path), (synthetic.st_dev, synthetic.st_ino), None))
            return synthetic
        with mock.patch.object(discovery, '_verify_directory', side_effect=verifier):
            self.assertEqual(discovery.verify_record_directory(state),
                             (project.stat().st_dev, project.stat().st_ino))

    def test_fake_installation_locks_and_records_with_deny_ancestors(self):
        parent = self.target('lock-chain')
        self.add(parent, 'group:everyone deny delete')
        project = parent / 'project'
        project.mkdir(mode=0o700)
        self.targets.append(project)
        self.add(project, 'group:everyone deny delete')
        state = project / '.model_proxy_runtime'
        state.mkdir(mode=0o700)
        runtime = project / 'runtime'
        runtime.mkdir(mode=0o700)
        paths = {name: runtime / name for name in
                 ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')}
        original = discovery._verify_directory
        synthetic = self.case.stat()
        def verifier(path, *, private, _capture=None):
            path = Path(path)
            if path == self.case or path.is_relative_to(self.case):
                return original(path, private=private, _capture=_capture)
            if _capture is not None:
                _capture.append((str(path), (synthetic.st_dev, synthetic.st_ino), None))
            return synthetic
        with mock.patch.object(discovery, '_verify_directory', side_effect=verifier):
            snapshot = discovery.inspect_runtime_identities(project, paths)
            with platform_lock.acquire_lock(snapshot, 'anchor.lock', create=True,
                                            access_verifier=platform_lock.verify_local_access) as anchor:
                with platform_lock.acquire_lock(anchor.snapshot, 'lock', create=True,
                                                anchor=anchor, access_verifier=platform_lock.verify_local_access) as legacy:
                    dev, inode = discovery.verify_record_directory(state)
                    record = {'schema': discovery.INSTANCE_SCHEMA, 'record': 'install',
                              'install_id': 'a' * 32, 'project_root': str(project),
                              'root_identity': {'device': dev, 'inode': inode},
                              'initialization_source': 'fake fixture', 'ever_started': False,
                              'last_instance_nonce': None}
                    discovery.write_instance_record(state, 'install.json', record, anchor=anchor, legacy=legacy)
                    self.assertEqual(discovery.read_instance_record(state, 'install.json'), record)

    def test_real_deny_only_file_remains_rejected(self):
        path = self.target('file', directory=False)
        self.add(path, 'group:everyone deny delete')
        fd = self.fd(path)
        try:
            with self.assertRaises(acl.ACLVerificationError): acl.read_acl_snapshot(fd)
            with self.assertRaises(discovery.PreflightError): discovery.verify_no_extended_acl(fd)
        finally:
            os.close(fd)
        with self.assertRaises(secure_files.SecureSnapshotError):
            secure_files.mac_private_protection(str(path), path.lstat())
        self.assertEqual(path.read_text(), 'FAKE')

    def test_changed_deny_acl_is_part_of_target_snapshot(self):
        parent = self.target('parent')
        target = parent / 'settings.json'
        target.write_text('FAKE', encoding='utf-8')
        target.chmod(0o600)
        self.add(parent, 'group:everyone deny delete')
        # Do not inspect any real ancestor ACL: only fake paths under self.case
        # use the native reader. Outside ancestors are synthetic unit evidence.
        def reader(path, info):
            p = Path(path)
            if p == self.case or p.is_relative_to(self.case):
                return secure_files.mac_private_protection(path, info)
            return secure_files.ProtectionEvidence(True, ('synthetic-ancestor', info.st_dev, info.st_ino))
        before = secure_files.capture_target(target, authorized_root=self.case,
                                             protection_reader=reader, max_bytes=1024)
        parent_proof = dict((p, evidence.token) for p, _, evidence in before.parents)[str(parent)]
        result = subprocess.run(['/bin/chmod', '-N', str(parent)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.add(parent, 'group:everyone deny writeattr')
        after = secure_files.capture_target(target, authorized_root=self.case,
                                            protection_reader=reader, max_bytes=1024)
        newer_proof = dict((p, evidence.token) for p, _, evidence in after.parents)[str(parent)]
        self.assertNotEqual(parent_proof[3], newer_proof[3])
        self.assertFalse(secure_files.target_unchanged(before, protection_reader=reader, max_bytes=1024))

    def test_secure_write_succeeds_with_fake_deny_parent(self):
        parent = self.target('client-dir')
        target = parent / 'settings.json'
        target.write_text('FAKE_OLD', encoding='utf-8')
        target.chmod(0o600)
        self.add(parent, 'group:everyone deny delete')
        original = secure_files.mac_private_protection
        def reader(path, info):
            p = Path(path)
            if p == self.case or p.is_relative_to(self.case):
                return original(path, info)
            return secure_files.ProtectionEvidence(True, ('synthetic-ancestor', info.st_dev, info.st_ino))
        with mock.patch.object(secure_files, 'mac_private_protection', side_effect=reader):
            snap = secure_files.capture_target(target, authorized_root=self.case,
                                               protection_reader=reader, max_bytes=1024)
            outcome = secure_files.secure_replace(snap, b'FAKE_NEW', max_bytes=1024)
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(target.read_text(), 'FAKE_NEW')
        self.assertEqual(Path(outcome.backup).read_text(), 'FAKE_OLD')
        self.assertEqual(Path(outcome.backup).stat().st_mode & 0o777, 0o600)

    def test_secure_write_refuses_changed_deny_acl_before_backup(self):
        parent = self.target('client-changed')
        target = parent / 'settings.json'
        target.write_text('FAKE_OLD', encoding='utf-8')
        target.chmod(0o600)
        self.add(parent, 'group:everyone deny delete')
        original = secure_files.mac_private_protection
        def reader(path, info):
            p = Path(path)
            if p == self.case or p.is_relative_to(self.case):
                return original(path, info)
            return secure_files.ProtectionEvidence(True, ('synthetic-ancestor', info.st_dev, info.st_ino))
        snap = secure_files.capture_target(target, authorized_root=self.case,
                                           protection_reader=reader, max_bytes=1024)
        self.assertEqual(subprocess.run(['/bin/chmod', '-N', str(parent)], capture_output=True).returncode, 0)
        self.add(parent, 'group:everyone deny writeattr')
        with mock.patch.object(secure_files, 'mac_private_protection', side_effect=reader):
            with self.assertRaises(secure_files.SecureWriteError) as caught:
                secure_files.secure_replace(snap, b'FAKE_NEW', max_bytes=1024)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(target.read_text(), 'FAKE_OLD')
        self.assertEqual(list(parent.glob('*.bak.*')), [])

    def test_discovery_detects_change_between_protection_observations(self):
        path = self.target('directory')
        with mock.patch.object(discovery, 'verify_no_extended_acl', side_effect=[b'A', b'B']):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._verify_directory(path, private=True)
        self.assertEqual(caught.exception.code, 'protection_changed')

    def test_record_chain_compares_full_acl_snapshots_between_passes(self):
        project = self.target('project')
        state = project / '.model_proxy_runtime'
        state.mkdir(mode=0o700)
        info = project.stat()
        count = 0
        total = len(project.parents) + 2
        def verifier(path, *, private, _capture):
            nonlocal count
            count += 1
            evidence = b'changed' if count == total + 1 else b'original'
            _capture.append((str(path), (info.st_dev, info.st_ino), evidence))
            return info
        with mock.patch.object(discovery, '_verify_directory', side_effect=verifier):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.verify_record_directory(state)
        self.assertEqual(caught.exception.code, 'protection_changed')

if __name__ == '__main__':
    unittest.main()
