"""Isolated fake-config tests. No network, production config or services."""
import contextlib
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.mac_config_path import capture_config, open_target_parent
from core.mac_config_writer import WriteAuthorization, MacWriteError, replace_config


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['MP_TEST_ROOT'])
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'config'
        self.folder.mkdir(mode=0o700)
        self.target = self.folder / 'business.json'
        self.target.write_bytes(b'{"old":true}\n')
        self.target.chmod(0o600)
        self.alias = self.root / 'alias'
        self.alias.symlink_to('config/business.json')

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, path=None):
        snap = capture_config(str(path or self.alias))
        with open_target_parent(snap) as (fd, _):
            p = os.fstat(fd)
            auth = WriteAuthorization(snap.target_path, (p.st_dev, p.st_ino),
                                      tuple(snap.final_identity[:2]), True)
        return snap, auth

    def write(self, snap, auth, **kwargs):
        return replace_config(snap, b'{"new":true}\n', auth,
                              preflight=kwargs.get('preflight', lambda _: True),
                              before_replace=kwargs.get('before_replace', lambda: None))

    def test_read_only_source_with_xattrs_is_replaced_and_backed_up(self):
        subprocess.run(['/usr/bin/xattr', '-w', 'com.example.fake-attribute',
                        'fake', str(self.target)], check=True, capture_output=True)
        self.target.chmod(0o400)
        snap, auth = self.prepare()
        result = self.write(snap, auth)
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual((self.folder / result.backup_name).read_bytes(), snap.data)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((self.folder / result.backup_name).stat().st_mode), 0o400)
        for path in (self.target, self.folder / result.backup_name):
            self.assertEqual(subprocess.run(['/usr/bin/xattr', '-p', 'com.example.fake-attribute',
                                             str(path)], capture_output=True).returncode, 0)
        self.assertTrue(self.alias.is_symlink())

    def test_copy_error_warns_and_writes_with_staging_xattr(self):
        from core.cli._config_ops import _mac_runtime_locations
        from core.secure_files import SecureSnapshotError
        snap, auth = self.prepare()
        state = self.root / '.runtime'
        state.mkdir(mode=0o700)
        stage = state / 'tmp'
        stage.mkdir(mode=0o700)
        subprocess.run(['/usr/bin/xattr', '-w', 'com.example.fake-attribute',
                        'fake', str(stage)], check=True, capture_output=True)
        locations = _mac_runtime_locations(self.root, snap, lambda _: True)
        with patch('core.secure_files._copy_extended_attributes',
                   side_effect=SecureSnapshotError('extended_attributes_copy_failed')), \
             self.assertLogs('core.secure_files', level='WARNING') as logged:
            result = replace_config(snap, b'{"new":true}\n', auth,
                                    preflight=lambda _: True,
                                    before_replace=lambda: None, locations=locations)
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual(Path(result.backup_name).read_bytes(), snap.data)
        self.assertTrue(any('extended attributes could not be copied' in line
                            for line in logged.output))

    def test_actual_backend_parent_descriptor_matches_snapshot_protection(self):
        snap, _auth = self.prepare()
        with open_target_parent(snap) as (fd, name):
            parent = os.fstat(fd)
            self.assertEqual(name, 'business.json')
            self.assertEqual((parent.st_dev, parent.st_ino),
                             tuple(snap.target_parent_protection[0][:2]))
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            self.assertEqual((entry.st_dev, entry.st_ino),
                             tuple(snap.final_identity[:2]))

    def test_parent_protection_change_after_replace_is_postcheck_failure(self):
        snap, auth = self.prepare()
        real_replace = os.replace
        def replace_then_change_parent(*args, **kwargs):
            real_replace(*args, **kwargs)
            self.folder.chmod(0o500)
        try:
            with patch('core.mac_config_writer.os.replace', side_effect=replace_then_change_parent):
                with self.assertRaises(MacWriteError) as caught:
                    self.write(snap, auth)
            self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
            self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
            self.assertTrue(self.alias.is_symlink())
        finally:
            self.folder.chmod(0o700)

    def test_backend_parent_close_unknown_after_replace_is_reported(self):
        from core.mac_config_path import MacConfigError
        snap, auth = self.prepare()
        @contextlib.contextmanager
        def close_failure(snapshot):
            with open_target_parent(snapshot) as (fd, name):
                yield fd, name
            raise MacConfigError('fd_close_unknown')
        with patch('core.mac_config_writer.open_target_parent', close_failure):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertTrue(caught.exception.close_unknown)
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')

    def test_symlink_replace_and_backup(self):
        snap, auth = self.prepare()
        link_before = os.lstat(self.alias)
        target_before = os.stat(self.target)
        result = self.write(snap, auth)
        self.assertEqual(result.state, 'VERIFIED')
        link_after = os.lstat(self.alias)
        target_after = os.stat(self.target)
        self.assertEqual((link_before.st_dev, link_before.st_ino),
                         (link_after.st_dev, link_after.st_ino))
        self.assertNotEqual((target_before.st_dev, target_before.st_ino),
                            (target_after.st_dev, target_after.st_ino))
        self.assertTrue(self.alias.is_symlink())
        self.assertEqual(os.readlink(self.alias), 'config/business.json')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual((self.folder / result.backup_name).read_bytes(), snap.data)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(result.marker, capture_config(str(self.alias)).marker)

    def test_absolute_and_multilevel_final_links(self):
        middle = self.root / 'absolute-link'
        middle.symlink_to(self.target)
        self.alias.unlink()
        self.alias.symlink_to(middle)
        before = [(os.lstat(p).st_dev, os.lstat(p).st_ino, os.readlink(p))
                  for p in (self.alias, middle)]
        snap, auth = self.prepare()
        self.write(snap, auth)
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual(before, [(os.lstat(p).st_dev, os.lstat(p).st_ino, os.readlink(p))
                                  for p in (self.alias, middle)])

    def test_unlimited_default_writes_large_ordinary_config(self):
        size = 2 * 1024 * 1024 + 73
        previous = b'{"blob":"' + b'a' * size + b'"}\n'
        new = b'{"blob":"' + b'b' * (size + 19) + b'"}\n'
        self.target.write_bytes(previous)
        snap, auth = self.prepare(self.target)
        self.assertIsNone(snap.max_bytes)
        result = replace_config(snap, new, auth, preflight=lambda _: True,
                                before_replace=lambda: None)
        self.assertEqual(result.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), new)
        self.assertEqual((self.folder / result.backup_name).read_bytes(), previous)

    def test_explicit_size_limit_is_respected(self):
        limit = 64
        snap = capture_config(str(self.target), max_bytes=limit)
        with open_target_parent(snap) as (fd, _):
            st = os.fstat(fd)
            auth = WriteAuthorization(snap.target_path, (st.st_dev, st.st_ino),
                                      tuple(snap.final_identity[:2]), True)
        with self.assertRaises(MacWriteError) as caught:
            replace_config(snap, b'x' * (limit + 1), auth, preflight=lambda _: True,
                           before_replace=lambda: None)
        self.assertEqual(caught.exception.code, 'invalid_new_content')
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snap.data)
        self.assertFalse(list(self.folder.glob('*.bak.*')))

    def test_multilink_directory_and_dotdot_semantics(self):
        (self.folder / 'inner').mkdir()
        other = self.root / 'other'
        other.mkdir()
        (other / 'pivot').symlink_to('../config/inner')
        (self.root / 'bridge').symlink_to('other/pivot')
        (other / 'business.json').write_bytes(b'decoy')
        path = self.root / 'bridge' / '..' / 'business.json'
        before_links = [(os.lstat(p).st_dev, os.lstat(p).st_ino, os.readlink(p))
                        for p in (self.root / 'bridge', other / 'pivot')]
        snap, auth = self.prepare(path)
        self.write(snap, auth)
        self.assertEqual(before_links,
                         [(os.lstat(p).st_dev, os.lstat(p).st_ino, os.readlink(p))
                          for p in (self.root / 'bridge', other / 'pivot')])
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertEqual((other / 'business.json').read_bytes(), b'decoy')
        self.assertTrue((other / 'pivot').is_symlink())
        self.assertTrue((self.root / 'bridge').is_symlink())

    def test_authorization_rejects_foreign_target_and_parent(self):
        snap, auth = self.prepare()
        for bad in (WriteAuthorization('/elsewhere/config', auth.parent_identity,
                                       auth.target_identity, True),
                    WriteAuthorization(auth.target_path, (0, 0), auth.target_identity, True),
                    WriteAuthorization(auth.target_path, auth.parent_identity,
                                       auth.target_identity, False)):
            with self.subTest(bad=bad), self.assertRaises(MacWriteError) as caught:
                self.write(snap, bad)
            self.assertEqual(caught.exception.state, 'NOT_REPLACED')
            self.assertEqual(self.target.read_bytes(), snap.data)
            self.assertEqual(sorted(x.name for x in self.folder.iterdir()), ['business.json'])

    def test_hardlink_rejected(self):
        os.link(self.target, self.folder / 'hardlink')
        snap, auth = self.prepare()
        with self.assertRaises(MacWriteError) as caught:
            self.write(snap, auth)
        self.assertEqual(caught.exception.code, 'hardlinks_not_preserved')
        self.assertEqual(self.target.read_bytes(), snap.data)

    def test_changed_before_commit_and_link_change(self):
        for mutate in ('content', 'link'):
            with self.subTest(mutate=mutate):
                snap, auth = self.prepare()
                def hook():
                    if mutate == 'content':
                        self.target.write_bytes(b'external')
                    else:
                        self.alias.unlink()
                        self.alias.symlink_to('config/./business.json')
                with self.assertRaises(MacWriteError) as caught:
                    self.write(snap, auth, before_replace=hook)
                self.assertEqual(caught.exception.state, 'NOT_REPLACED')
                if mutate == 'content':
                    self.assertEqual(self.target.read_bytes(), b'external')
                    self.target.write_bytes(b'{"old":true}\n')
                else:
                    self.assertEqual(self.target.read_bytes(), snap.data)
                for p in self.folder.glob('business.json.bak.*'):
                    p.unlink()

    def test_protection_change_after_confirmation_is_blocked(self):
        snap, auth = self.prepare()
        def changed():
            self.target.chmod(0o400)
        with self.assertRaises(MacWriteError) as caught:
            self.write(snap, auth, before_replace=changed)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snap.data)
        self.assertTrue(self.alias.is_symlink())

    def test_parent_directory_xattr_change_before_replace_does_not_change_proof(self):
        import ctypes
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        lib.fsetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        lib.fsetxattr.restype = ctypes.c_int
        fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY)
        snap, auth = self.prepare()
        changed = []
        value = b'fake-tag-value'
        def mutate():
            data = ctypes.create_string_buffer(value)
            if lib.fsetxattr(fd, b'com.apple.metadata:_kMDItemUserTags', data,
                             len(value), 0, 0) != 0:
                raise OSError(ctypes.get_errno(), 'fake xattr setup failed')
            changed.append(True)
        try:
            result = self.write(snap, auth, before_replace=mutate)
            self.assertEqual(changed, [True])
            self.assertEqual(result.state, 'VERIFIED')
            self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
            after = capture_config(str(self.alias))
            self.assertEqual(after.target_parent_protection, snap.target_parent_protection)
        finally:
            os.close(fd)

    def test_preflight_failure_is_before_backup(self):
        snap, auth = self.prepare()
        with self.assertRaises(MacWriteError) as caught:
            self.write(snap, auth, preflight=lambda _: False)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertFalse(list(self.folder.glob('*.bak.*')))

    def test_boundary_guard_precedes_backup_staging_and_replace(self):
        from core.mac_config_writer import _open_private as real_create
        real_replace = os.replace
        snap, auth = self.prepare()
        events = []
        def guard():
            events.append('guard')
        def create(parent_fd, name, *args, **kwargs):
            events.append('staging' if name.startswith('.model-proxy-config-') else 'backup')
            return real_create(parent_fd, name, *args, **kwargs)
        def replace(*args, **kwargs):
            events.append('replace')
            return real_replace(*args, **kwargs)
        with patch('core.mac_config_writer._open_private', side_effect=create), \
             patch('core.mac_config_writer.os.replace', side_effect=replace):
            outcome = replace_config(snap, b'{"new":true}\n', auth, preflight=lambda _: True,
                                     before_replace=lambda: events.append('marker'),
                                     boundary_guard=guard)
        self.assertEqual(events, ['guard', 'backup', 'guard', 'staging', 'marker',
                                  'guard', 'replace'])
        self.assertEqual(outcome.state, 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertTrue(self.alias.is_symlink())

    def test_final_preflight_rechecks_before_replace(self):
        snap, auth = self.prepare()
        calls, marked = [], []
        def preflight(actual):
            calls.append(actual)
            return len(calls) < 4
        with self.assertRaises(MacWriteError) as caught:
            self.write(snap, auth, preflight=preflight, before_replace=lambda: marked.append(True))
        self.assertEqual(len(calls), 4)
        self.assertEqual(marked, [True])
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snap.data)

    def test_temp_creation_failure_leaves_matching_backup_and_no_replace(self):
        from core.mac_config_writer import _open_private
        snap, auth = self.prepare()
        calls = 0
        def create(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('fake staging failure')
            return _open_private(*args, **kwargs)
        with patch('core.mac_config_writer._open_private', side_effect=create):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snap.data)
        self.assertEqual((self.folder / caught.exception.backup_name).read_bytes(), snap.data)

    def test_close_unknown_is_reported(self):
        snap, auth = self.prepare()
        with patch('core.mac_config_writer._check_created',
                   side_effect=MacWriteError('staging_close_unknown', close_unknown=True)):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertTrue(caught.exception.close_unknown)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snap.data)

    def test_cleanup_failure_preserves_prior_close_unknown(self):
        from core.mac_config_writer import _check_created
        snap, auth = self.prepare()
        calls = 0
        real_unlink = os.unlink
        def check(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise MacWriteError('staging_close_unknown', close_unknown=True)
            return _check_created(*args, **kwargs)
        def unlink(path, *args, **kwargs):
            if str(path).startswith('.model-proxy-config-'):
                raise OSError('synthetic cleanup failure')
            return real_unlink(path, *args, **kwargs)
        with patch('core.mac_config_writer._check_created', side_effect=check), \
             patch('core.mac_config_writer.os.unlink', side_effect=unlink):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertTrue(caught.exception.close_unknown)
        self.assertTrue(caught.exception.cleanup_unknown)
        self.assertTrue(caught.exception.staging_name)
        self.assertEqual(self.target.read_bytes(), snap.data)

    def test_replace_failure_is_unknown(self):
        snap, auth = self.prepare()
        with patch('core.mac_config_writer.os.replace', side_effect=OSError('test')):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertEqual(caught.exception.state, 'UNKNOWN')
        self.assertFalse(caught.exception.close_unknown)
        self.assertEqual(self.target.read_bytes(), snap.data)
        self.assertTrue(caught.exception.backup_name)

    def test_postcheck_failure_never_rolls_back(self):
        snap, auth = self.prepare()
        with patch('core.mac_config_writer.capture_config', side_effect=RuntimeError('test')):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertEqual(caught.exception.state, 'POSTCHECK_FAILED')
        self.assertEqual(self.target.read_bytes(), b'{"new":true}\n')
        self.assertTrue(self.alias.is_symlink())
        self.assertEqual((self.folder / caught.exception.backup_name).read_bytes(), snap.data)

    def test_staging_protection_failure_before_replace(self):
        snap, auth = self.prepare()
        from core.mac_config_writer import _mac_fd_protection
        def altered(fd, *, directory):
            proof = _mac_fd_protection(fd, directory=directory)
            return proof[:-1] + (((b'fake', b'changed'),),)
        with patch('core.mac_config_writer._mac_fd_protection', side_effect=altered):
            with self.assertRaises(MacWriteError) as caught:
                self.write(snap, auth)
        self.assertEqual(caught.exception.state, 'NOT_REPLACED')
        self.assertEqual(self.target.read_bytes(), snap.data)


if __name__ == '__main__':
    unittest.main()
