"""Read-only identity-preflight tests using only an isolated fixture tree."""

import os
from dataclasses import replace
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from core.discovery import PreflightError, _identity_contains, _identity_same, inspect_runtime_identities
from core.runtime_paths import PATH_KEYS


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "project"
        self.runtime = Path(self.tmp.name).resolve() / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()
        self.paths = {key: self.runtime / key for key in PATH_KEYS}

    def test_normal_preflight_is_read_only_and_does_not_create_anchor(self):
        self.paths["log"].write_text("fake log", encoding="utf-8")
        with patch("os.mkdir", side_effect=AssertionError("mkdir forbidden")), \
             patch("os.open", side_effect=AssertionError("open forbidden")), \
             patch("builtins.open", side_effect=AssertionError("open forbidden")):
            result = inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(self.paths["log"].read_text(encoding="utf-8"), "fake log")
        self.assertFalse((self.root / ".runtime").exists())
        self.assertTrue(result.as_dict()["log"].exists)
        self.assertFalse(result.as_dict()["anchor.lock"].exists)

    def test_identical_role_paths_are_rejected_before_any_data_writes(self):
        self.paths["totals"] = self.paths["log"]
        self.paths["log"].write_text("do not change", encoding="utf-8")
        with self.assertRaises(PreflightError):
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(self.paths["log"].read_text(encoding="utf-8"), "do not change")
        self.assertFalse((self.root / ".runtime").exists())

    def test_two_names_for_one_hardlink_are_rejected(self):
        self.paths["log"].write_text("fake", encoding="utf-8")
        os.link(self.paths["log"], self.paths["totals"])
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "same_identity")

    def test_final_symlink_to_another_role_is_rejected(self):
        self.paths["totals"].write_text("fake", encoding="utf-8")
        self.paths["log"].symlink_to(self.paths["totals"])
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "same_identity")

    def test_dangling_symlink_is_unknown_not_a_new_file(self):
        self.paths["log"].symlink_to(self.runtime / "missing")
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "dangling_link")

    def test_runtime_target_cannot_alias_an_absent_anchor(self):
        self.paths["totals"] = self.root / ".runtime" / "anchor.lock"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "same_identity")
        self.assertFalse((self.root / ".runtime").exists())

    def test_parent_symlink_cannot_hide_fixed_file_collision(self):
        state = self.root / ".runtime"
        state.mkdir()
        alias = self.runtime / "state-alias"
        alias.symlink_to(state, target_is_directory=True)
        self.paths["log"] = alias / "anchor.lock"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "same_identity")

    def test_fixed_directory_and_fixed_files_cannot_be_symlinks(self):
        other = self.runtime / "state"
        other.mkdir()
        state = self.root / ".runtime"
        state.symlink_to(other, target_is_directory=True)
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "fixed_link")
        state.unlink()
        state.mkdir()
        (other / "lock").write_text("fake", encoding="utf-8")
        (state / "anchor.lock").symlink_to(other / "lock")
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "fixed_link")

    def test_fixed_file_with_multiple_hardlinks_is_rejected(self):
        state = self.root / ".runtime"
        state.mkdir()
        anchor = state / "anchor.lock"
        anchor.write_text("fake", encoding="utf-8")
        os.link(anchor, self.runtime / "another-name")
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "fixed_hardlink")

    def test_missing_path_with_dotdot_is_unknown(self):
        self.paths["log"] = self.runtime / "missing" / ".." / "log"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "unknown_path")

    def test_dotdot_resolving_to_a_directory_cannot_be_a_log_file(self):
        (self.runtime / "existing").mkdir()
        self.paths["log"] = self.runtime / "existing" / ".."
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "invalid_type")

    def test_parent_identity_changes_are_visible_in_snapshots(self):
        first = self.runtime / "first"
        second = self.runtime / "second"
        first.mkdir()
        second.mkdir()
        alias = self.runtime / "parent"
        alias.symlink_to(first, target_is_directory=True)
        self.paths["log"] = alias / "log"
        before = inspect_runtime_identities(self.root, self.paths)
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        after = inspect_runtime_identities(self.root, self.paths)
        self.assertNotEqual(before.as_dict()["log"], after.as_dict()["log"])

    def test_denied_metadata_is_unknown(self):
        original_lstat = os.lstat

        def deny_target(path, *args, **kwargs):
            if Path(path) == self.paths["log"]:
                raise PermissionError("synthetic denial")
            return original_lstat(path, *args, **kwargs)

        with patch("os.lstat", side_effect=deny_target):
            with self.assertRaises(PreflightError) as caught:
                inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "unknown_path")

    def test_unknown_inode_refuses_preflight_before_writes(self):
        original_lstat = os.lstat

        def unknown_inode(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if Path(path) == self.runtime:
                fields = list(info)
                fields[1] = 0
                return os.stat_result(fields)
            return info

        with patch("os.lstat", side_effect=unknown_inode), patch("os.open") as opened:
            with self.assertRaises(PreflightError) as caught:
                inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "unknown_path")
        opened.assert_not_called()
        self.assertFalse((self.root / ".runtime").exists())

    def test_absent_state_case_alias_rejected_before_anchor_creation(self):
        self.paths["totals"] = self.root / ".RUNTIME" / "anchor.lock"
        with patch("os.open") as opened, patch("os.mkdir") as created:
            with self.assertRaises(PreflightError) as caught:
                inspect_runtime_identities(self.root, self.paths)
        self.assertIn(caught.exception.code, ("state_conflict", "same_identity"))
        opened.assert_not_called()
        created.assert_not_called()
        self.assertFalse((self.root / ".runtime").exists())
        self.assertFalse((self.root / ".RUNTIME").exists())

    def test_existing_state_case_alias_rejected_before_anchor_creation(self):
        (self.root / ".runtime").mkdir(mode=0o700)
        self.paths["log"] = self.root / ".RUNTIME" / "anchor.lock"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertIn(caught.exception.code, ("state_conflict", "same_identity"))
        self.assertFalse((self.root / ".runtime" / "anchor.lock").exists())

    def test_absent_case_alias_through_parent_symlink_is_rejected(self):
        alias = self.runtime / "project-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.paths["log"] = alias / ".RUNTIME" / "ANCHOR.LOCK"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertIn(caught.exception.code, ("state_conflict", "same_identity"))
        self.assertFalse((self.root / ".runtime").exists())

    def test_absent_role_paths_with_case_alias_are_rejected(self):
        self.paths["log"] = self.runtime / "ACCESS.LOG"
        self.paths["totals"] = self.runtime / "access.log"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "same_identity")
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())

    def test_absent_unicode_normalization_alias_is_rejected(self):
        self.paths["log"] = self.runtime / "CAFÉ.log"
        self.paths["totals"] = self.runtime / "cafe\u0301.LOG"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "same_identity")
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())

    def test_unknown_unicode_equivalence_fails_closed(self):
        # No platform adapter proves whether the missing Unicode name differs
        # from the reserved state directory, so the answer is UNKNOWN/closed.
        self.paths["log"] = self.root / "配置" / "log"
        with self.assertRaises(PreflightError) as caught:
            inspect_runtime_identities(self.root, self.paths)
        self.assertEqual(caught.exception.code, "state_conflict")
        self.assertFalse((self.root / ".runtime").exists())

    def test_shared_existing_ancestor_alias_with_missing_suffix_is_detected(self):
        # Pure synthetic paths: no stat/open on either of these strings.
        original = inspect_runtime_identities(self.root, self.paths).as_dict()["log"]
        left = replace(original, canonical="/fake-a/AREA/Result.LOG",
                       ancestor_device=123, ancestor_inode=456,
                       unresolved=("AREA", "Result.LOG"))
        right = replace(original, canonical="/fake-b/area/result.log",
                        ancestor_device=123, ancestor_inode=456,
                        unresolved=("area", "result.log"))
        parent = replace(left, canonical="/fake-a/AREA", unresolved=("AREA",))
        self.assertTrue(_identity_same(left, right))
        self.assertTrue(_identity_contains(parent, right))


class InstanceObservationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin protections")
    def test_record_directory_checks_project_and_state_twice(self):
        import core.discovery as discovery
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "project"
            root.mkdir(mode=0o700)
            state = root / ".runtime"
            state.mkdir(mode=0o700)
            with patch.object(discovery, "_verify_directory",
                              wraps=discovery._verify_directory) as verify:
                discovery.verify_record_directory(state)
            self.assertEqual([call.args[0] for call in verify.call_args_list],
                             [root, state, root, state])

    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin protections")
    def test_project_parent_allows_public_read_but_not_sticky_public_write(self):
        import core.discovery as discovery
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "project"
            root.mkdir(mode=0o700)
            state = root / ".runtime"
            state.mkdir(mode=0o700)
            try:
                root.chmod(0o755)
                discovery.verify_record_directory(state)
                root.chmod(0o1777)
                with self.assertRaises(PreflightError) as caught:
                    discovery.verify_record_directory(state)
                self.assertEqual(caught.exception.code, "protection_unavailable")
            finally:
                root.chmod(0o700)

    def test_native_darwin_process_start_identity_is_queryable(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin-only OS query")
        from core.discovery import process_start_identity
        first = process_start_identity(os.getpid())
        self.assertIsNotNone(first)
        self.assertEqual(first["kind"], "darwin_proc_bsdinfo")
        self.assertEqual(first, process_start_identity(os.getpid()))
        from core.discovery import process_executable
        image = process_executable(os.getpid())
        self.assertIsNotNone(image)
        self.assertTrue(os.path.isabs(image))
        self.assertEqual(image, process_executable(os.getpid()))

    def make_state(self, directory):
        from contextlib import contextmanager
        from core.discovery import verify_record_directory
        from core.platform_lock import acquire_lock, verify_local_access
        @contextmanager
        def fixture():
            root = Path(directory) / "project"
            root.mkdir(mode=0o700)
            state = root / ".runtime"
            state.mkdir(mode=0o700)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            paths = {key: runtime / key for key in PATH_KEYS}
            snapshot = inspect_runtime_identities(root, paths)
            with acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=verify_local_access) as anchor:
                with acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor, access_verifier=verify_local_access) as legacy:
                    device, inode = verify_record_directory(state)
                    install = {"schema": 2, "record": "install", "install_id": "a" * 32,
                               "project_root": str(root), "root_identity": {"device": device, "inode": inode},
                               "initialization_source": "fake fixture", "ever_started": False,
                               "last_instance_nonce": None}
                    yield state, install, anchor, legacy
        return fixture()

    def test_read_record_and_inconsistent_set_are_unknown(self):
        from core.discovery import read_instance_record, read_instance_set, write_instance_record
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, install, anchor, legacy):
            self.assertIsNone(read_instance_record(state, "active.json"))
            self.assertIsNone(read_instance_set(state))
            write_instance_record(state, "install.json", install, anchor=anchor, legacy=legacy)
            self.assertEqual(read_instance_set(state)["install"], install)
            install["ever_started"] = True
            install["last_instance_nonce"] = "b" * 32
            write_instance_record(state, "install.json", install, anchor=anchor, legacy=legacy)
            with self.assertRaises(PreflightError):
                read_instance_set(state)

    def test_disagreeing_active_and_last_good_is_unknown(self):
        from core.discovery import (read_instance_set, write_instance_record,
                                    process_start_identity, process_executable)
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, install, anchor, legacy):
            paths = {key: legacy.snapshot.as_dict()[key].canonical for key in PATH_KEYS}
            locks = {}
            for name, held in (("anchor", anchor), ("legacy", legacy)):
                info = os.fstat(held.fileno())
                locks[name] = {"device": info.st_dev, "inode": info.st_ino}
            common = {"schema": 2, "install_id": install["install_id"],
                      "project_root": install["project_root"], "instance_nonce": "b" * 32,
                      "listen": {"host": "127.0.0.1", "port": 12345}, "runtime_paths": paths}
            active = dict(common, record="active", state="READY", pid=os.getpid(),
                          process_start=process_start_identity(os.getpid()),
                          executable=process_executable(os.getpid()), locks=locks)
            last = dict(common, record="last_good_paths", instance_nonce="c" * 32)
            install.update(ever_started=True, last_instance_nonce="b" * 32)
            for name, value in (("install.json", install), ("active.json", active), ("last_good_paths.json", last)):
                write_instance_record(state, name, value, anchor=anchor, legacy=legacy)
            with self.assertRaises(PreflightError) as caught:
                read_instance_set(state)
            self.assertEqual(caught.exception.code, "records_inconsistent")

    def test_unknown_acl_or_old_schema_never_becomes_trusted(self):
        from core.discovery import read_instance_set
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, install, anchor, legacy):
            with patch("core.discovery.verify_no_extended_acl", side_effect=PreflightError("protection_unavailable", "fake")):
                with self.assertRaises(PreflightError):
                    read_instance_set(state)
            target = state / "install.json"
            target.write_text('{"schema":1,"record":"install"}')
            target.chmod(0o600)
            with self.assertRaises(PreflightError):
                read_instance_set(state)

    def test_writer_requires_both_live_instance_locks(self):
        from core.discovery import write_instance_record
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, record, anchor, legacy):
            with self.assertRaises(PreflightError):
                write_instance_record(state, "install.json", record, anchor=anchor, legacy=None)
            self.assertFalse((state / "install.json").exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin protections")
    def test_held_lock_direct_parent_must_not_be_writable_by_others(self):
        from core.discovery import verify_held_instance_locks, write_instance_record
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, record, anchor, legacy):
            parent = Path(legacy.snapshot.as_dict()["lock"].canonical).parent
            try:
                parent.chmod(0o755)
                verify_held_instance_locks(anchor, legacy)
                parent.chmod(0o1777)
                with self.assertRaises(PreflightError):
                    write_instance_record(state, "install.json", record, anchor=anchor, legacy=legacy)
                self.assertFalse((state / "install.json").exists())
            finally:
                parent.chmod(0o700)

    def test_record_postcheck_failure_reports_replacement(self):
        import core.discovery as discovery
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, record, anchor, legacy):
            original = discovery._check_record_target
            calls = 0
            def fail_postcheck(path):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise PreflightError("fake", "fake")
                return original(path)
            with patch.object(discovery, "_check_record_target", side_effect=fail_postcheck):
                with self.assertRaises(PreflightError) as caught:
                    discovery.write_instance_record(state, "install.json", record, anchor=anchor, legacy=legacy)
            self.assertEqual(caught.exception.code, "record_postcheck_failed")
            self.assertTrue((state / "install.json").exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin protections")
    def test_postcommit_project_or_state_protection_change_is_reported(self):
        import core.discovery as discovery
        for target_role in ("project", "state"):
            with self.subTest(target=target_role), tempfile.TemporaryDirectory() as directory:
                with self.make_state(directory) as (state, record, anchor, legacy):
                    target = state.parent if target_role == "project" else state
                    original_mode = target.stat().st_mode & 0o777
                    real_replace = os.replace
                    def replace_and_change(*args, **kwargs):
                        value = real_replace(*args, **kwargs)
                        target.chmod(0o750)
                        return value
                    try:
                        with patch.object(discovery.os, "replace", side_effect=replace_and_change):
                            with self.assertRaises(PreflightError) as caught:
                                discovery.write_instance_record(
                                    state, "install.json", record, anchor=anchor, legacy=legacy)
                        self.assertEqual(caught.exception.code, "record_postcheck_failed")
                        self.assertTrue((state / "install.json").exists())
                    finally:
                        target.chmod(original_mode)

    def test_instance_record_rejects_symlink_and_invalid_old_content(self):
        from core.discovery import write_instance_record
        with tempfile.TemporaryDirectory() as directory, self.make_state(directory) as (state, install, anchor, legacy):
            source = Path(directory) / "sentinel"
            source.write_text("sentinel")
            target = state / "install.json"
            target.symlink_to(source)
            with self.assertRaises(PreflightError):
                write_instance_record(state, "install.json", install, anchor=anchor, legacy=legacy)
            self.assertEqual(source.read_text(), "sentinel")
            target.unlink()
            target.write_text("broken")
            target.chmod(0o600)
            with self.assertRaises(PreflightError):
                write_instance_record(state, "install.json", install, anchor=anchor, legacy=legacy)
            self.assertEqual(target.read_text(), "broken")


if __name__ == "__main__":
    unittest.main()
