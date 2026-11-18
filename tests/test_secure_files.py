"""Read-only fake install-target snapshots; ACL evidence is deliberately mocked."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.secure_files import (
    ProtectionEvidence, SecureSnapshotError, capture_target, target_unchanged,
)


class SecureSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "authorized"
        self.root.mkdir(mode=0o700)
        self.target = self.root / "client.json"
        self.target.write_bytes(b'{"token":"FAKE_SECRET"}')
        self.target.chmod(0o600)

    @staticmethod
    def fake_acl(path, info):
        return ProtectionEvidence(True, (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), "fake"))

    def capture(self, target=None, reader=None, limit=1024):
        return capture_target(target or self.target, authorized_root=self.root,
                              protection_reader=self.fake_acl if reader is None else reader,
                              max_bytes=limit)

    def test_no_acl_adapter_fails_before_any_filesystem_access(self):
        with patch("os.lstat") as lstat, patch("os.open") as opened:
            with self.assertRaises(SecureSnapshotError) as caught:
                capture_target(self.target, authorized_root=self.root, protection_reader=None, max_bytes=1024)
        self.assertEqual(caught.exception.code, "protection_unknown")
        lstat.assert_not_called()
        opened.assert_not_called()

    def test_existing_snapshot_is_read_only_and_repr_hides_contents_and_path(self):
        before = self.target.stat()
        snapshot = self.capture()
        self.assertEqual(snapshot.contents, b'{"token":"FAKE_SECRET"}')
        self.assertNotIn("FAKE_SECRET", repr(snapshot))
        self.assertNotIn(str(self.target), repr(snapshot))
        after = self.target.stat()
        self.assertEqual((before.st_ino, before.st_mode, before.st_size),
                         (after.st_ino, after.st_mode, after.st_size))
        self.assertTrue(target_unchanged(snapshot, protection_reader=self.fake_acl, max_bytes=1024))

    def test_absent_snapshot_does_not_create_target_and_detects_later_creation(self):
        self.target.unlink()
        snapshot = self.capture()
        self.assertFalse(snapshot.exists)
        self.assertFalse(self.target.exists())
        self.target.write_bytes(b"fake new file")
        self.assertFalse(target_unchanged(snapshot, protection_reader=self.fake_acl, max_bytes=1024))

    def test_changed_bytes_or_permissions_invalidate_snapshot(self):
        snapshot = self.capture()
        self.target.write_bytes(b'{"token":"DIFFERENT!!"}')
        self.assertFalse(target_unchanged(snapshot, protection_reader=self.fake_acl, max_bytes=1024))
        snapshot = self.capture()
        self.target.chmod(0o640)
        self.assertFalse(target_unchanged(snapshot, protection_reader=self.fake_acl, max_bytes=1024))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)

    def test_atomic_replacement_invalidates_snapshot(self):
        snapshot = self.capture()
        replacement = self.root / "replacement"
        replacement.write_bytes(b'{"token":"FAKE_SECRET"}')
        os.replace(replacement, self.target)
        self.assertFalse(target_unchanged(snapshot, protection_reader=self.fake_acl, max_bytes=1024))

    def test_target_symlink_is_rejected_without_reading_fake_secret(self):
        secret = self.root / "fake-secret"
        secret.write_bytes(b"fake protected bytes")
        self.target.unlink()
        self.target.symlink_to(secret)
        with patch("os.read", side_effect=AssertionError("must not read link target")):
            with self.assertRaises(SecureSnapshotError) as caught:
                self.capture()
        self.assertEqual(caught.exception.code, "link_or_type_unknown")
        self.assertEqual(secret.read_bytes(), b"fake protected bytes")

    def test_parent_symlink_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (other / "client.json").write_bytes(b"fake")
        alias = self.root / "alias"
        alias.symlink_to(other, target_is_directory=True)
        with self.assertRaises(SecureSnapshotError) as caught:
            self.capture(target=alias / "client.json")
        self.assertEqual(caught.exception.code, "parent_link_or_type")

    def test_hardlinked_target_is_rejected_before_read(self):
        os.link(self.target, self.root / "alias")
        with patch("os.read", side_effect=AssertionError("must not read hardlink")):
            with self.assertRaises(SecureSnapshotError) as caught:
                self.capture()
        self.assertEqual(caught.exception.code, "link_or_type_unknown")

    def test_swap_to_symlink_between_stat_and_open_is_rejected(self):
        secret = self.root / "fake-secret"
        secret.write_bytes(b"fake protected bytes")
        original_open = os.open
        swapped = False

        def race(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == self.target.name and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                self.target.unlink()
                self.target.symlink_to(secret)
            return original_open(path, flags, *args, **kwargs)

        with patch("os.open", side_effect=race):
            with self.assertRaises(SecureSnapshotError):
                self.capture()
        self.assertTrue(swapped)
        self.assertEqual(secret.read_bytes(), b"fake protected bytes")

    def test_swap_after_read_is_detected_before_snapshot_return(self):
        secret = self.root / "fake-secret"
        secret.write_bytes(b"fake protected bytes")
        original_read = os.read
        swapped = False

        def race(fd, count):
            nonlocal swapped
            data = original_read(fd, count)
            if data and not swapped:
                swapped = True
                self.target.unlink()
                self.target.symlink_to(secret)
            return data

        with patch("os.read", side_effect=race):
            with self.assertRaises(SecureSnapshotError) as caught:
                self.capture()
        self.assertEqual(caught.exception.code, "target_changed")
        self.assertEqual(secret.read_bytes(), b"fake protected bytes")

    def test_acl_rejection_and_exception_are_sanitized(self):
        def rejected(path, info):
            return ProtectionEvidence(False, "FAKE_SECRET")
        with patch("os.read", side_effect=AssertionError("must not read")):
            with self.assertRaises(SecureSnapshotError) as caught:
                self.capture(reader=rejected)
        self.assertEqual(caught.exception.code, "protection_unknown")
        self.assertNotIn("FAKE_SECRET", str(caught.exception))

        def throws(path, info):
            raise RuntimeError("FAKE_SECRET")
        with self.assertRaises(SecureSnapshotError) as caught:
            self.capture(reader=throws)
        self.assertNotIn("FAKE_SECRET", str(caught.exception))

    def test_limits_and_authorized_root_are_fail_closed(self):
        with patch("os.read", side_effect=AssertionError("must not read")):
            with self.assertRaises(SecureSnapshotError) as caught:
                self.capture(limit=1)
        self.assertEqual(caught.exception.code, "target_too_large")
        with self.assertRaises(SecureSnapshotError) as caught:
            self.capture(target=self.root.parent / "outside.json")
        self.assertEqual(caught.exception.code, "outside_authorized_root")

    def test_windows_fails_without_opening_anything(self):
        with patch("core.secure_files.os.name", "nt"), patch("os.open") as opened:
            with self.assertRaises(SecureSnapshotError) as caught:
                self.capture()
        opened.assert_not_called()
        self.assertEqual(caught.exception.code, "platform_unverified")


if __name__ == "__main__":
    unittest.main()
