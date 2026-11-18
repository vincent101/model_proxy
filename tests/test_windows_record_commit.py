"""Record commits with synthetic NTFS/Win32 behavior; never touches real paths."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import core.discovery as discovery
import core.platform_lock as locks
import core.platform_windows as win
import core.windows_acl as acl
from core.runtime_paths import PATH_KEYS
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL, VirtualCRT
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class RecordFS(ConfigFS):
    def __init__(self):
        super().__init__()
        self.commit_calls = []
        self.fail_code = None
        self.fail_only_replace = False
        self.post_acl_change = False
        self.post_data_change = False
        self.post_attribute_change = False
        self.post_other_role = None
        self.unsafe_temp_acl = False
        self.bad_temp_close = False
        self.write_calls = []

    def _open(self, path, typ, create=False):
        handle = super()._open(path, typ, create)
        if create and ".record-" in path:
            if self.unsafe_temp_acl:
                self.handles[handle][1]["unsafe_group"] = True
            if self.bad_temp_close:
                self.handles[handle][1]["attributes"] = 0x400
                self.close_fail.add(handle)
        return handle

    def write_all_and_flush(self, handle, data):
        self.write_calls.append(handle)
        obj = self.handles[handle][1]
        obj["data"] = data
        obj["write"] = obj.get("write", 1000) + 100
        obj["change"] = obj.get("change", 2000) + 100

    def _assert_no_target_or_source_handles(self, target, source):
        if any(path in (target, source) for path, _ in self.handles.values()):
            raise AssertionError("rename while a no-delete-sharing file handle is open")

    def _commit(self, target, source, replace):
        self.commit_calls.append(("replace" if replace else "new", target, source))
        self._assert_no_target_or_source_handles(target, source)
        if self.fail_code is not None and (replace or not self.fail_only_replace):
            if self.fail_code in (1176, 1177):
                old = self.objects.pop(target, None)
                if self.fail_code == 1177 and old is not None:
                    self.objects[target + ".unlocated"] = old
                    self.objects[source]["control"] ^= 0x1000
            raise win._error(self.fail_code, operation="replace")
        if not replace and target in self.objects:
            raise win._error(183, operation="move")
        if replace and target not in self.objects:
            raise win._error(2, operation="replace")
        self.objects[target] = self.objects.pop(source)
        if self.post_acl_change:
            self.objects[target]["control"] ^= 0x1000
        if self.post_data_change:
            self.objects[target]["data"] += b"changed"
            self.objects[target]["change"] += 100
        if self.post_attribute_change:
            self.objects[target]["attributes"] ^= 0x20
        if self.post_other_role is not None:
            self.objects[self.post_other_role] = self.obj(777)

    def replace_existing(self, target, source): self._commit(target, source, True)
    def move_new(self, source, target): self._commit(target, source, False)


class WindowsRecordCommitTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    STATE = r"C:\fixture\project\.runtime"
    RUNTIME = r"C:\fixture\runtime"

    def setUp(self):
        self.fs = RecordFS()
        self.crt = VirtualCRT(self.fs)
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.paths = {key: self.RUNTIME + "\\" + key for key in PATH_KEYS}
        self.install = {"schema": discovery.INSTANCE_SCHEMA, "record": "install", "install_id": "a" * 32,
                        "project_root": self.ROOT, "root_identity": {"device": 17, "inode": 11},
                        "ever_started": False, "last_instance_nonce": None,
                        "initialization_source": "fake fixture"}
        self.counter = iter((f"{n:032x}" for n in range(100)))
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(locks, "_windows_release_unknown", False),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(win, "_NativeFDAPI", return_value=self.crt),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
            patch("secrets.token_hex", side_effect=lambda n: next(self.counter)),
        ):
            item.start()
            self.addCleanup(item.stop)
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        self.assertEqual(snapshot.as_dict()["state_dir"].canonical, self.STATE)
        self.anchor = locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.legacy = locks.acquire_lock(self.anchor.snapshot, "lock", create=True, anchor=self.anchor)
        self.addCleanup(self.close_locks)

    def close_locks(self):
        if not self.legacy.closed:
            self.legacy.close()
        if not self.anchor.closed:
            self.anchor.close()

    def write(self, data=None):
        discovery.write_instance_record(self.STATE, "install.json", data or self.install,
                                        anchor=self.anchor, legacy=self.legacy)

    def test_old_state_path_cannot_use_new_state_locks(self):
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery.write_instance_record(
                self.ROOT + r"\.model_proxy_runtime", "install.json", self.install,
                anchor=self.anchor, legacy=self.legacy)
        self.assertEqual(caught.exception.code, "locks_unverified")
        self.assertFalse(self.fs.commit_calls)
        self.assertFalse(self.fs.write_calls)

    def test_new_and_existing_commit_refresh_immutable_held_snapshot(self):
        before = self.legacy.snapshot
        self.write()
        path = self.STATE + r"\install.json"
        first_id = self.fs.objects[path]["index"]
        self.assertNotEqual(before, self.legacy.snapshot)
        self.assertEqual(self.anchor.snapshot, self.legacy.snapshot)
        self.assertEqual(discovery.read_instance_record(self.STATE, "install.json"), self.install)
        discovery.verify_held_instance_locks(self.anchor, self.legacy)
        changed = dict(self.install, initialization_source="fake updated")
        self.write(changed)
        self.assertEqual([item[0] for item in self.fs.commit_calls], ["new", "replace"])
        self.assertNotEqual(self.fs.objects[path]["index"], first_id)
        self.assertEqual(discovery.read_instance_record(self.STATE, "install.json"), changed)
        self.assertEqual(self.anchor.snapshot, self.legacy.snapshot)
        discovery.verify_held_instance_locks(self.anchor, self.legacy)

    def assert_replace_failure(self, code):
        self.write()
        self.fs.fail_code = code
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write(dict(self.install, initialization_source="next"))
        error = caught.exception
        self.assertEqual(error.code, "record_commit_unknown")
        self.assertEqual(error.winerror, code)
        self.assertEqual(error.stage, "api_failed")
        self.assertTrue(error.temporary_created)
        self.assertTrue(error.temporary_may_contain_data)
        self.assertEqual(error.recovery.target, "missing" if code in (1176, 1177) else "original")
        self.assertEqual(error.recovery.source, "replacement_changed" if code == 1177 else "replacement")
        self.assertEqual(error.recovery.old_other_name, "unknown" if code in (1176, 1177) else "not_assumed")
        self.assertTrue(locks._windows_release_unknown)
        self.assertEqual(len(self.fs.commit_calls), 2)

    def test_replace_1175_reports_original_and_replacement(self): self.assert_replace_failure(1175)
    def test_replace_1176_reports_missing_and_replacement(self): self.assert_replace_failure(1176)
    def test_replace_1177_reports_unknown_old_and_changed_replacement(self): self.assert_replace_failure(1177)
    def test_replace_other_error_still_observes_both_names(self): self.assert_replace_failure(5)

    def test_partial_temp_write_reports_data_and_does_not_commit(self):
        def partial(handle, data):
            self.fs.handles[handle][1]["data"] = data[:3]
            raise OSError("fake partial write")
        self.fs.write_all_and_flush = partial
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_precommit_failed")
        self.assertTrue(caught.exception.temporary_created)
        self.assertTrue(caught.exception.temporary_may_contain_data)
        self.assertEqual(self.fs.commit_calls, [])
        self.assertNotIn(self.STATE + r"\install.json", self.fs.objects)
        self.assertFalse(locks._windows_release_unknown)

    def test_unsafe_temp_acl_is_rejected_before_any_record_data(self):
        self.fs.unsafe_temp_acl = True
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_precommit_failed")
        self.assertTrue(caught.exception.temporary_created)
        self.assertFalse(caught.exception.temporary_may_contain_data)
        self.assertEqual(self.fs.write_calls, [])
        self.assertEqual(self.fs.commit_calls, [])
        temps = [obj for path, obj in self.fs.objects.items() if ".record-" in path]
        self.assertEqual(len(temps), 1)
        self.assertEqual(temps[0].get("data", b""), b"")

    def test_temp_open_close_failure_is_unknown_and_not_retried(self):
        self.fs.bad_temp_close = True
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertTrue(caught.exception.temporary_created)
        self.assertIsNone(caught.exception.temporary_may_contain_data)
        failed_handle = next(iter(self.fs.close_fail))
        self.assertEqual(self.fs.close_calls.count(failed_handle), 1)
        self.assertEqual(self.fs.commit_calls, [])
        self.assertTrue(locks._windows_release_unknown)

    def test_other_fixed_record_drift_after_commit_is_not_accepted(self):
        self.fs.post_other_role = self.STATE + r"\active.json"
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_postcheck_failed")
        self.assertEqual(caught.exception.recovery.target, "replacement")
        self.assertTrue(locks._windows_release_unknown)
        self.assertEqual(len(self.fs.commit_calls), 1)

    def test_new_target_appearing_before_move_is_not_overwritten(self):
        original_move = self.fs.move_new
        target = self.STATE + r"\install.json"
        def collision(source, dest):
            self.fs.objects[dest] = dict(self.fs.obj(888), data=b"sentinel")
            original_move(source, dest)
        self.fs.move_new = collision
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_commit_unknown")
        self.assertEqual(caught.exception.winerror, 183)
        self.assertEqual(caught.exception.recovery.target, "other")
        self.assertEqual(caught.exception.recovery.source, "replacement")
        self.assertEqual(self.fs.objects[target]["data"], b"sentinel")
        self.assertEqual(len(self.fs.commit_calls), 1)

    def test_corrupt_existing_record_is_not_overwritten(self):
        target = self.STATE + r"\install.json"
        self.fs.objects[target] = dict(self.fs.obj(888), data=b"corrupt")
        fresh = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        self.anchor.snapshot = fresh
        self.legacy.snapshot = fresh
        with self.assertRaises(discovery.PreflightError):
            self.write()
        self.assertEqual(self.fs.objects[target]["data"], b"corrupt")
        self.assertEqual(self.fs.commit_calls, [])
        self.assertFalse(any(".record-" in path for path in self.fs.objects))

    def test_temp_acl_change_after_write_prevents_commit(self):
        def mutate(typ, path, count):
            if typ == "read" and ".record-" in path and count == 1:
                self.fs.objects[path]["control"] ^= 0x1000
        self.fs.on_open = mutate
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_precommit_failed")
        self.assertTrue(caught.exception.temporary_created)
        self.assertTrue(caught.exception.temporary_may_contain_data)
        self.assertEqual(self.fs.commit_calls, [])

    def test_postcommit_content_change_reports_unverified_without_rollback(self):
        self.fs.post_data_change = True
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_postcheck_failed")
        self.assertEqual(caught.exception.stage, "reported_success")
        self.assertEqual(caught.exception.recovery.target, "replacement_changed")
        self.assertEqual(caught.exception.recovery.source, "missing")
        self.assertEqual(len(self.fs.commit_calls), 1)
        self.assertTrue(locks._windows_release_unknown)

    def test_postcommit_attribute_change_is_reported_as_changed(self):
        self.write()
        self.fs.post_attribute_change = True
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write(dict(self.install, initialization_source="next"))
        self.assertEqual(caught.exception.code, "record_postcheck_failed")
        self.assertEqual(caught.exception.recovery.target, "replacement_changed")
        self.assertEqual(caught.exception.recovery.source, "missing")
        self.assertTrue(locks._windows_release_unknown)
        self.assertEqual(len(self.fs.commit_calls), 2)

    def test_postcommit_acl_change_reports_success_but_not_verified(self):
        self.write()
        self.fs.post_acl_change = True
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write(dict(self.install, initialization_source="next"))
        self.assertEqual(caught.exception.code, "record_postcheck_failed")
        self.assertEqual(caught.exception.stage, "reported_success")
        self.assertEqual(caught.exception.recovery.source, "missing")
        self.assertTrue(locks._windows_release_unknown)
        self.assertEqual(len(self.fs.commit_calls), 2)

    def test_postcommit_other_role_drift_is_not_rolled_back(self):
        self.fs.post_other_role = self.paths["log"]
        with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
            self.write()
        self.assertEqual(caught.exception.code, "record_postcheck_failed")
        self.assertEqual(caught.exception.stage, "reported_success")
        self.assertEqual(caught.exception.recovery.target, "replacement")
        self.assertEqual(len(self.fs.commit_calls), 1)
        self.assertTrue(locks._windows_release_unknown)

    def test_conflicting_temp_name_refused_before_creation_or_write(self):
        temp = self.STATE + r"\.record-00000000000000000000000000000000.tmp"
        self.fs.objects[temp] = dict(self.fs.obj(876), data=b"sentinel")
        with self.assertRaises(discovery.PreflightError):
            self.write()
        self.assertEqual(self.fs.objects[temp]["data"], b"sentinel")
        self.assertEqual(self.fs.commit_calls, [])


if __name__ == "__main__":
    unittest.main()
