"""Snapshot rollback contract in a disposable, sandboxed real-Git worktree.

Run only after the concurrent product implementation is ready:
    python3 -m unittest tests.test_macos_rollback_snapshot -v

The reused fixture copies only the three product entry points, injects fake
lock/migration paths and process listing in a child, and runs that child under
macOS sandbox-exec. All fake repositories, homes and credentials are under a
single /private/tmp temporary directory. No real service or network is used.
These tests do not prove production safety.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_macos_switch_worktree as fixture


SIDECAR_A = b'{"fake-client":{"fake-session":"fake-old-route"}}\n'
SIDECAR_B = b'{"fake-client":{"fake-session":"fake-new-route"}}\n'
THIRD_PARTY = b'{"fake-third-party":"must survive"}\n'
MCOPILOT = b'{"fake-mcopilot-credential":"must survive"}\n'


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(),
                     "requires native macOS sandbox and Git worktree")
class MacosRollbackSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A blanket refusal must never make a negative security case look green.
        control = cls("test_restore_old_snapshot_after_new_version_changes")
        result = unittest.TestResult()
        control.run(result)
        if not result.wasSuccessful():
            detail = "\n".join(text for _, text in result.failures + result.errors)
            raise AssertionError("snapshot positive control failed; negative cases not credited:\n" + detail)

    def make_fixture(self, *, sidecar_exists=True):
        # FakeWorktree uses TemporaryDirectory without an explicit dir. Override
        # only its construction so the sandbox boundary remains its single tree.
        original = tempfile.TemporaryDirectory

        def in_private_tmp(*args, **kwargs):
            kwargs["dir"] = "/private/tmp"
            return original(*args, **kwargs)

        with mock.patch.object(fixture.tempfile, "TemporaryDirectory", in_private_tmp):
            f = fixture.FakeWorktree(self)
        self.assertEqual(f.base.parent, Path("/private/tmp").resolve())
        if sidecar_exists:
            fixture.put(f.sidecar, SIDECAR_A)
        else:
            f.sidecar.unlink()
        f.third_party = f.root / "ignored/third-party-config.json"
        f.mcopilot = f.home / ".config/mcopilot/credentials.json"
        fixture.put(f.third_party, THIRD_PARTY)
        fixture.put(f.mcopilot, MCOPILOT)
        f.original_config = f.config.read_bytes()
        f.original_index = f.git("write-tree").stdout.strip()
        return f

    def change_new_version(self, f, *, atomic_sidecar=True, change_sidecar=True):
        # Keep the listen context and appkey valid; change business config.
        newer = copy.deepcopy(f.config_json)
        newer["admin_token"] = "fake-new-admin"
        fixture.put(f.config, json.dumps(newer, indent=2) + "\n")
        # A real atomic replacement, not write-in-place: inode must differ.
        if not change_sidecar:
            self.assertFalse(os.path.lexists(f.sidecar), "sidecar must remain absent")
        elif atomic_sidecar:
            replacement = f.sidecar.with_name(".fake-new-sidecar")
            fixture.put(replacement, SIDECAR_B)
            if f.sidecar.exists():
                old_inode = f.sidecar.stat().st_ino
                self.assertNotEqual(replacement.stat().st_ino, old_inode)
            else:
                old_inode = None
            os.replace(replacement, f.sidecar)
            if old_inode is not None:
                self.assertNotEqual(f.sidecar.stat().st_ino, old_inode)
        else:
            # Used solely to keep an original sidecar symlink for a retarget test.
            fixture.put(f.sidecar, SIDECAR_B)
        fixture.put(f.root / ".runtime/model_proxy_totals.json", fixture.ledger(103))
        self.assertNotEqual(f.config.read_bytes(), f.original_config)
        if change_sidecar:
            self.assertEqual(f.sidecar.read_bytes(), SIDECAR_B)
        else:
            self.assertFalse(os.path.lexists(f.sidecar), "sidecar must remain absent")

    def assert_unrelated_preserved(self, f, *, expected_root_head):
        self.assertEqual(f.third_party.read_bytes(), THIRD_PARTY)
        self.assertEqual(f.mcopilot.read_bytes(), MCOPILOT)
        self.assertEqual(f.key.read_bytes(), b"fake-key: fake-value\n")
        self.assertEqual((f.root / "docs/worktrees.md").read_bytes(),
                         b"user edit outside changed paths\n")
        self.assertEqual((f.worktree / ".git").read_bytes(), f.git_file)
        self.assertEqual((f.root / ".git").stat().st_ino, f.git_identity)

        def worktrees(text):
            result = {}
            for block in text.strip().split("\n\n"):
                fields = {}
                for line in block.splitlines():
                    key, _, value = line.partition(" ")
                    self.assertNotIn(key, fields, "duplicate worktree field")
                    fields[key] = value
                path = fields.pop("worktree")
                self.assertNotIn(path, result, "duplicate worktree path")
                result[path] = fields
            return result

        before = worktrees(f.worktree_list)
        current = worktrees(f.git("worktree", "list", "--porcelain").stdout)
        self.assertEqual(set(current), set(before), "worktree set or paths changed")
        for path in before:
            self.assertEqual({k: v for k, v in current[path].items() if k != "HEAD"},
                             {k: v for k, v in before[path].items() if k != "HEAD"},
                             f"worktree branch or metadata changed: {path}")
        self.assertEqual(current[str(f.root)]["HEAD"], expected_root_head)
        self.assertEqual(current[str(f.worktree)]["HEAD"], f.target)
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), expected_root_head)
        self.assertEqual(f.git("rev-parse", "HEAD", cwd=f.worktree).stdout.strip(), f.target)
        self.assertFalse(f.audit.exists(), "signal/launch helper invoked")

    def assert_blocked(self, p):
        output = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, "unsafe rollback reported success\n" + fixture.diagnostic(p))
        self.assertTrue(any(word in output.upper() for word in
                            ("UNKNOWN", "阻断", "拒绝", "已停止", "失败")),
                        "must report blocking or UNKNOWN\n" + fixture.diagnostic(p))

    def assert_restored(self, f, *, sidecar_exists):
        self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), fixture.ledger(100))
        self.assertEqual((f.root / ".model_proxy.log").read_bytes(), b"fake old log must remain\n")
        self.assertEqual((f.root / fixture.COLLISION).read_bytes(), fixture.A)
        self.assertEqual(f.git("write-tree").stdout.strip(), f.original_index)
        self.assertEqual((f.root / "code/intersect.txt").read_bytes(), b"old code\n")
        self.assertEqual(f.config.read_bytes(), f.original_config)
        if sidecar_exists:
            self.assertEqual(f.sidecar.read_bytes(), SIDECAR_A)
        else:
            self.assertFalse(os.path.lexists(f.sidecar), "originally absent sidecar must be absent")
        self.assertEqual(stat.S_IMODE((f.root / fixture.COLLISION).stat().st_mode), 0o640)
        self.assert_unrelated_preserved(f, expected_root_head=f.old_oid)

    def test_restore_old_snapshot_after_new_version_changes(self):
        f = self.make_fixture()
        _, tx = f.successful_switch()
        self.change_new_version(f)
        p = f.command("rollback", tx.name)
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assert_restored(f, sidecar_exists=True)

    def test_restore_originally_absent_sidecar(self):
        f = self.make_fixture(sidecar_exists=False)
        _, tx = f.successful_switch()
        self.change_new_version(f, change_sidecar=False)
        p = f.command("rollback", tx.name)
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assert_restored(f, sidecar_exists=False)

    def test_originally_absent_unknown_sidecar_must_be_preserved(self):
        for kind in ("regular", "symlink"):
            with self.subTest(kind=kind):
                f = self.make_fixture(sidecar_exists=False)
                _, tx = f.successful_switch()
                self.change_new_version(f, change_sidecar=False)
                target = f.sidecar if kind == "regular" else f.base / "unknown-sidecar-target.json"
                # Legal sidecar JSON and mode 0600 alone do not establish ownership.
                fixture.put(target, SIDECAR_B)
                if kind == "symlink":
                    f.sidecar.symlink_to(target)
                path_before = f.sidecar.lstat()
                target_before = target.stat()
                self.assertEqual(target.read_bytes(), SIDECAR_B)

                p = f.command("rollback", tx.name)
                self.assert_blocked(p)
                self.assertTrue(os.path.lexists(f.sidecar), "unknown sidecar must not be deleted")
                path_after = f.sidecar.lstat()
                target_after = target.stat()
                for before, after in ((path_before, path_after), (target_before, target_after)):
                    self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                                     (before.st_ino, before.st_mode, before.st_mtime_ns))
                self.assertEqual(target.read_bytes(), SIDECAR_B)
                if kind == "symlink":
                    self.assertTrue(f.sidecar.is_symlink())
                    self.assertEqual(os.readlink(f.sidecar), str(target))
                else:
                    self.assertTrue(stat.S_ISREG(path_after.st_mode))
                self.assert_unrelated_preserved(f, expected_root_head=f.target)

    def test_retargeted_leaf_symlinks_never_overwrite_unrelated_files(self):
        for which in ("config", "sidecar"):
            with self.subTest(which=which):
                f = self.make_fixture()
                path = f.config if which == "config" else f.sidecar
                _, tx = f.successful_switch()
                self.change_new_version(f)
                unrelated = f.base / ("unrelated-" + which + ".json")
                # Both remain valid JSON, so a refusal cannot be attributed to
                # malformed input rather than the changed destination.
                fixture.put(unrelated, path.read_bytes())
                before = unrelated.read_bytes()
                before_stat = unrelated.stat()
                link = path.with_name(".fake-retarget-link")
                link.symlink_to(unrelated)
                os.replace(link, path)
                p = f.command("rollback", tx.name)
                self.assert_blocked(p)
                self.assertTrue(path.is_symlink())
                self.assertEqual(os.readlink(path), str(unrelated))
                self.assertEqual(unrelated.read_bytes(), before)
                after_stat = unrelated.stat()
                self.assertEqual((after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_mode),
                                 (before_stat.st_ino, before_stat.st_mtime_ns, before_stat.st_mode))
                self.assert_unrelated_preserved(f, expected_root_head=f.target)

    def test_initial_symlinks_rejected_before_switch_or_snapshot(self):
        for case in ("config", "sidecar", "parent"):
            with self.subTest(case=case):
                f = self.make_fixture()
                foreign = f.base / "initial-fake-third-party"
                foreign.mkdir(mode=0o700)
                f.no_follow_root = foreign
                if case == "parent":
                    runtime_paths = (f.root / "config/runtime_paths.json").read_bytes()
                    (f.root / "config").rename(f.root / "fake-saved-config")
                    payloads = {
                        foreign / "model_proxy_config.json": f.original_config,
                        foreign / "session_overrides.json": SIDECAR_A,
                        foreign / "runtime_paths.json": runtime_paths,
                    }
                    link = f.root / "config"
                    destination = foreign
                else:
                    link = f.config if case == "config" else f.sidecar
                    payloads = {foreign / "unknown.json":
                                f.original_config if case == "config" else SIDECAR_A}
                    destination = next(iter(payloads))
                    link.unlink()
                for path, content in payloads.items():
                    fixture.put(path, content)
                link.symlink_to(destination, target_is_directory=(case == "parent"))
                link_before = link.lstat()
                target_before = {path: path.stat() for path in payloads}

                p = f.command("switch")
                self.assert_blocked(p)
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), str(destination))
                link_after = link.lstat()
                self.assertEqual((link_after.st_ino, link_after.st_mode, link_after.st_mtime_ns),
                                 (link_before.st_ino, link_before.st_mode, link_before.st_mtime_ns))
                for path, content in payloads.items():
                    before, after = target_before[path], path.stat()
                    self.assertEqual(path.read_bytes(), content)
                    self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                                     (before.st_ino, before.st_mode, before.st_mtime_ns))
                self.assertFalse(any(path.is_dir() for path in f.migration.iterdir()),
                                 "initial symlink must not produce a transaction snapshot")
                self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), fixture.ledger(100))
                self.assertEqual((f.root / fixture.COLLISION).read_bytes(), fixture.A)
                self.assertFalse((f.root / ".runtime").exists())
                # The fixture's guarded helpers record attempted access to the
                # foreign subtree in audit; a mere fixture-induced refusal fails.
                self.assert_unrelated_preserved(f, expected_root_head=f.old_oid)

    def test_retargeted_config_parent_never_overwrites_unrelated_files(self):
        f = self.make_fixture()
        _, tx = f.successful_switch()
        self.change_new_version(f)
        original_dir = f.root / "config-original-fake"
        unrelated_dir = f.base / "unrelated-config-dir"
        unrelated_config = unrelated_dir / f.config.name
        unrelated_sidecar = unrelated_dir / f.sidecar.name
        fixture.put(unrelated_config, f.config.read_bytes())
        fixture.put(unrelated_sidecar, f.sidecar.read_bytes())
        config_before, sidecar_before = unrelated_config.read_bytes(), unrelated_sidecar.read_bytes()
        config_stat, sidecar_stat = unrelated_config.stat(), unrelated_sidecar.stat()
        (f.root / "config").rename(original_dir)
        (f.root / "config").symlink_to(unrelated_dir, target_is_directory=True)
        p = f.command("rollback", tx.name)
        self.assert_blocked(p)
        self.assertEqual(unrelated_config.read_bytes(), config_before)
        self.assertEqual(unrelated_sidecar.read_bytes(), sidecar_before)
        for path, before in ((unrelated_config, config_stat), (unrelated_sidecar, sidecar_stat)):
            after = path.stat()
            self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_mode),
                             (before.st_ino, before.st_mtime_ns, before.st_mode))
        self.assertTrue((f.root / "config").is_symlink())
        self.assertEqual((original_dir / f.config.name).read_bytes(), config_before)
        self.assertEqual((original_dir / f.sidecar.name).read_bytes(), sidecar_before)
        self.assert_unrelated_preserved(f, expected_root_head=f.target)

    def test_missing_old_snapshot_cannot_report_success(self):
        # The injection assumes each snapshot has a private file containing
        # exact old bytes. If implementation stores it differently, fail with
        # an explicit seam mismatch; do not weaken the rollback assertions.
        for which in ("config", "sidecar", "ledger"):
            with self.subTest(which=which):
                f = self.make_fixture()
                _, tx = f.successful_switch()
                self.change_new_version(f)
                old = {"config": f.original_config, "sidecar": SIDECAR_A,
                       "ledger": fixture.ledger(100)}[which]
                snapshots = [p for p in tx.rglob("*")
                             if p.is_file() and not p.is_symlink()
                             and p.name != "manifest.json" and p.read_bytes() == old]
                self.assertTrue(snapshots, f"{which} snapshot injection seam differs: "
                                          "no private file with exact original bytes")
                for snapshot in snapshots:
                    snapshot.unlink()
                p = f.command("rollback", tx.name)
                self.assert_blocked(p)
                self.assert_unrelated_preserved(f, expected_root_head=f.target)

    def test_missing_transaction_record_cannot_report_success(self):
        f = self.make_fixture()
        _, tx = f.successful_switch()
        self.change_new_version(f)
        (tx / "manifest.json").unlink()
        p = f.command("rollback", tx.name)
        self.assert_blocked(p)
        self.assert_unrelated_preserved(f, expected_root_head=f.target)


if __name__ == "__main__":
    unittest.main()
