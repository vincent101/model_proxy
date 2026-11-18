"""Offline fake-worktree tests for Git-before-ledger migration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_macos_switch_worktree as fixture

TOKEN = "FAKE_CLIENT_TOKEN_runtime_safety_7e9d"
ADMIN = "FAKE_ADMIN_CONFIG_runtime_safety_48cd"
KEY = "FAKE_APPKEY_runtime_safety_a73c"
BACKUP = "FAKE_CONFIG_BACKUP_runtime_safety_e52d"

# The fixture child runs inside a network-denying macOS sandbox. Both process
# listing and port probes are synthetic, and no listener is created.
DRIVER = fixture.DRIVER.replace(
    'assert command[2] == "-iTCP:" + str(cfg["port"]) and cfg["port"] != 3586',
    'assert command[2] == "-iTCP:" + str(cfg["port"]) and cfg["port"] != 3586\n'
    '        return subprocess.CompletedProcess(command, 1, b"", b"")',
).replace(
    'elif point == "ledger.after_apply_before_done":',
    '''elif point in ("ledger.after_stage", "git.after_advance", "git.after_apply_before_done",
                     "rollback.after_apply_before_done", "runtime-dir.after_apply_before_done"):
    original_done = m.Manifest.done
    def done(self, action, *args, **kwargs):
        if ((point == "rollback.after_apply_before_done" and action == "GIT_RETURN") or
                (point == "git.after_apply_before_done" and action == "GIT_ADVANCE") or
                (point == "runtime-dir.after_apply_before_done" and action == "PARK_RUNTIME_DIRECTORY")):
            rendezvous()
        result = original_done(self, action, *args, **kwargs)
        if ((point == "ledger.after_stage" and action == "STAGE_NEW_LEDGER") or
                (point == "git.after_advance" and action == "GIT_ADVANCE")):
            rendezvous()
        return result
    m.Manifest.done = done
elif point == "runtime-dir.before_commit":
    original_intent = m.Manifest.intent
    def intent(self, action, evidence):
        result = original_intent(self, action, evidence)
        if action == "PARK_RUNTIME_DIRECTORY":
            rendezvous()
        return result
    m.Manifest.intent = intent
elif point == "runtime-dir.cross_device":
    source = Path(cfg["base"]) / "project/.runtime/backups"
    expected_inode = source.lstat().st_ino
    real_fstat = os.fstat
    def fake_fstat(fd):
        st = real_fstat(fd)
        if st.st_ino == expected_inode:
            values = list(st)
            values[2] = st.st_dev + 1000
            return os.stat_result(values)
        return st
    os.fstat = fake_fstat
elif point == "runtime-dir.no_contents_read":
    root = Path(cfg["base"]) / "project/.runtime"
    protected = {(root / name).lstat().st_ino for name in ("backups", "tmp")}
    real_fstat, real_open, real_scandir, real_listdir = os.fstat, os.open, os.scandir, os.listdir
    def guard_fd(fd):
        if isinstance(fd, int) and real_fstat(fd).st_ino in protected:
            raise m.SafetyError("fixture blocked access to directory contents")
    def guarded_open(path, *args, **kwargs):
        guard_fd(kwargs.get("dir_fd"))
        if not isinstance(path, int):
            p = Path(path)
            if any((root / name) in p.parents for name in ("backups", "tmp")):
                raise m.SafetyError("fixture blocked child-file access")
        return real_open(path, *args, **kwargs)
    def guarded_scandir(path):
        guard_fd(path)
        if not isinstance(path, int) and Path(path) in (root / "backups", root / "tmp"):
            raise m.SafetyError("fixture blocked directory enumeration")
        return real_scandir(path)
    def guarded_listdir(path):
        guard_fd(path)
        if not isinstance(path, int) and Path(path) in (root / "backups", root / "tmp"):
            raise m.SafetyError("fixture blocked directory enumeration")
        return real_listdir(path)
    os.open, os.scandir, os.listdir = guarded_open, guarded_scandir, guarded_listdir
elif point == "git.partial_before_ref":
    original_git = m._git
    def git(root, *args, **kwargs):
        if args[:1] == ("merge",):
            # Simulate one interrupted Git worktree/index update while HEAD is old.
            original_git(root, "checkout", args[-1], "--", ".gitignore")
            raise InterruptedError("fake partial Git update")
        return original_git(root, *args, **kwargs)
    m._git = git
elif point in ("ignore.delete_after_check", "ignore.change_after_check"):
    original_verify = m._verify_target_runtime_ignore
    changed = False
    def verify(tx, runtime):
        global changed
        result = original_verify(tx, runtime)
        if not changed:
            changed = True
            sentinel = runtime / ".gitignore"
            if point == "ignore.delete_after_check":
                sentinel.unlink()
            else:
                sentinel.write_bytes(b"!*\\n")
        return result
    m._verify_target_runtime_ignore = verify
elif point == "ignore.git_failure":
    original_git = m._git
    def git(root, *args, **kwargs):
        if args[:1] == ("check-ignore",) and ".runtime/" in args:
            raise m.SafetyError("fake check-ignore failure")
        return original_git(root, *args, **kwargs)
    m._git = git
elif point == "ledger.after_apply_before_done":''',
)


class NoSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bind(self, address):
        assert address == ("127.0.0.1", 0)

    def getsockname(self):
        return ("127.0.0.1", 54321)


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(),
                     "requires native macOS sandbox and Git")
class RuntimeIgnoreTest(unittest.TestCase):
    def make_fixture(self, *, target_ignores=True, legacy_ledger_ignored=True,
                     target_legacy_ignored=True):
        original_put = fixture.put

        def build_ignore(path, data, mode=0o600):
            path = Path(path)
            if path.name == ".gitignore" and isinstance(data, str):
                data = (data.replace(".runtime/\n", "") +
                        "config/fake-appkey.yaml\nconfig/model_proxy_config.json.bak.*\n")
                if not legacy_ledger_ignored:
                    data = data.replace(".model_proxy_totals.json\n", "")
                elif legacy_ledger_ignored == "negated":
                    data += "!.model_proxy_totals.json\n"
            result = original_put(path, data, mode)
            if (path.name == "runtime_paths.json"
                    and ".claude/worktrees/" in str(path)):
                ignore = path.parent.parent / ".gitignore"
                existing = ignore.read_bytes()
                if not target_legacy_ignored:
                    existing = existing.replace(b".model_proxy_totals.json\n", b"")
                suffix = b".runtime/\n" if target_ignores else b""
                if target_ignores == "negated":
                    suffix += b"!.runtime/\n"
                original_put(ignore, existing + suffix)
            return result

        with mock.patch.object(fixture, "put", build_ignore), \
             mock.patch.object(fixture, "DRIVER", DRIVER), \
             mock.patch.object(fixture.socket, "socket", return_value=NoSocket()):
            f = fixture.FakeWorktree(self)
        self.addCleanup(lambda: self.assertFalse(f.audit.exists(), "signal or launch helper invoked"))
        self.assertNotIn(".runtime/", (f.root / ".gitignore").read_text())
        self.assertEqual(".runtime/" in (f.worktree / ".gitignore").read_text(), bool(target_ignores))
        content = json.loads(fixture.ledger(100))
        content["client_token"] = TOKEN
        self.old_ledger = (json.dumps(content) + "\n").encode()
        fixture.put(f.root / ".model_proxy_totals.json", self.old_ledger)
        f.key = f.root / "config/fake-appkey.yaml"
        fixture.put(f.key, KEY + "\n")
        fixture.put(f.root / "config/model_proxy_config.json.bak.fake", BACKUP + "\n")
        f.config_json["admin_token"] = ADMIN
        supply = dict(f.config_json["supplies"][0], appkey_file=str(f.key))
        f.config_json["supplies"] = [dict(supply, id="fake-" + str(i)) for i in range(6)]
        f.save_config()
        self.config, self.sidecar = f.config.read_bytes(), f.sidecar.read_bytes()
        return f

    def tx(self, f):
        return next(p for p in f.migration.iterdir() if p.is_dir())

    def assert_no_secrets_staged(self, f):
        f.git("add", "-A")
        for marker in (TOKEN, ADMIN, KEY, BACKUP):
            p = subprocess.run(
                ["/usr/bin/git", "-C", str(f.root), "grep", "--cached", "-I", "-F", "-q",
                 "-e", marker], env=f.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 1, "sensitive fake marker in index or Git error")
        paths = f.git("ls-files", "-z").stdout.split("\0")
        self.assertNotIn(".runtime/model_proxy_totals.json", paths)
        self.assertFalse(any(p.startswith(".runtime/.ledger-") for p in paths))

    def assert_snapshot_and_original(self, f, tx):
        self.assertEqual((tx / "ledger.snapshot").read_bytes(), self.old_ledger)
        self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)
        self.assertEqual(f.config.read_bytes(), self.config)
        self.assertEqual(f.sidecar.read_bytes(), self.sidecar)
        self.assertEqual((tx / "conflict.backup").read_bytes(), fixture.A)
        self.assertEqual((f.root / "docs/worktrees.md").read_bytes(), b"user edit outside changed paths\n")
        self.assertEqual((f.worktree / ".git").read_bytes(), f.git_file)
        self.assertEqual(f.git("rev-parse", "HEAD", cwd=f.worktree).stdout.strip(), f.target)

    def tamper(self, f, action):
        sentinel = f.root / ".runtime/.gitignore"
        if action == "delete":
            sentinel.unlink()
        else:
            sentinel.write_text("!*\n")

    def test_before_git_advance_no_runtime_ledger_even_if_sentinel_tampered(self):
        for action in ("delete", "change"):
            with self.subTest(action=action):
                f = self.make_fixture()
                with f.paused("switch", "merge.pre_apply", abort=True) as process:
                    self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
                    self.assertFalse((f.root / ".runtime/model_proxy_totals.json").exists())
                    self.assertFalse(list((f.root / ".runtime").glob(".ledger-*.tmp")))
                    self.tamper(f, action)
                self.assertNotEqual(process.returncode, 0, str(process.test_output))
                tx = self.tx(f)
                self.assert_snapshot_and_original(f, tx)
                self.assert_no_secrets_staged(f)
                recovery = f.command("rollback", tx.name, recover=True)
                self.assertNotEqual(recovery.returncode, 0, fixture.diagnostic(recovery))
                self.assertIn("UNKNOWN", (recovery.stdout + recovery.stderr).upper())

    def test_interrupt_after_advance_stage_and_install_survives_sentinel_tampering(self):
        for point in ("git.after_advance", "git.after_apply_before_done",
                      "ledger.after_stage", "ledger.after_apply_before_done"):
            for action in ("delete", "change"):
                with self.subTest(point=point, action=action):
                    f = self.make_fixture()
                    with f.paused("switch", point, abort=True) as process:
                        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)
                    self.assertNotEqual(process.returncode, 0, str(process.test_output))
                    tx = self.tx(f)
                    self.assert_snapshot_and_original(f, tx)
                    if point == "ledger.after_stage":
                        self.assertEqual((f.root / ".runtime" / (".ledger-" + tx.name + ".tmp")).read_bytes(),
                                         self.old_ledger)
                    elif point == "ledger.after_apply_before_done":
                        self.assertEqual((f.root / ".runtime/model_proxy_totals.json").read_bytes(),
                                         self.old_ledger)
                    else:
                        self.assertIn("Git 可能已前进", "".join(process.test_output))
                        self.assertFalse((f.root / ".runtime/model_proxy_totals.json").exists())
                    self.tamper(f, action)
                    self.assert_no_secrets_staged(f)
                    recovery = f.command("rollback", tx.name, recover=True)
                    self.assertNotEqual(recovery.returncode, 0, fixture.diagnostic(recovery))
                    self.assertIn("UNKNOWN", (recovery.stdout + recovery.stderr).upper())
                    self.assert_snapshot_and_original(f, tx)

    def test_target_ignore_must_be_tracked_not_just_sentinel(self):
        for case in ("git_failure", "missing", "negated"):
            with self.subTest(case=case):
                f = self.make_fixture(target_ignores={"git_failure": True, "missing": False,
                                                      "negated": "negated"}[case])
                point = "ignore.git_failure" if case == "git_failure" else None
                p = f._finish(f._start("switch", point=point, hook=f.base / "unused"))
                self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
                self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)
                self.assertFalse((f.root / ".runtime/model_proxy_totals.json").exists())
                self.assertFalse(list((f.root / ".runtime").glob(".ledger-*.tmp")))
                self.assert_snapshot_and_original(f, self.tx(f))
                self.assertIn("Git 可能已前进", p.stderr)
                self.tamper(f, "delete")
                self.assert_no_secrets_staged(f)

    def test_partial_git_update_with_old_head_never_creates_runtime_ledger(self):
        f = self.make_fixture()
        p = f._finish(f._start("switch", point="git.partial_before_ref", hook=f.base / "unused"))
        self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
        self.assertIn(".runtime/", (f.root / ".gitignore").read_text())
        self.assertFalse((f.root / ".runtime/model_proxy_totals.json").exists())
        self.assertFalse(list((f.root / ".runtime").glob(".ledger-*.tmp")))
        tx = self.tx(f)
        self.assert_snapshot_and_original(f, tx)
        self.assertIn("Git 可能已前进", p.stderr)
        self.tamper(f, "delete")
        self.assert_no_secrets_staged(f)
        recovery = f.command("rollback", tx.name, recover=True)
        self.assertNotEqual(recovery.returncode, 0, fixture.diagnostic(recovery))
        self.assertIn("UNKNOWN", (recovery.stdout + recovery.stderr).upper())

    def test_tampering_sentinel_after_check_does_not_affect_target_ignore(self):
        for action in ("delete", "change"):
            with self.subTest(action=action):
                f = self.make_fixture()
                p = f._finish(f._start("switch", point="ignore." + action + "_after_check",
                                       hook=f.base / "unused"))
                self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
                self.assertEqual((f.root / ".runtime/model_proxy_totals.json").read_bytes(), self.old_ledger)
                self.assert_no_secrets_staged(f)

    def test_switch_and_snapshot_rollback_with_six_appkey_references(self):
        f = self.make_fixture()
        p = f.command("switch")
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        tx = f.txn(p)
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)
        self.assertEqual((f.root / ".runtime/model_proxy_totals.json").read_bytes(), self.old_ledger)
        fixture.put(f.root / ".runtime/model_proxy_totals.json", fixture.ledger(103))
        fixture.put(f.root / ".runtime/model_proxy.log", "fake log " + TOKEN + "\n")
        f.config_json["admin_token"] = "FAKE_NEW_CONFIG"
        f.save_config()
        fixture.put(f.sidecar, b'{"fake":{"session":"new"}}\n')
        fixture.put(f.root / ".model_proxy_totals.json", fixture.ledger(103))
        p = f.command("rollback", tx.name)
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
        self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)
        self.assertEqual(f.config.read_bytes(), self.config)
        self.assertEqual(f.sidecar.read_bytes(), self.sidecar)
        self.assertFalse((f.root / ".runtime/model_proxy_totals.json").exists())
        self.assertEqual((tx / "new-ledger.before-return").read_bytes(), fixture.ledger(103))
        self.assertEqual((tx / "new-log.before-return").read_text(), "fake log " + TOKEN + "\n")
        self.assertFalse((f.root / ".runtime/model_proxy.log").exists())
        self.tamper(f, "delete")
        self.assert_no_secrets_staged(f)

    def test_rollback_interruption_after_git_return_has_no_runtime_ledger(self):
        f = self.make_fixture()
        p = f.command("switch")
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        tx = f.txn(p)
        # No new writes: the runtime ledger remains an exact old snapshot copy.
        with f.paused("rollback", "rollback.after_apply_before_done", tx.name, abort=True) as process:
            self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
            self.assertFalse((f.root / ".runtime/model_proxy_totals.json").exists())
        self.assertNotEqual(process.returncode, 0, str(process.test_output))
        self.assertEqual((tx / "new-ledger.before-return").read_bytes(), self.old_ledger)
        self.tamper(f, "delete")
        self.assert_no_secrets_staged(f)
        recovery = f.command("rollback", tx.name, recover=True)
        self.assertNotEqual(recovery.returncode, 0, fixture.diagnostic(recovery))
        self.assertIn("UNKNOWN", (recovery.stdout + recovery.stderr).upper())

    def test_unknown_runtime_is_never_claimed(self):
        for existing in ("unknown", ".gitignore"):
            with self.subTest(existing=existing):
                f = self.make_fixture()
                fixture.put(f.root / ".runtime" / existing, b"user data\n")
                p = f.command("switch")
                self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
                self.assertEqual((f.root / ".runtime" / existing).read_bytes(), b"user data\n")

    def test_unignored_old_source_refuses_before_transaction(self):
        for rule in (False, "negated"):
            with self.subTest(rule=rule):
                f = self.make_fixture(legacy_ledger_ignored=rule)
                p = f.command("switch")
                self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
                self.assertIn("旧账本未被旧提交", p.stderr)
                self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
                self.assertFalse((f.root / ".runtime").exists())
                self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)

    def test_target_must_also_ignore_legacy_source_before_git_advance(self):
        f = self.make_fixture(target_legacy_ignored=False)
        p = f.command("switch")
        self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assertIn("目标提交的根忽略规则未保护原旧账本", p.stderr)
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
        self.assertFalse((f.root / ".runtime").exists())
        self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)

    def test_nonempty_backup_and_tmp_are_parked_without_reading_contents(self):
        f = self.make_fixture()
        p = f.command("switch")
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        tx = f.txn(p)
        runtime = f.root / ".runtime"
        backups, tmp = runtime / "backups", runtime / "tmp"
        backups.mkdir(mode=0o700)
        tmp.mkdir(mode=0o700)
        fixture.put(backups / "config.bak", "fake old copy " + BACKUP)
        fixture.put(tmp / "pending.tmp", "fake pending " + TOKEN)
        foreign = f.base / "foreign-fake.txt"
        fixture.put(foreign, b"untouched fake external data")
        (tmp / "foreign-link").symlink_to(foreign)
        source_ids = {path.name: (path.stat().st_dev, path.stat().st_ino) for path in (backups, tmp)}
        foreign_before = foreign.stat()
        fixture.put(runtime / "model_proxy_totals.json", fixture.ledger(103))
        fixture.put(f.root / ".model_proxy_totals.json", fixture.ledger(103))
        f.config_json["admin_token"] = "FAKE_CHANGED_CONFIG"
        f.save_config()
        fixture.put(f.sidecar, b'{"new":"sidecar"}\n')
        p = f._finish(f._start("rollback", tx.name, point="runtime-dir.no_contents_read",
                               hook=f.base / "unused"))
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.old_oid)
        self.assertEqual(f.config.read_bytes(), self.config)
        self.assertEqual(f.sidecar.read_bytes(), self.sidecar)
        self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)
        for name in ("backups", "tmp"):
            self.assertFalse(os.path.lexists(runtime / name))
            destination = tx / ("runtime-" + name + ".before-return")
            self.assertEqual((destination.stat().st_dev, destination.stat().st_ino), source_ids[name])
        self.assertEqual((tx / "runtime-backups.before-return/config.bak").read_text(),
                         "fake old copy " + BACKUP)
        self.assertEqual((tx / "runtime-tmp.before-return/pending.tmp").read_text(),
                         "fake pending " + TOKEN)
        link = tx / "runtime-tmp.before-return/foreign-link"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(foreign))
        self.assertEqual(foreign.read_bytes(), b"untouched fake external data")
        self.assertEqual(foreign.stat().st_ino, foreign_before.st_ino)
        manifest = json.loads((tx / "manifest.json").read_text())
        events = [event for event in manifest["events"]
                  if event["action"] == "PARK_RUNTIME_DIRECTORY"]
        self.assertEqual({event["source"] for event in events}, {"backups", "tmp"})
        for event in events:
            self.assertEqual(event["state"], "DONE")
            self.assertEqual(event["source_id"], event["target_id"])
            self.assertEqual(tuple(event["source_id"][:2]), source_ids[event["source"]])
            self.assertIn("source_parent_id", event)
            self.assertIn("target_parent_id", event)
        self.tamper(f, "delete")
        self.assert_no_secrets_staged(f)

    def test_symlink_and_unsafe_mode_are_rejected_without_touching_target(self):
        for kind in ("backups", "tmp", "unsafe-mode"):
            with self.subTest(kind=kind):
                f = self.make_fixture()
                p = f.command("switch")
                self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
                tx = f.txn(p)
                runtime = f.root / ".runtime"
                foreign = f.base / "fake-foreign"
                foreign.mkdir(mode=0o700)
                fixture.put(foreign / "unknown", b"foreign remains")
                before = foreign.stat()
                if kind == "unsafe-mode":
                    entry = runtime / "backups"
                    entry.mkdir(mode=0o700)
                    entry.chmod(0o755)
                    fixture.put(entry / "unknown", b"do not touch")
                else:
                    entry = runtime / kind
                    entry.symlink_to(foreign, target_is_directory=True)
                p = f.command("rollback", tx.name)
                self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
                self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)
                self.assertEqual((foreign / "unknown").read_bytes(), b"foreign remains")
                self.assertEqual((foreign.stat().st_ino, foreign.stat().st_mtime_ns),
                                 (before.st_ino, before.st_mtime_ns))
                if kind == "unsafe-mode":
                    self.assertEqual((entry / "unknown").read_bytes(), b"do not touch")
                else:
                    self.assertTrue(entry.is_symlink())
                    self.assertEqual(os.readlink(entry), str(foreign))
                self.assertFalse((tx / ("runtime-" + entry.name + ".before-return")).exists())
                self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)

    def test_existing_destination_and_simulated_cross_volume_block_without_overwrite(self):
        for case in ("destination", "cross-volume"):
            with self.subTest(case=case):
                f = self.make_fixture()
                p = f.command("switch")
                self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
                tx = f.txn(p)
                directory = f.root / ".runtime/backups"
                directory.mkdir(mode=0o700)
                fixture.put(directory / "unknown", b"source data")
                destination = tx / "runtime-backups.before-return"
                if case == "destination":
                    destination.mkdir(mode=0o700)
                    fixture.put(destination / "unknown", b"pre-existing destination")
                point = "runtime-dir.cross_device" if case == "cross-volume" else None
                p = f._finish(f._start("rollback", tx.name, point=point, hook=f.base / "unused"))
                self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
                self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)
                self.assertEqual((directory / "unknown").read_bytes(), b"source data")
                if case == "destination":
                    self.assertEqual((destination / "unknown").read_bytes(), b"pre-existing destination")
                else:
                    self.assertFalse(os.path.lexists(destination))

    def test_directory_migration_interruption_is_unknown_and_preserves_contents(self):
        f = self.make_fixture()
        p = f.command("switch")
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        tx = f.txn(p)
        runtime = f.root / ".runtime"
        for name in ("backups", "tmp"):
            (runtime / name).mkdir(mode=0o700)
            fixture.put(runtime / name / "fake", name + " data")
        with f.paused("rollback", "runtime-dir.after_apply_before_done", tx.name, abort=True) as process:
            manifest = json.loads((tx / "manifest.json").read_text())
            event = manifest["events"][-1]
            self.assertEqual((event["action"], event["state"], event["source"]),
                             ("PARK_RUNTIME_DIRECTORY", "INTENT", "backups"))
            self.assertEqual((tx / "runtime-backups.before-return/fake").read_text(), "backups data")
            self.assertEqual((runtime / "tmp/fake").read_text(), "tmp data")
        self.assertNotEqual(process.returncode, 0, str(process.test_output))
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)
        self.assertEqual((f.root / ".model_proxy_totals.json").read_bytes(), self.old_ledger)
        recovery = f.command("rollback", tx.name, recover=True)
        self.assertNotEqual(recovery.returncode, 0, fixture.diagnostic(recovery))
        self.assertIn("UNKNOWN", (recovery.stdout + recovery.stderr).upper())
        self.assertEqual((tx / "runtime-backups.before-return/fake").read_text(), "backups data")

    def test_replaced_runtime_parent_blocks_without_touching_replacement(self):
        f = self.make_fixture()
        p = f.command("switch")
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        tx = f.txn(p)
        runtime = f.root / ".runtime"
        (runtime / "backups").mkdir(mode=0o700)
        fixture.put(runtime / "backups/fake", b"original directory")
        moved_runtime = f.root / ".runtime-original"
        with f.paused("rollback", "runtime-dir.before_commit", tx.name) as process:
            runtime.rename(moved_runtime)
            runtime.mkdir(mode=0o700)
            fixture.put(runtime / "unrelated", b"replacement must not be touched")
        self.assertNotEqual(process.returncode, 0, str(process.test_output))
        self.assertEqual((runtime / "unrelated").read_bytes(), b"replacement must not be touched")
        self.assertEqual((moved_runtime / "backups/fake").read_bytes(), b"original directory")
        self.assertFalse(os.path.lexists(tx / "runtime-backups.before-return"))
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)

    def test_unknown_runtime_root_entry_still_blocks_without_modification(self):
        f = self.make_fixture()
        p = f.command("switch")
        self.assertEqual(p.returncode, 0, fixture.diagnostic(p))
        tx = f.txn(p)
        unknown = f.root / ".runtime/unknown-root-file"
        fixture.put(unknown, b"preserve user file")
        p = f.command("rollback", tx.name)
        self.assertNotEqual(p.returncode, 0, fixture.diagnostic(p))
        self.assertEqual(unknown.read_bytes(), b"preserve user file")
        self.assertEqual(f.git("rev-parse", "HEAD").stdout.strip(), f.target)


if __name__ == "__main__":
    unittest.main()
