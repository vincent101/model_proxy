"""Only virtual handles/evidence; no Windows, user files, services, or network."""
from __future__ import annotations

from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import core.discovery as discovery
import core.platform_windows as win
import core.windows_acl as acl
import core.windows_secure_files as secure
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_acl import raw_ace
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class EmptyHandleTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    TARGET = ROOT + r"\client.json"
    TEMP = ROOT + r"\temporary"

    def setUp(self):
        self.fs = ConfigFS()
        self.fs.objects[self.TARGET] = dict(self.fs.obj(801), data=b"fake-old-secret")
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
        self.snap = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.snap.close)
        self.owned = win.open_file(self.TEMP, create=True, backend=self.fs)
        self.addCleanup(self.owned.close)

    def inspect(self):
        return secure.inspect_empty_handle(self.snap, self.owned, acl_backend=self.acl)

    def test_equal_private_protection_on_empty_distinct_same_volume_handle(self):
        observed = self.inspect()
        self.assertEqual(observed.identity.file_index, 1000)
        self.assertEqual(observed.protection, self.snap._read.protection)
        self.assertEqual(observed.marker[-1], 0)
        self.assertNotIn("fake-old-secret", repr(observed))
        self.assertIn(self.owned.handle, self.fs.handles)
        self.assertEqual(self.fs.objects[self.TEMP].get("data", b""), b"")

    def test_unsafe_or_different_descriptor_never_writes(self):
        for field, value in (("control", 0x8004), ("unsafe_group", True),
                             ("volume", 18), ("links", 2), ("attributes", 0x400),
                             ("index", 801), ("data", b"already populated")):
            with self.subTest(field=field):
                obj = self.fs.objects[self.TEMP]
                old = dict(obj)
                obj[field] = value
                with self.assertRaises(secure.SecureSnapshotError):
                    self.inspect()
                self.assertEqual(obj[field], value)
                self.assertFalse(self.owned.closed)
                obj.clear()
                obj.update(old)

    def test_owner_group_and_inherited_ace_must_match_original(self):
        original = self.acl.read_observation
        handle = self.owned.handle
        def altered(change):
            def read(current, kind, required):
                result = original(current, kind, required)
                if current != handle:
                    return result
                if change == "owner":
                    return replace(result, owner=acl._SYSTEM)
                if change == "group":
                    return replace(result, group=acl._SYSTEM)
                ace = result.aces[0]
                changed = acl._parse_ace(raw_ace(ace.sid, flags=ace.flags | 0x10, mask=ace.mask))
                return replace(result, aces=(changed,) + result.aces[1:])
            return read
        for change in ("owner", "group", "inherited_ace"):
            with self.subTest(change=change), patch.object(self.acl, "read_observation", side_effect=altered(change)):
                with self.assertRaises(secure.SecureSnapshotError) as caught:
                    self.inspect()
                self.assertEqual(caught.exception.code, "protection_changed")
                self.assertEqual(self.fs.objects[self.TEMP].get("data", b""), b"")

    def test_changed_target_bytes_or_parent_reject(self):
        self.fs.objects[self.TARGET]["data"] = b"changed"
        with self.assertRaises(secure.SecureSnapshotError) as caught:
            self.inspect()
        self.assertEqual(caught.exception.code, "target_changed")
        self.assertFalse(self.owned.closed)

    def test_temp_changes_during_acl_observation_reject(self):
        temp_handle = self.owned.handle
        def mutate(handle, kind):
            if handle == temp_handle:
                self.fs.objects[self.TEMP]["data"] = b"raced"
        self.acl.on_read = mutate
        with self.assertRaises(secure.SecureSnapshotError):
            self.inspect()
        self.assertFalse(self.owned.closed)

    def test_unknown_close_or_acl_evidence_does_not_become_success(self):
        actual = acl.read_stable_evidence
        def inject(handle, **kwargs):
            if handle == self.owned.handle:
                raise acl.ACLVerificationError("handle_close_failed")
            return actual(handle, **kwargs)
        with patch.object(acl, "read_stable_evidence", side_effect=inject):
            with self.assertRaises(secure.SecureSnapshotError) as caught:
                self.inspect()
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertFalse(self.owned.closed)
        with self.assertRaises(secure.SecureSnapshotError) as invalid:
            self.snap.revalidate()
        self.assertEqual(invalid.exception.code, "snapshot_invalid")

    def test_closed_or_not_new_handle_reject(self):
        self.owned.created = False
        with self.assertRaises(secure.SecureSnapshotError):
            self.inspect()
        self.owned.created = True
        self.owned.close()
        with self.assertRaises(secure.SecureSnapshotError):
            self.inspect()

    def test_missing_target_has_no_invented_default_protection(self):
        self.snap.close()
        del self.fs.objects[self.TARGET]
        self.snap = secure.capture_target(self.TARGET, authorized_root=self.ROOT, max_bytes=1024)
        self.addCleanup(self.snap.close)
        with self.assertRaises(secure.SecureSnapshotError) as caught:
            self.inspect()
        self.assertEqual(caught.exception.code, "creation_protection_unverified")



if __name__ == "__main__":
    unittest.main()
