"""Offline tests of read-only Windows install snapshots using synthetic ACLs."""
from __future__ import annotations

import os
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import core.discovery as discovery
import core.platform_windows as win
import core.windows_acl as acl
import core.windows_secure_files as secure
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class SecureWindowsSnapshotTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    TARGET = ROOT + r"\client.json"
    SECRET = b"fake-token-value"

    def setUp(self):
        self.fs = ConfigFS()
        self.fs.objects[self.TARGET] = dict(self.fs.obj(801), data=self.SECRET)
        self.acl = VirtualACL(self.fs)
        self.paths = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(secure, "_windows_path_backend", return_value=self.paths),
            patch.object(discovery, "_windows_path_backend", return_value=self.paths),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
        ):
            item.start()
            self.addCleanup(item.stop)

    def capture(self, target=None, root=None, limit=1024):
        return secure.capture_target(target or self.TARGET,
                                     authorized_root=root or self.ROOT, max_bytes=limit)

    def check_error(self, code, action):
        with self.assertRaises(secure.SecureSnapshotError) as caught:
            action()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn(self.SECRET.decode(), str(caught.exception))
        self.assertNotIn("C:\\fixture", str(caught.exception))
        return caught.exception

    def assert_no_creation(self):
        self.assertFalse(any(event[0] == "open" and event[3] for event in self.fs.calls))

    def test_existing_read_revalidate_and_hold_handles(self):
        with self.capture() as snapshot:
            self.assertTrue(snapshot.exists)
            self.assertEqual(snapshot.contents, self.SECRET)
            self.assertNotIn(self.SECRET.decode(), repr(snapshot))
            self.assertNotIn(self.TARGET, repr(snapshot))
            with self.assertRaises(FrozenInstanceError):
                snapshot.contents = b"altered preview"
            self.assertTrue(self.fs.handles)
            snapshot.revalidate()
            snapshot.revalidate()
            self.assert_no_creation()
        self.assertFalse(self.fs.handles)
        self.check_error("snapshot_invalid", snapshot.revalidate)

    def test_byte_change_even_same_size_and_marker_requires_new_preview(self):
        with self.capture() as snapshot:
            self.fs.objects[self.TARGET]["data"] = b"X" * len(self.SECRET)
            self.check_error("target_changed", snapshot.revalidate)
            self.fs.objects[self.TARGET]["data"] = self.SECRET
            self.check_error("snapshot_invalid", snapshot.revalidate)
        self.assert_no_creation()

    def test_target_acl_change_or_unsafe_acl_invalidates_snapshot(self):
        for change in ("control", "unsafe_group"):
            with self.subTest(change=change):
                obj = self.fs.objects[self.TARGET]
                old = obj[change]
                with self.capture() as snapshot:
                    obj[change] = old ^ 0x1000 if change == "control" else True
                    self.check_error(None, snapshot.revalidate)
                    obj[change] = old
                    self.check_error("snapshot_invalid", snapshot.revalidate)
                self.assertFalse(self.fs.handles)
        self.assert_no_creation()

    def test_unknown_reader_error_is_sanitized_and_sticky(self):
        with self.capture() as snapshot:
            with patch.object(secure, "read_guarded_file", side_effect=OSError("fake-token-value")):
                self.check_error("target_unverified", snapshot.revalidate)
            self.check_error("snapshot_invalid", snapshot.revalidate)
        self.assertFalse(self.fs.handles)

    def test_identity_change_or_parent_acl_change_rejected(self):
        for change in ("target", "parent_acl", "parent_identity"):
            with self.subTest(change=change):
                with self.capture() as snapshot:
                    original = self.fs.objects[self.TARGET if change == "target" else self.ROOT]
                    replacement = dict(original)
                    if change == "parent_acl":
                        replacement["control"] ^= 0x1000
                    else:
                        replacement["index"] += 99
                    key = self.TARGET if change == "target" else self.ROOT
                    self.fs.objects[key] = replacement
                    self.check_error(None, snapshot.revalidate)
                    self.fs.objects[key] = original
                self.assertFalse(self.fs.handles)
        self.assert_no_creation()

    def test_missing_target_creation_and_missing_parent(self):
        del self.fs.objects[self.TARGET]
        with self.capture() as snapshot:
            self.assertFalse(snapshot.exists)
            self.assertIsNone(snapshot.contents)
            snapshot.revalidate()
            self.fs.objects[self.TARGET] = dict(self.fs.obj(802), data=b"new")
            self.check_error("target_changed", snapshot.revalidate)
        self.assertFalse(self.fs.handles)
        self.check_error("parent_missing", lambda: self.capture(self.ROOT + r"\missing\file"))
        self.assert_no_creation()

    def test_reparse_hardlink_and_unsafe_acl_fail_before_read(self):
        for field, value in (("attributes", 0x400), ("links", 2), ("unsafe_group", True)):
            with self.subTest(field=field):
                obj = self.fs.objects[self.TARGET]
                before = obj[field]
                obj[field] = value
                self.fs.reads.clear()
                self.check_error(None, self.capture)
                self.assertEqual(self.fs.reads, [])
                self.assertFalse(self.fs.handles)
                obj[field] = before
        for path in (self.ROOT, r"C:\fixture"):
            obj = self.fs.objects[path]
            obj["attributes"] |= 0x400
            self.check_error(None, self.capture)
            self.assertFalse(self.fs.handles)
            obj["attributes"] &= ~0x400
        self.assert_no_creation()

    def test_changed_during_read_and_oversized_fail(self):
        def change(handle, obj):
            obj["data"] = b"changed payload"
            obj["change"] = 3000
        self.fs.on_read = change
        self.check_error(None, self.capture)
        self.assertFalse(self.fs.handles)
        self.fs.on_read = None
        self.check_error(None, lambda: self.capture(limit=1))
        self.assertFalse(self.fs.handles)
        self.assert_no_creation()

    def test_rejects_invalid_and_outside_authorization_without_path_access(self):
        for target, root in ((r"C:\fixture\other\file", self.ROOT),
                             (self.ROOT, self.ROOT),
                             (r"C:\fixture\project\..\other", self.ROOT),
                             (r"C:\fixture\PROJECT\file", self.ROOT),
                             (r"D:\fixture\project\file", self.ROOT)):
            with self.subTest(target=target):
                calls = len(self.fs.calls)
                self.check_error(None, lambda: self.capture(target, root))
                self.assertEqual(len(self.fs.calls), calls)
        for limit in (-1, True, None):
            self.check_error("invalid_limit", lambda: self.capture(limit=limit))
        self.assert_no_creation()

    def test_unknown_close_is_reported(self):
        snapshot = self.capture()
        handle = next(iter(self.fs.handles))
        self.fs.close_fail.add(handle)
        self.check_error("release_unknown", snapshot.close)
        self.assertEqual(self.fs.close_calls.count(handle), 1)
        self.check_error("snapshot_invalid", snapshot.revalidate)

    def test_write_unconditionally_refused_no_staging_backup_or_replace(self):
        with self.capture() as snapshot:
            old = dict(self.fs.objects)
            with self.assertRaises(secure.SecureWriteError) as caught:
                secure.write_target(snapshot, b"new-fake-secret")
            self.assertEqual(caught.exception.code, "windows_secure_write_unverified")
            self.assertEqual(caught.exception.state, "NOT_REPLACED")
            self.assertIsNone(caught.exception.backup)
            self.assertEqual(old, self.fs.objects)
            self.assertEqual(self.fs.objects[self.TARGET]["data"], self.SECRET)
            self.assert_no_creation()

    def test_write_fails_even_without_a_valid_snapshot(self):
        with self.assertRaises(secure.SecureWriteError) as caught:
            secure.write_target(None, b"fake")
        self.assertEqual(caught.exception.state, "NOT_REPLACED")
        self.assertEqual(self.fs.calls, [])

    def test_non_windows_capture_refuses_before_path_or_acl_access(self):
        with patch.object(os, "name", "posix"):
            self.check_error("platform_unverified", self.capture)
        self.assertEqual(self.fs.calls, [])


if __name__ == "__main__":
    unittest.main()
