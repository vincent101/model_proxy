"""Targeted fake-ps/lsof and disposable-Git tests; no real service inspection."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools/macos_switch_impl.py"
spec = importlib.util.spec_from_file_location("_fake_switch_quiescence", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ProcessCheckTests(unittest.TestCase):
    def check(self, ps=b"", *, listener=b"", lsof_rc=1, lsof_err=b"",
              ps_rc=0, ps_err=b""):
        calls = []

        def fake_run(command):
            calls.append(command)
            if command[0] == "/usr/sbin/lsof":
                self.assertEqual(command, ["/usr/sbin/lsof", "-nP", "-iTCP:54321",
                                           "-sTCP:LISTEN", "-t"])
                return subprocess.CompletedProcess(command, lsof_rc, listener, lsof_err)
            self.assertEqual(command, ["/bin/ps", "-ww", "-axo", "pid=,uid=,command="])
            return subprocess.CompletedProcess(command, ps_rc, ps, ps_err)

        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.os, "getpid", return_value=12345), \
             mock.patch.object(m, "_run_without_signals", side_effect=fake_run):
            m.check_processes_and_port(54321)
        return calls

    def test_known_entries_including_other_accounts_are_rejected(self):
        commands = [
            b"python /vault/tools/model_proxy/model_proxy.py",
            b"python /vault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/model_proxy.py",
            b"python /vault/tools/model_proxy/core/server.py",
            b"python /vault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/server.py",
            b"/bin/sh /vault/tools/model_proxy/model_proxy_cli.sh on",
            b"python /vault/tools/model_proxy/core/cli/main.py on",
            b"python ./core/server.py",
            b"python /other-project/core/server.py",
            b"python core/cli/main.py",
            b"python /other-project/core/cli/main.py",
            b"python -m core.server",
            b'python -m "core.server"',
            b"python -mcore.server",
            b"python -m core.cli.main",
            b"python -mcore.cli.main",
            b"python -m model_proxy",
            b"python -mmodel_proxy",
            b"python -m core.cli.model_proxy",
            b"python -m 'model_proxy.server'",
            b"python /vault/model_proxy/server.py",
            b"python /vault/My model_proxy Project/server.py",
            b"python /vault/model_proxy/custom_entry.py",
            b"python /vault/model_proxy_server.py",
        ]
        for uid in (501, 0, 999):
            for command in commands:
                with self.subTest(uid=uid, command=command):
                    with self.assertRaisesRegex(m.SafetyError, "入口进程"):
                        self.check(b"200 " + str(uid).encode() + b" " + command + b"\n")

    def test_unrelated_relative_and_spaced_absolute_servers_are_allowed(self):
        calls = self.check(
            b"101 501 python server.py\n"
            b"102 999 /usr/bin/python3 server.py\n"
            b"103 0 python ./server.py\n"
            b"104 501 python /other-project/server.py\n"
            b"105 999 python /other/My Project/server.py\n"
            b'106 0 python "/other/My Project/server.py"\n')
        self.assertEqual(len(calls), 2)

    def test_error_does_not_reveal_command_or_secret(self):
        with self.assertRaises(m.SafetyError) as raised:
            self.check(b"101 999 python -mcore.server --token=FAKE_SECRET\n")
        self.assertNotIn("FAKE_SECRET", str(raised.exception))
        self.assertNotIn("python", str(raised.exception))

    def test_help_states_generic_entry_and_manual_stop_limits(self):
        help_text = m._parser("switch").format_help()
        self.assertIn("通用 server.py 无法判定归属", help_text)
        self.assertIn("不能发现任意改名或自定义入口", help_text)
        self.assertIn("须由操作者自行确保停稳", help_text)

    def test_self_is_excluded_but_other_uid_is_not(self):
        self.check(b"12345 501 python /vault/model_proxy.py\n")
        with self.assertRaisesRegex(m.SafetyError, "入口进程"):
            self.check(b"12346 0 python /vault/model_proxy.py\n")

    def test_listener_and_failed_or_malformed_checks_reject(self):
        with self.assertRaisesRegex(m.SafetyError, "端口仍有监听"):
            self.check(listener=b"200\n", lsof_rc=0)
        for kwargs in ({"lsof_rc": 2}, {"lsof_err": b"permission denied"},
                       {"ps_rc": 1}, {"ps_err": b"error"},
                       {"ps": b"malformed\n"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(m.SafetyError):
                self.check(**kwargs)


class FakeRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="switch-fake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "fake-repo"
        self.root.mkdir()
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self.tmp.name, "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        subprocess.run(["/usr/bin/git", "init", "-q", "--template=", str(self.root)],
                       env=self.env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.runtime = self.root / ".runtime"
        self.runtime.mkdir()
        self.sentinel = self.runtime / ".gitignore"
        self.sentinel.write_bytes(b"*\n")
        self.root_ignore = self.root / ".gitignore"
        self.root_ignore.write_bytes(b".runtime/\n")
        self.ledger = self.runtime / "model_proxy_totals.json"
        self.ledger.write_bytes(b"fake ledger only\n")
        self.ids = {self.root: (1,), self.root / ".git": (2,), self.runtime: (3,)}
        self.sentinel_id = (4,)
        content = self.root_ignore.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest().encode()
        self.tree = {b".gitignore": (b"100644", blob)}
        self.tx = SimpleNamespace(data={
            "transaction": "a" * 32,
            "repository": {"root": str(self.root), "root_id": (1,), "git_id": (2,),
                           "target_oid": "a" * 40},
            "events": [
                {"action": "CREATE_RUNTIME", "state": "DONE", "runtime_id": (3,)},
                {"action": "CREATE_RUNTIME_IGNORE", "state": "DONE",
                 "name": ".gitignore", "runtime_id": (3,), "identity": (4,)},
            ],
        })

    def patches(self, *, remove_after_check=False):
        def fake_git(root, *args, **kwargs):
            self.assertEqual(root, self.root)
            self.assertIn(args[0], ("ls-files", "check-ignore"))
            result = subprocess.run(["/usr/bin/git", "-C", str(root), *args],
                                    env=self.env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
            if result.returncode not in kwargs.get("allowed", (0,)):
                raise m.SafetyError("fake Git check failed")
            if remove_after_check and args[0] == "check-ignore":
                # Simulated non-cooperative actor, deleting only the fake root rule.
                self.root_ignore.unlink()
            return result.stdout

        def fake_read(path):
            self.assertEqual(path, self.root_ignore)
            try:
                return path.read_bytes(), self.sentinel_id
            except OSError:
                raise m.SafetyError("fake sentinel missing") from None

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(m, "_directory_identity",
                                              side_effect=lambda path: self.ids[path]))
        stack.enter_context(mock.patch.object(m, "_read_stable_file", side_effect=fake_read))
        stack.enter_context(mock.patch.object(m, "_git", side_effect=fake_git))
        stack.enter_context(mock.patch.object(m, "_git_scalar", return_value="a" * 40))
        stack.enter_context(mock.patch.object(m, "_tree", return_value=self.tree))
        stack.enter_context(mock.patch.object(m, "_index", return_value=self.tree))
        return stack

    def test_fake_git_root_ignore_control_and_missing_sentinel(self):
        with self.patches():
            m._verify_target_runtime_ignore(self.tx, self.runtime)
            self.sentinel.unlink()
            # The current implementation relies on the root rule, not the sentinel.
            m._verify_target_runtime_ignore(self.tx, self.runtime)
            self.root_ignore.unlink()
            with self.assertRaises(m.SafetyError):
                m._verify_target_runtime_ignore(self.tx, self.runtime)
        self.assertEqual(self.ledger.read_bytes(), b"fake ledger only\n")

    def test_root_ignore_removed_after_git_check_is_detected_and_cli_warns(self):
        class FakeTTY(io.StringIO):
            def fileno(self):
                return 9

        def fake_switch(**kwargs):
            m._verify_target_runtime_ignore(self.tx, self.runtime)
            self.fail("missing fake root rule must stop the transaction")

        preview = {"config_original": "fake", "sidecar_original": "fake",
                   "old_listen": ("127.0.0.1", 54321),
                   "new_listen": ("127.0.0.1", 54321)}
        args = ["switch", "--root", str(self.root), "--target", "a" * 40,
                "--port", "54321", "--old-cwd", str(self.root), "--new-cwd", str(self.root),
                "--old-home", self.tmp.name, "--new-home", self.tmp.name,
                "--manual-stopped", "--auto-start-disabled"]
        err = io.StringIO()
        with self.patches(remove_after_check=True), \
             mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch("builtins.open", return_value=FakeTTY()), \
             mock.patch.object(m.os, "isatty", return_value=True), \
             mock.patch.object(m.signal, "signal"), \
             mock.patch.object(m, "_ask", side_effect=["UNSET", "UNSET", "UNSET", "一致"]), \
             mock.patch.object(m, "_manual_risk_confirmation"), \
             mock.patch.object(m, "inspect_context", return_value=preview), \
             mock.patch.object(m, "_migration_base", return_value=self.root), \
             mock.patch.object(m, "_legacy_lock_path", return_value=self.root / "fake.lock"), \
             mock.patch.object(m, "switch_operation", side_effect=fake_switch), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(m.main(args), 1)
        self.assertIn("账本可能未被Git忽略，禁止 git add/push；保持停服并检查事务", err.getvalue())
        self.assertIn("保留现场", err.getvalue())
        self.assertEqual(self.ledger.read_bytes(), b"fake ledger only\n")
        self.assertFalse(self.root_ignore.exists())
        self.assertTrue(self.sentinel.exists())


if __name__ == "__main__":
    unittest.main()
