"""macOS path tests: only run from a copied project under an isolated /tmp root."""
import dataclasses
import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ISO = Path(os.environ.get('MODEL_PROXY_TEST_ISOLATION', '/invalid')).resolve()
if (Path(__file__).resolve().parents[1] != ISO / 'project'
        or not (ISO / '.model-proxy-isolation').is_file()
        or not Path(os.environ.get('HOME', '/invalid')).resolve().is_relative_to(ISO)
        or not ISO.is_relative_to(Path('/tmp').resolve())):
    raise RuntimeError('isolated source and fake HOME required')
sys.path.insert(0, str(ISO / 'project'))
from core import mac_config_path as m


@unittest.skipUnless(sys.platform == 'darwin', 'native Darwin/APFS required')
class MacConfigPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ISO, prefix='paths-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / 'config'
        self.config_dir.mkdir(mode=0o700)
        self.inner = self.config_dir / 'inner'
        self.inner.mkdir(mode=0o700)
        self.config = self.config_dir / 'config.json'
        self.config.write_bytes(b'{"fake":"TEST_SECRET"}')
        self.config.chmod(0o600)
        self.other = self.root / 'other'
        self.other.mkdir(mode=0o700)

    def capture(self, path=None, **kwargs):
        return m.capture_config(str(self.config if path is None else path), **kwargs)

    def assert_error(self, fn, code=None):
        with self.assertRaises(m.MacConfigError) as ctx:
            fn()
        if code:
            self.assertEqual(ctx.exception.code, code)
        self.assertNotIn('TEST_SECRET', str(ctx.exception))
        self.assertNotIn('fake-secret-path', str(ctx.exception))
        self.assertNotIn(str(self.root), str(ctx.exception))

    def test_direct_relative_and_dotdot(self):
        a = self.capture()
        b = self.capture('inner/../config.json', cwd=str(self.config_dir))
        self.assertIn('/inner/../', b.source_path)
        self.assertEqual(b.target_path, str(self.config))
        self.assertTrue(m.same_target(a, b))
        self.assertNotEqual(a.chain, b.chain)
        self.assertEqual(a.marker.mtime_ns, self.config.stat().st_mtime_ns)
        self.assertNotIn('TEST_SECRET', repr(a))
        self.assertNotIn(str(self.config), repr(a))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.data = b'changed'

    def test_final_relative_link_and_absolute_link(self):
        a = self.other / 'a'; a.symlink_to('../config/config.json')
        b = self.other / 'b'; b.symlink_to(a)
        direct = self.capture()
        sa, sb = self.capture(a), self.capture(b)
        self.assertEqual(sa.target_path, str(self.config))
        self.assertTrue(m.same_target(direct, sa))
        self.assertTrue(m.same_target(sa, sb))
        self.assertNotEqual(sa.marker, sb.marker)

    def test_directory_symlink_and_dotdot_inducer(self):
        decoy = self.other / 'config.json'; decoy.write_bytes(b'decoy'); decoy.chmod(0o600)
        (self.other / 'pivot').symlink_to('../config/inner')
        path = str(self.other) + '/pivot/../config.json'
        snap = self.capture(path)
        self.assertEqual(snap.data, self.config.read_bytes())
        self.assertEqual(snap.target_path, str(self.config))
        self.assertNotEqual(snap.data, decoy.read_bytes())
        self.assertIn('/pivot/../', snap.source_path)

    def test_dots_slashes_and_non_directory(self):
        self.assertEqual(self.capture(str(self.config_dir) + '/./config.json').data, self.config.read_bytes())
        for value in ('', '/', str(self.config) + '/', str(self.config) + '/.',
                      str(self.config) + '/..', str(self.config_dir) + '/.',
                      str(self.config_dir) + '/missing/../config.json'):
            with self.subTest(value=value):
                self.assert_error(lambda: self.capture(value))

    def test_broken_loop_and_link_limit(self):
        p = self.other / 'bad'; p.symlink_to('missing')
        self.assert_error(lambda: self.capture(p))
        p.unlink(); p.symlink_to('loop'); (self.other / 'loop').symlink_to('bad')
        self.assert_error(lambda: self.capture(p), 'too_many_links')
        for n in range(m.MAX_SYMLINKS + 1):
            (self.other / ('l%d' % n)).symlink_to(('l%d' % (n + 1)) if n < m.MAX_SYMLINKS else '../config/config.json')
        self.assert_error(lambda: self.capture(self.other / 'l0'), 'too_many_links')
        self.assertEqual(self.capture(self.other / 'l1').data, self.config.read_bytes())
        self.assertEqual((self.other / 'l1').read_bytes(), self.config.read_bytes())
        with self.assertRaises(OSError) as os_error:
            (self.other / 'l0').read_bytes()
        self.assertEqual(os_error.exception.errno, errno.ELOOP)

    def test_link_change_same_target_changes_marker(self):
        p = self.other / 'link'; p.symlink_to('../config/config.json')
        before = self.capture(p)
        p.unlink(); p.symlink_to('../config/inner/../config.json')
        after = self.capture(p)
        self.assertEqual(before.final_identity, after.final_identity)
        self.assertNotEqual(before.marker, after.marker)
        self.assert_error(lambda: m.verify_config(before), 'config_changed')
        self.assert_error(lambda: m.same_target(before, self.capture()))

    def test_directory_replaced_but_target_same(self):
        path = str(self.inner) + '/../config.json'
        before = self.capture(path)
        self.inner.rename(self.config_dir / 'moved')
        self.inner.mkdir(mode=0o700)
        self.assertEqual(self.capture(path).final_identity, before.final_identity)
        self.assert_error(lambda: m.verify_config(before), 'config_changed')

    def test_size_permissions_and_file_changes(self):
        self.assert_error(lambda: self.capture(max_bytes=1), 'config_too_large')
        self.assert_error(lambda: self.capture(max_bytes=-1), 'invalid_size_limit')
        before = self.capture()
        self.config.chmod(0o644)
        self.assert_error(lambda: m.verify_config(before))
        self.config.chmod(0o600)
        self.config.write_bytes(b'different fake data')
        self.assert_error(lambda: m.verify_config(before), 'config_changed')
        self.inner.chmod(0o777)
        self.assertEqual(self.capture(str(self.inner) + '/../config.json').target_path, str(self.config))
        self.config_dir.chmod(0o777)
        self.assert_error(lambda: self.capture())

    def test_default_unlimited_and_explicit_size_boundaries(self):
        data = b'fake-data-' * (m.MAX_CONFIG_BYTES // 10 + 100) + b'end'
        self.assertGreater(len(data), m.MAX_CONFIG_BYTES)
        self.config.write_bytes(data)
        snapshot = self.capture()
        self.assertIsNone(snapshot.max_bytes)
        self.assertEqual(snapshot.data, data)
        self.assertEqual(m.verify_config(snapshot).data, data)
        with m.open_target_parent(snapshot) as (fd, name):
            self.assertEqual(os.stat(name, dir_fd=fd).st_size, len(data))
        bounded = self.capture(max_bytes=len(data))
        self.assertEqual(bounded.max_bytes, len(data))
        self.assertEqual(bounded.data, data)
        self.assertEqual(snapshot.marker, bounded.marker)
        self.assertTrue(m.same_target(snapshot, bounded))
        self.assert_error(lambda: self.capture(max_bytes=len(data) - 1), 'config_too_large')
        self.assert_error(lambda: self.capture(max_bytes=m.MAX_CONFIG_BYTES), 'config_too_large')
        for invalid in (-1, True, 1.5, '3'):
            with self.subTest(invalid=invalid):
                self.assert_error(lambda: self.capture(max_bytes=invalid), 'invalid_size_limit')
        self.config.write_bytes(b'')
        self.assertEqual(self.capture(max_bytes=0).data, b'')
        self.config.write_bytes(b'x')
        self.assert_error(lambda: self.capture(max_bytes=0), 'config_too_large')

    def test_missing_path_and_missing_during_revalidation(self):
        self.assert_error(lambda: self.capture(self.other / 'missing'), 'file_not_found')
        broken = self.other / 'broken'
        broken.symlink_to('missing')
        self.assert_error(lambda: self.capture(broken), 'file_not_found')
        snapshot = self.capture()
        self.config.unlink()
        self.assert_error(lambda: self.capture(), 'file_not_found')
        self.assert_error(lambda: m.verify_config(snapshot), 'config_changed')

    def test_second_walk_disappearance_or_growth_is_changed(self):
        real = m._walk
        count = 0
        def disappear(*args, **kwargs):
            nonlocal count
            result = real(*args, **kwargs)
            count += 1
            if count == 1:
                self.config.unlink()
            return result
        with mock.patch.object(m, '_walk', side_effect=disappear):
            self.assert_error(lambda: self.capture(), 'config_changed')
        self.config.write_bytes(b'fake')
        self.config.chmod(0o600)
        count = 0
        def grow(*args, **kwargs):
            nonlocal count
            result = real(*args, **kwargs)
            count += 1
            if count == 1:
                self.config.write_bytes(b'fake much larger')
            return result
        with mock.patch.object(m, '_walk', side_effect=grow):
            self.assert_error(lambda: self.capture(max_bytes=4), 'config_changed')

    def test_known_read_errors_are_not_protection_unknown(self):
        real_stat = m.os.stat
        def denied_stat(path, *args, **kwargs):
            if path == 'config.json' and kwargs.get('dir_fd') is not None:
                raise PermissionError(errno.EACCES, 'fake-secret-path')
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(m.os, 'stat', side_effect=denied_stat):
            self.assert_error(lambda: self.capture(), 'permission_denied')
        real_open = m.os.open
        def denied_open(path, *args, **kwargs):
            if path == 'config.json' and kwargs.get('dir_fd') is not None:
                raise PermissionError(errno.EPERM, 'fake-secret-path')
            return real_open(path, *args, **kwargs)
        with mock.patch.object(m.os, 'open', side_effect=denied_open):
            self.assert_error(lambda: self.capture(), 'permission_denied')
        with mock.patch.object(m.os, 'read', side_effect=PermissionError(errno.EACCES, 'fake-secret-path')):
            self.assert_error(lambda: self.capture(), 'permission_denied')
        with mock.patch.object(m.os, 'read', side_effect=OSError(errno.EIO, 'fake-secret-path')):
            self.assert_error(lambda: self.capture(), 'config_read_error')
        # An identical errno inside ACL/filesystem proof is not a read error.
        with mock.patch.object(m, '_mac_fd_protection',
                               side_effect=PermissionError(errno.EACCES, 'fake-secret-path')):
            self.assert_error(lambda: self.capture(), 'filesystem_unverified')
        with mock.patch.object(m, '_mac_fd_protection',
                               side_effect=m.SecureSnapshotError('acl_unavailable')):
            self.assert_error(lambda: self.capture(), 'acl_unavailable')

    def test_missing_or_invalid_after_lstat_is_changed(self):
        real_open = m.os.open
        def gone_after_stat(path, *args, **kwargs):
            if path == 'config.json' and kwargs.get('dir_fd') is not None:
                raise FileNotFoundError(errno.ENOENT, 'fake-secret-path')
            return real_open(path, *args, **kwargs)
        with mock.patch.object(m.os, 'open', side_effect=gone_after_stat):
            self.assert_error(lambda: self.capture(), 'target_changed')
        link = self.other / 'link'
        link.symlink_to('../config/config.json')
        with mock.patch.object(m.os, 'readlink',
                               side_effect=OSError(errno.EINVAL, 'fake-secret-path')):
            self.assert_error(lambda: self.capture(link), 'link_changed')

    def test_replaced_target_is_changed_even_if_new_permissions_unsafe(self):
        real_open = m.os.open
        changed = False
        def replace(path, flags, *args, **kwargs):
            nonlocal changed
            if path == 'config.json' and kwargs.get('dir_fd') is not None and not changed:
                changed = True
                new = self.config_dir / 'replacement'
                new.write_bytes(b'fake-replacement')
                new.chmod(0o644)
                os.replace(new, self.config)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(m.os, 'open', side_effect=replace):
            self.assert_error(lambda: self.capture(), 'target_changed')
        self.assertTrue(changed)

    def test_replaced_directory_is_changed_even_if_new_permissions_unsafe(self):
        real_open = m.os.open
        changed = False
        def replace(path, flags, *args, **kwargs):
            nonlocal changed
            if path == 'inner' and kwargs.get('dir_fd') is not None and not changed:
                changed = True
                self.inner.rename(self.config_dir / 'old-inner')
                self.inner.mkdir(mode=0o700)
                self.inner.chmod(0o777)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(m.os, 'open', side_effect=replace):
            self.assert_error(lambda: self.capture(str(self.inner) + '/../config.json'),
                              'directory_changed')
        self.assertTrue(changed)

    def test_large_file_grows_during_read_and_fails_changed(self):
        self.config.write_bytes(b'a' * (m.MAX_CONFIG_BYTES + 65536))
        original = self.capture()
        self.assertIsNone(original.max_bytes)
        real_read = m.os.read
        changed = False
        def grow(fd, size):
            nonlocal changed
            result = real_read(fd, size)
            if not changed:
                changed = True
                with self.config.open('ab') as stream:
                    stream.write(b'fake-extra')
            return result
        with mock.patch.object(m.os, 'read', side_effect=grow):
            self.assert_error(lambda: self.capture(), 'target_changed')
        self.assertTrue(changed)
        self.assert_error(lambda: m.verify_config(original), 'config_changed')

    def test_sticky_directory_untrusted_entry_owner(self):
        parent = mock.Mock(st_mode=stat.S_IFDIR | 0o1777)
        child = mock.Mock(st_uid=os.geteuid() + 100000)
        self.assert_error(lambda: m._sticky_entry(parent, child), 'sticky_entry_owner_unverified')
        child.st_uid = os.geteuid(); m._sticky_entry(parent, child)

    def test_parent_fd_is_real_target_and_closed(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        snap = self.capture(link)
        with m.open_target_parent(snap) as (fd, name):
            self.assertEqual(name, 'config.json')
            self.assertEqual(os.fstat(fd).st_ino, self.config_dir.stat().st_ino)
            self.assertEqual(os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino, self.config.stat().st_ino)
        with self.assertRaises(OSError): os.fstat(fd)
        self.assertTrue(link.is_symlink())

    def test_parent_fd_rejects_changed_chain(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        snap = self.capture(link)
        link.unlink(); link.symlink_to('../config/inner/../config.json')
        self.assert_error(lambda: m.open_target_parent(snap).__enter__())

    def test_same_target_rejects_changed_second_chain(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        a, b = self.capture(), self.capture(link)
        link.unlink(); link.symlink_to('../config/inner/../config.json')
        self.assert_error(lambda: m.same_target(a, b))

    def test_injected_link_swap_between_stat_and_readlink(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        real = m.os.readlink
        def swap(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path == 'link':
                link.unlink(); link.symlink_to('../config/inner/../config.json')
            return result
        with mock.patch.object(m.os, 'readlink', side_effect=swap):
            self.assert_error(lambda: self.capture(link), 'link_changed')

    def test_injected_replacement_during_read(self):
        real = m.os.read
        called = False
        def swap(fd, count):
            nonlocal called
            data = real(fd, count)
            if not called:
                called = True
                replacement = self.config_dir / 'new'
                replacement.write_bytes(b'fake replacement'); replacement.chmod(0o600)
                os.replace(replacement, self.config)
            return data
        with mock.patch.object(m.os, 'read', side_effect=swap):
            self.assert_error(lambda: self.capture())

    def test_injected_acl_filesystem_and_chain_changes_fail_closed(self):
        for code in ('acl_unavailable', 'filesystem_unverified'):
            with mock.patch.object(m, '_mac_fd_protection', side_effect=m.SecureSnapshotError(code)):
                self.assert_error(lambda: self.capture(), code)
        real = m._walk
        count = 0
        def change(*args, **kwargs):
            nonlocal count
            result = real(*args, **kwargs); count += 1
            if count == 1:
                self.config.write_bytes(b'changed between walks')
            return result
        with mock.patch.object(m, '_walk', side_effect=change):
            self.assert_error(lambda: self.capture(), 'config_changed')

    def test_hardlinks_are_evidenced_for_writer_to_reject(self):
        os.link(self.config, self.other / 'hard')
        snapshot = self.capture()
        self.assertEqual(snapshot.final_identity[5], 2)
        self.assertTrue(m.same_target(snapshot, self.capture(self.other / 'hard')))

    def test_caller_error_is_not_rewritten(self):
        with self.assertRaisesRegex(OSError, 'caller error'):
            with m.open_target_parent(self.capture()):
                raise OSError('caller error')


    def test_marker_tracks_safe_target_and_directory_protection_changes(self):
        first = self.capture()
        self.config.chmod(0o400)
        second = self.capture()
        self.assertEqual(first.marker.mtime_ns, second.marker.mtime_ns)
        self.assertNotEqual(first.marker, second.marker)
        self.assert_error(lambda: m.verify_config(first), 'config_changed')
        self.config_dir.chmod(0o500)
        third = self.capture()
        self.assertEqual(second.marker.mtime_ns, third.marker.mtime_ns)
        self.assertNotEqual(second.marker, third.marker)
        self.config_dir.chmod(0o700)

    def test_config_ancestor_and_parent_xattrs_do_not_drive_marker(self):
        name = 'com.example.config-read-test'
        before = self.capture()
        for directory in (self.root, self.config_dir):
            subprocess.run(['/usr/bin/xattr', '-w', name, 'fake-value', str(directory)],
                           check=True, capture_output=True)
        after = self.capture()
        self.assertEqual(before.marker, after.marker)
        self.assertEqual(m.verify_config(before), before)
        self.assertIsNone(after.target_parent_protection[1][-1])
        self.assertNotIn('fake-value', repr(after.chain))

    def test_file_unknown_xattr_read_allowed_shared_policy_stays_strict(self):
        subprocess.run(['/usr/bin/xattr', '-w', 'com.example.config-read-test',
                        'fake-value', str(self.config)], check=True, capture_output=True)
        snapshot = self.capture()
        self.assertIsNone(snapshot.protection[1][-1])
        self.assertEqual(m.verify_config(snapshot), snapshot)
        fd = os.open(self.config, os.O_RDONLY)
        try:
            with self.assertRaises(m.SecureSnapshotError) as caught:
                m._mac_fd_protection(fd, directory=False)
            self.assertEqual(caught.exception.code, 'extended_attributes_unverified')
        finally:
            os.close(fd)

    def test_target_xattr_only_change_does_not_trigger_reload_marker(self):
        before = self.capture()
        subprocess.run(['/usr/bin/xattr', '-w', 'com.example.fake-attribute',
                        'fake', str(self.config)], check=True, capture_output=True)
        after = self.capture()
        self.assertEqual(before.marker, after.marker)
        self.config.write_bytes(b'{"fake":"different"}')
        self.assertNotEqual(after.marker, self.capture().marker)

    def test_synthetic_lastuseddate_repeated_read_keeps_stable_marker(self):
        # This is a manually created fixture, not a system-produced attribute.
        created = subprocess.run(['/usr/bin/xattr', '-wx', 'com.apple.lastuseddate#PS',
                                  '00' * 16, str(self.config)], capture_output=True)
        if created.returncode:
            self.skipTest('cannot create synthetic lastuseddate')
        first = self.capture()
        first_ctime = self.config.stat().st_ctime_ns
        for _ in range(5):
            self.assertEqual(self.capture().marker, first.marker)
            self.assertEqual(self.config.stat().st_ctime_ns, first_ctime)

    def test_business_config_does_not_enumerate_directory_xattrs(self):
        with mock.patch.object(m, '_directory_attributes', side_effect=AssertionError('unexpected')):
            self.assertEqual(self.capture().data, self.config.read_bytes())

    def test_directory_attributes_retains_strict_shared_policy(self):
        subprocess.run(['/usr/bin/xattr', '-w', 'com.example.config-read-test',
                        'fake-value', str(self.config_dir)], check=True, capture_output=True)
        fd = os.open(self.config_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assert_error(lambda: m._directory_attributes(fd), 'directory_attributes_unsupported')
            self.assert_error(lambda: m._protection(fd, True), 'directory_attributes_unsupported')
        finally:
            os.close(fd)

    def test_unrelated_directory_entries_do_not_change_marker(self):
        before = self.capture()
        unrelated = self.config_dir / 'other-fake-file'
        unrelated.write_bytes(b'fake')
        unrelated.chmod(0o600)
        (self.config_dir / 'other-fake-directory').mkdir(mode=0o700)
        after = self.capture()
        self.assertEqual(before.marker, after.marker)
        self.assertEqual(m.verify_config(before).marker, before.marker)
        unrelated.unlink()
        (self.config_dir / 'other-fake-directory').rmdir()
        self.assertEqual(m.verify_config(before).marker, before.marker)

    def test_marker_tracks_symlink_mode(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        first = self.capture(link)
        subprocess.run(['/bin/chmod', '-h', '700', str(link)], check=True, capture_output=True)
        second = self.capture(link)
        self.assertEqual(first.marker.mtime_ns, second.marker.mtime_ns)
        self.assertNotEqual(first.marker, second.marker)
        self.assert_error(lambda: m.verify_config(first), 'config_changed')

    def test_sticky_directory_chain_checks_entry_owner(self):
        sticky = self.root / 'sticky'; sticky.mkdir(mode=0o700); sticky.chmod(0o1777)
        link = sticky / 'link'; link.symlink_to('../config/config.json')
        self.assertEqual(self.capture(link).data, self.config.read_bytes())
        real_stat = m.os.stat
        def stat_untrusted(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == 'link' and kwargs.get('dir_fd') is not None:
                return mock.Mock(wraps=info, st_uid=os.geteuid() + 100000)
            return info
        with mock.patch.object(m.os, 'stat', side_effect=stat_untrusted):
            self.assert_error(lambda: self.capture(link), 'sticky_entry_owner_unverified')

    def test_unknown_symlink_xattr_is_readable_but_identity_changes_are_detected(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        snapshot = self.capture(link)
        subprocess.run(['/usr/bin/xattr', '-w', '-s', 'com.example.fake-test', 'fake', str(link)],
                       check=True, capture_output=True)
        self.assert_error(lambda: m.verify_config(snapshot), 'config_changed')
        self.assertEqual(self.capture(link).data, self.config.read_bytes())
        fd = os.open(link, os.O_SYMLINK | os.O_CLOEXEC)
        try:
            self.assert_error(lambda: m._link_protection(fd), 'link_attributes_unverified')
        finally:
            os.close(fd)

    def test_real_acl_rejected_on_file_and_directory(self):
        before = self.capture()
        subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(self.config)],
                       check=True, capture_output=True)
        self.assert_error(lambda: m.verify_config(before))
        subprocess.run(['/bin/chmod', '-N', str(self.config)], check=True, capture_output=True)
        path = str(self.inner) + '/../config.json'
        before = self.capture(path)
        subprocess.run(['/bin/chmod', '+a', 'everyone allow list', str(self.inner)],
                       check=True, capture_output=True)
        self.assertEqual(self.capture(path).data, self.config.read_bytes())
        subprocess.run(['/bin/chmod', '+a', 'everyone allow list', str(self.config_dir)],
                       check=True, capture_output=True)
        self.assert_error(lambda: self.capture(path))

    def test_link_acl_is_checked_on_link_itself(self):
        link = self.other / 'link'; link.symlink_to('../config/config.json')
        snapshot = self.capture(link)
        subprocess.run(['/bin/chmod', '-h', '+a', 'everyone allow writesecurity', str(link)],
                       check=True, capture_output=True)
        self.assert_error(lambda: m.verify_config(snapshot), 'link_acl_present')
        self.assert_error(lambda: self.capture(link), 'link_acl_present')
        # The target itself has no ACL; the direct path remains valid.
        self.assertEqual(self.capture().data, self.config.read_bytes())

    def test_target_and_parent_replaced_between_stat_and_open(self):
        real_open = m.os.open
        happened = False
        def swap(name, flags, *args, **kwargs):
            nonlocal happened
            if name == 'inner' and not happened:
                happened = True
                self.inner.rename(self.config_dir / 'old-inner')
                self.inner.mkdir(mode=0o700)
            return real_open(name, flags, *args, **kwargs)
        with mock.patch.object(m.os, 'open', side_effect=swap):
            self.assert_error(lambda: self.capture(str(self.inner) + '/../config.json'),
                              'directory_changed')
        self.assertTrue(happened)

    def test_final_replaced_by_symlink_between_stat_and_open(self):
        real_open = m.os.open
        happened = False
        def swap(name, flags, *args, **kwargs):
            nonlocal happened
            if name == 'config.json' and not happened:
                happened = True
                self.config.rename(self.config_dir / 'old.json')
                self.config.symlink_to('old.json')
            return real_open(name, flags, *args, **kwargs)
        with mock.patch.object(m.os, 'open', side_effect=swap):
            self.assert_error(lambda: self.capture())
        self.assertTrue(happened)

    def test_open_parent_detects_replacement_before_yield(self):
        snapshot = self.capture()
        real_verify = m.verify_config
        count = 0
        def swap(snap):
            nonlocal count
            count += 1
            if count == 2:
                self.config.write_bytes(b'fake changed')
            return real_verify(snap)
        with mock.patch.object(m, 'verify_config', side_effect=swap):
            self.assert_error(lambda: m.open_target_parent(snapshot).__enter__())
        self.assertEqual(count, 2)

    def test_fd_close_failure_reports_and_closes_duplicate(self):
        duplicate = []
        real_dup, real_close = m.os.dup, m.os.close
        failed = False
        def dup(fd):
            result = real_dup(fd)
            duplicate.append(result)
            return result
        def close(fd):
            nonlocal failed
            real_close(fd)
            if not failed and duplicate and fd not in duplicate:
                failed = True
                raise OSError('simulated close failure')
        with mock.patch.object(m.os, 'dup', side_effect=dup), \
             mock.patch.object(m.os, 'close', side_effect=close):
            self.assert_error(lambda: m._checked_walk(str(self.config), m.MAX_CONFIG_BYTES,
                                                       hold_parent=True), 'fd_close_unknown')
        self.assertTrue(failed)
        self.assertEqual(len(duplicate), 1)
        with self.assertRaises(OSError): os.fstat(duplicate[0])

    def test_repeated_dotdot_succeeds_under_low_descriptor_limit(self):
        (self.config_dir / 'a').mkdir(mode=0o700)
        raw = str(self.config_dir) + '/a/..' * 130 + '/config.json'
        script = '''
import os, resource, sys
project, raw, direct = sys.argv[1:]
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
assert hard == resource.RLIM_INFINITY or hard >= 256
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
sys.path.insert(0, project)
from core import mac_config_path as m
with open(raw, 'rb') as stream:
    expected = stream.read()
snapshot = m.capture_config(raw)
assert snapshot.data == expected
assert snapshot.target_path == direct
assert m.verify_config(snapshot).data == expected
with m.open_target_parent(snapshot) as (fd, name):
    assert os.stat(name, dir_fd=fd).st_ino == os.stat(direct).st_ino
'''
        subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script,
                        str(ISO / 'project'), raw, str(self.config)],
                       cwd=str(ISO / 'project'), check=True, capture_output=True)

    def test_reused_fd_leases_close_once_on_success_and_error(self):
        (self.config_dir / 'a').mkdir(mode=0o700)
        link = self.other / 'absolute'
        link.symlink_to(self.config)
        raw = str(self.config_dir) + '/a/..' * 40 + '/config.json'
        real_open, real_dup, real_close = m.os.open, m.os.dup, m.os.close
        live, seen, reused = set(), set(), set()
        def register(fd):
            self.assertNotIn(fd, live)
            if fd in seen:
                reused.add(fd)
            seen.add(fd)
            live.add(fd)
            return fd
        def tracked_open(*args, **kwargs):
            return register(real_open(*args, **kwargs))
        def tracked_dup(fd):
            return register(real_dup(fd))
        def tracked_close(fd):
            self.assertIn(fd, live, 'attempt to close an inactive or reused fd')
            live.remove(fd)
            return real_close(fd)
        with mock.patch.object(m.os, 'open', side_effect=tracked_open), \
             mock.patch.object(m.os, 'dup', side_effect=tracked_dup), \
             mock.patch.object(m.os, 'close', side_effect=tracked_close):
            snapshot = self.capture(raw)
            with m.open_target_parent(snapshot) as (fd, name):
                self.assertEqual(name, 'config.json')
                self.assertIn(fd, live)
            self.assertEqual(self.capture(link).data, snapshot.data)
            self.assert_error(lambda: self.capture(raw + '/missing'))
        self.assertFalse(live)
        self.assertTrue(reused)

    def test_dotdot_close_failure_is_not_retried(self):
        inode = self.inner.stat().st_ino
        real_close = m.os.close
        failed = False
        closed = []
        def fail_once(fd):
            nonlocal failed
            self.assertNotIn(fd, closed)
            closed.append(fd)
            info = os.fstat(fd)
            real_close(fd)
            if info.st_ino == inode and not failed:
                failed = True
                raise OSError('simulated close uncertainty')
        with mock.patch.object(m.os, 'close', side_effect=fail_once):
            self.assert_error(lambda: self.capture(str(self.inner) + '/../config.json'),
                              'fd_close_unknown')
        self.assertTrue(failed)

    def test_absolute_link_reset_close_failure_is_not_retried(self):
        link = self.other / 'absolute'
        link.symlink_to(self.config)
        inode = self.other.stat().st_ino
        real_close = m.os.close
        failed = False
        # fd numbers can be reused after the link FD closes; track only the
        # first descriptor that actually fails, not numeric history.
        failed_fd = None
        def fail_once(fd):
            nonlocal failed, failed_fd
            if failed and fd == failed_fd:
                self.fail('uncertain close was retried')
            info = os.fstat(fd)
            real_close(fd)
            if info.st_ino == inode and not failed:
                failed, failed_fd = True, fd
                raise OSError('simulated close uncertainty')
        with mock.patch.object(m.os, 'close', side_effect=fail_once):
            self.assert_error(lambda: self.capture(link), 'fd_close_unknown')
        self.assertTrue(failed)

    def test_uncertain_unclosed_fd_is_not_retried(self):
        inode = self.inner.stat().st_ino
        real_close = m.os.close
        failed_fd = None
        def fail_without_closing(fd):
            nonlocal failed_fd
            if fd == failed_fd:
                self.fail('uncertain close was retried')
            if failed_fd is None and os.fstat(fd).st_ino == inode:
                failed_fd = fd
                raise OSError('simulated close uncertainty')
            real_close(fd)
        try:
            with mock.patch.object(m.os, 'close', side_effect=fail_without_closing):
                self.assert_error(lambda: self.capture(str(self.inner) + '/../config.json'),
                                  'fd_close_unknown')
            self.assertIsNotNone(failed_fd)
            self.assertEqual(os.fstat(failed_fd).st_ino, inode)
        finally:
            if failed_fd is not None:
                real_close(failed_fd)

    def test_relative_cwd_preserves_symlink_dotdot_semantics(self):
        pivot = self.other / 'pivot'; pivot.symlink_to('../config/inner')
        snap = self.capture('../config.json', cwd=str(pivot))
        self.assertEqual(snap.target_path, str(self.config))
        self.assertIn('/pivot/../', snap.source_path)
        self.assertTrue(m.same_target(snap, self.capture()))

    def test_different_targets_never_equal(self):
        other = self.other / 'other.json'
        other.write_bytes(self.config.read_bytes()); other.chmod(0o600)
        self.assertFalse(m.same_target(self.capture(), self.capture(other)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
