"""Real Mac CLI lifecycle in an isolated temporary project; no external network."""
from __future__ import annotations

import contextlib
import fcntl
import http.client
import io
import json
import os
import pty
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_ISOLATION = Path(os.environ.get("MODEL_PROXY_TEST_ISOLATION", "/invalid")).resolve()
if (Path(__file__).resolve().parents[1] != _ISOLATION / "project"
        or not (_ISOLATION / ".model-proxy-isolation").is_file()
        or not Path(os.environ.get("HOME", "/invalid")).resolve().is_relative_to(_ISOLATION)):
    raise RuntimeError("an isolated source snapshot and fake HOME are required")
sys.path.insert(0, str(_ISOLATION / "project"))

from core.cli import dispatch
from core.cli import authority as authority_module
from core.cli.control import ControlError, MacControl
from core.cli.authority import MacAuthorityLease, MacInstallationAuthority
from core.discovery import PreflightError, process_executable, process_start_identity
from core import platform_process


@unittest.skipUnless(sys.platform == "darwin", "requires native Darwin process evidence")
class LiveMacCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="live-cli-", dir=_ISOLATION)
        self.root = Path(self.temp.name).resolve() / "installation"
        self.root.mkdir(mode=0o700)
        shutil.copytree(_ISOLATION / "project" / "core", self.root / "core",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.root / "config").mkdir(mode=0o700)
        (self.root / "runtime").mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        (self.root / "VERSION").write_text("0.0-test\n")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.assertNotEqual(self.port, 18889)
        self.assertGreaterEqual(self.port, 1024)
        self.paths = {key: str(self.root / "runtime" / key)
                      for key in ("log", "totals", "lock", "pid", "ensure_log", "start_lock")}
        paths = self.root / "config" / "runtime_paths.json"
        paths.write_text(json.dumps(self.paths))
        paths.chmod(0o600)
        self.config = self.root / "config" / "model_proxy_config.json"
        self.config.write_text(json.dumps({
            "listen": {"host": "127.0.0.1", "port": self.port},
            "admin_token": "fake-live-admin-token",
            "supplies": [], "routes": [], "strategies": [],
        }))
        self.config.chmod(0o600)
        self.env = {"HOME": str(self.home), "TMPDIR": str(self.root),
                    "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                    "MODEL_PROXY_CONFIG": str(self.config)}
        self.children = []
        self.child_identity = {}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in self.children:
            if child.poll() is None:
                start, image = self.child_identity.get(child, (None, None))
                if start is None or image is None:
                    raise AssertionError("owned child identity unavailable; no signal or cleanup")
                try:
                    platform_process.check_identity(child.pid, start, image)
                except platform_process.ProcessEvidenceError:
                    # The first observation may precede Python's exec. Refresh
                    # only the image of the same owned process generation,
                    # and independently require this exact fake entry.
                    fresh_start = process_start_identity(child.pid)
                    fresh_image = process_executable(child.pid)
                    if fresh_start != start or fresh_image is None:
                        raise AssertionError("owned child generation unverified; no signal or cleanup")
                    args, _ = platform_process.darwin_launch_evidence(child.pid, start, fresh_image)
                    if not args or args[-1] != str(self.root / "core/cli/model_proxy.py"):
                        raise AssertionError("owned child entry unverified; no signal or cleanup")
                    image = fresh_image
                platform_process.signal_verified(child.pid, start, image)
            child.wait(timeout=10)
        self.temp.cleanup()

    def _run(self, command):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = dispatch.main([command], root=self.root)
        return result, out.getvalue(), err.getvalue()

    def _run_from_root(self, command, config_spelling):
        # Relative config paths must be controlled from the same caller cwd.
        previous = os.getcwd()
        try:
            os.chdir(self.root)
            with mock.patch.dict(os.environ, {"MODEL_PROXY_CONFIG": config_spelling}):
                return self._run(command)
        finally:
            os.chdir(previous)

    @contextlib.contextmanager
    def _managed_environment(self, *, capture_stderr=False):
        real_popen = subprocess.Popen
        def capture_child(*args, **kwargs):
            if capture_stderr:
                fd = os.open(self.home / "fake-child-stderr.log",
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    kwargs["stderr"] = fd
                    child = real_popen(*args, **kwargs)
                finally:
                    os.close(fd)
            else:
                child = real_popen(*args, **kwargs)
            self.children.append(child)
            self.child_identity[child] = (process_start_identity(child.pid),
                                          process_executable(child.pid))
            threading.Thread(target=child.wait, daemon=True).start()
            return child
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch("core.cli.control.subprocess.Popen", side_effect=capture_child):
            yield

    def _start_interactively(self, expected=0):
        with contextlib.ExitStack() as stack:
            master, slave = pty.openpty()
            stack.callback(os.close, master)
            stack.callback(os.close, slave)
            stdin = stack.enter_context(os.fdopen(os.dup(slave), "r"))
            stdout = stack.enter_context(os.fdopen(os.dup(slave), "w"))
            os.write(master, b"temporary isolated fixture\nYES\n")
            stderr = io.StringIO()
            with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout), \
                    contextlib.redirect_stderr(stderr):
                result = dispatch.main(["on"], root=self.root)
            diagnostic = stderr.getvalue() + f" owned_children={len(self.children)}"
            log = self.home / "fake-child-stderr.log"
            if log.exists() and result != expected:
                diagnostic += ("\nchild exits: " + repr([child.poll() for child in self.children])
                               + "\nfake child stderr: "
                               + log.read_text(errors="replace").replace(
                                   "fake-live-admin-token", "[REDACTED]"))
            self.assertEqual(result, expected, diagnostic)
            return stderr.getvalue()

    def _install_listen_migration_stub(self):
        """Test-only substitute while the server agent updates the real guard."""
        inject = self.root / "test-only-stub"
        inject.mkdir(mode=0o700)
        (inject / "sitecustomize.py").write_text(r'''
import os, sys
from pathlib import Path
if Path(sys.argv[0]).name == "model_proxy.py":
    import core.server as server
    def checked_stub(records, current_paths, current_listen, lock_ids):
        active = records["active"]
        if active is None:
            return
        if active["runtime_paths"] != current_paths or active["locks"] != lock_ids:
            raise server.PreflightError("runtime_drift", "Runtime paths changed")
        try:
            os.kill(active["pid"], 0)
        except ProcessLookupError:
            pass
        except OSError:
            raise server.PreflightError("process_unverified", "Process unknown")
        else:
            raise server.PreflightError("process_still_active", "Old process still exists")
        # The CLI checks the old endpoint. A newer real server may already
        # hold an old-listener reservation while invoking this test stub.
    server._ensure_stale_instance = checked_stub
''')
        self.env["PYTHONPATH"] = str(inject) + os.pathsep + str(self.root)

    def _new_loopback_port(self, host="127.0.0.1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind((host, 0))
            port = probe.getsockname()[1]
        self.assertNotIn(port, (self.port, 18889))
        return port

    @staticmethod
    def _fake_ledger(version, requests=3):
        combo = {"requests": requests, "ok": requests, "fail": 0,
                 "usage_in": 7, "usage_out": 11}
        bucket = {"requests": requests, "ok": requests, "fail": 0,
                  "sum_ms": 0, "combos": {"fake-supply|fake-route": combo}}
        if version == 3:
            bucket.update(max_ms=0, client_disconnect=0)
            combo.update(attempts=0, attempt_fail=0, client_disconnect=0)
        return json.dumps({"version": version, "since": "2026-01-01",
                           "keep_days": 7, "total": bucket, "days": {},
                           "months_archive": {}}, sort_keys=True).encode()

    def test_relative_direct_entry_reexec_preserves_control_and_venv(self):
        import time
        import venv
        with self._managed_environment():
            self._start_interactively()
            self.assertEqual(self._run("off")[0], 0)
            (self.root / "config" / "inner").mkdir()
            venv.EnvBuilder(with_pip=False, symlinks=True).create(str(self.root / "venv"))
            spelling = "config/inner/../model_proxy_config.json"
            env = dict(self.env, MODEL_PROXY_CONFIG=spelling,
                       MODEL_PROXY_START_NONCE="b" * 32)
            stderr_file = self.home / "relative-stderr.log"
            with stderr_file.open("wb") as err:
                child = subprocess.Popen(["./venv/bin/python", "-B", "core/cli/model_proxy.py"],
                                         cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=err)
            active_file = self.root / ".runtime" / "active.json"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    self.fail("relative child exited: " + stderr_file.read_text(errors="replace"))
                try:
                    active = json.loads(active_file.read_text())
                    if active.get("pid") == child.pid and active.get("state") == "READY":
                        break
                except (FileNotFoundError, ValueError):
                    pass
                time.sleep(0.05)
            else:
                self.fail("relative child did not become ready: " + stderr_file.read_text(errors="replace"))
            start, image = process_start_identity(child.pid), process_executable(child.pid)
            self.child_identity[child] = (start, image)
            args, observed = platform_process.darwin_launch_evidence(child.pid, start, image)
            self.assertEqual(args[-2:], ("-B", str(self.root / "core/cli/model_proxy.py")))
            self.assertEqual(observed["MODEL_PROXY_CONFIG"], str(self.root / spelling))
            self.assertEqual(observed["MODEL_PROXY_START_NONCE"], "b" * 32)
            self.assertEqual(active["pid"], child.pid)
            for command in ("status", "reload"):
                result, _, err = self._run_from_root(command, spelling)
                self.assertEqual(result, 0, command + ": " + err)
            # An independently located sidecar link and a rotated-away log
            # must not falsely block a restart of the verified old process.
            sidecar_target = self.root / "config" / "sidecar-target.json"
            sidecar_target.write_text('{}'); sidecar_target.chmod(0o600)
            sidecar = self.root / "config" / "session_overrides.json"
            sidecar.symlink_to(sidecar_target.name)
            Path(self.paths['log']).unlink()
            result, _, err = self._run_from_root("restart", spelling)
            self.assertEqual(result, 0, "restart: " + err)
            child.wait(timeout=10)
            self.assertTrue(sidecar.is_symlink())
            self.assertEqual(sidecar_target.read_text(), '{}')
            result, _, err = self._run_from_root("off", spelling)
            self.assertEqual(result, 0, "off: " + err)

    def test_plain_relative_direct_entry_is_controllable(self):
        import time
        with self._managed_environment():
            self._start_interactively()
            self.assertEqual(self._run("off")[0], 0)
            stderr_file = self.home / "plain-relative-stderr.log"
            with stderr_file.open("wb") as err:
                child = subprocess.Popen([sys.executable, "core/cli/model_proxy.py"],
                                         cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=err)
            active_file = self.root / ".runtime" / "active.json"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    self.fail("direct child exited: " + stderr_file.read_text(errors="replace"))
                try:
                    active = json.loads(active_file.read_text())
                    if active.get("pid") == child.pid and active.get("state") == "READY":
                        break
                except (FileNotFoundError, ValueError):
                    pass
                time.sleep(0.05)
            else:
                self.fail("direct child not ready: " + stderr_file.read_text(errors="replace"))
            start, image = process_start_identity(child.pid), process_executable(child.pid)
            self.child_identity[child] = (start, image)
            args, observed = platform_process.darwin_launch_evidence(child.pid, start, image)
            self.assertEqual(args[-1], str(self.root / "core/cli/model_proxy.py"))
            self.assertEqual(len(args), 2)
            self.assertEqual(observed["MODEL_PROXY_CONFIG"], str(self.config))
            for command in ("status", "reload", "off"):
                result, _, err = self._run(command)
                self.assertEqual(result, 0, command + ": " + err)
            child.wait(timeout=10)

    def test_plain_relative_config_is_managed_using_same_spelling(self):
        import time
        with self._managed_environment():
            self._start_interactively()
            self.assertEqual(self._run("off")[0], 0)
            spelling = "config/model_proxy_config.json"
            env = dict(self.env, MODEL_PROXY_CONFIG=spelling)
            stderr_file = self.home / "relative-config-stderr.log"
            with stderr_file.open("wb") as err:
                child = subprocess.Popen([sys.executable, "core/cli/model_proxy.py"],
                                         cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=err)
            active_file = self.root / ".runtime/active.json"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    self.fail("relative config child exited: " + stderr_file.read_text(errors="replace"))
                try:
                    active = json.loads(active_file.read_text())
                    if active.get("pid") == child.pid and active.get("state") == "READY":
                        break
                except (FileNotFoundError, ValueError):
                    pass
                time.sleep(0.05)
            else:
                self.fail("relative config child not ready")
            start, image = process_start_identity(child.pid), process_executable(child.pid)
            self.child_identity[child] = (start, image)
            _, observed = platform_process.darwin_launch_evidence(child.pid, start, image)
            self.assertEqual(observed["MODEL_PROXY_CONFIG"], str(self.root / spelling))
            for command in ("status", "reload", "off"):
                result, _, err = self._run_from_root(command, spelling)
                self.assertEqual(result, 0, command + ": " + err)
            child.wait(timeout=10)

    def test_direct_unsafe_config_permissions_and_invalid_links_fail_before_listen(self):
        """Unsafe targets and broken/cyclic links are rejected before binding."""
        entry = str(self.root / "core/cli/model_proxy.py")
        config_dir = self.root / "config"
        (config_dir / "broken.json").symlink_to("absent.json")
        (config_dir / "loop-a.json").symlink_to("loop-b.json")
        (config_dir / "loop-b.json").symlink_to("loop-a.json")
        cases = (("config/model_proxy_config.json", 0o644),
                 ("config/model_proxy_config.json", 0o000),
                 ("config/broken.json", 0o600),
                 ("config/loop-a.json", 0o600))
        try:
            for spelling, mode in cases:
                with self.subTest(config=spelling, mode=oct(mode)):
                    self.config.chmod(mode)
                    env = dict(self.env, MODEL_PROXY_CONFIG=spelling)
                    result = subprocess.run([sys.executable, entry], cwd=self.root, env=env,
                                            text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("config_read_error", result.stderr)
                    self.assertNotIn("fake-live-admin-token", result.stdout + result.stderr)
                    self.assertFalse((self.root / ".runtime").exists())
                    self.assertFalse(any(Path(path).exists() for path in self.paths.values()))
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", self.port))
        finally:
            self.config.chmod(0o600)

    def test_trusted_config_links_direct_launch_and_control(self):
        """The selected OS target remains controllable for each link spelling."""
        import time
        config_dir = self.root / "config"
        (config_dir / "inner").mkdir()
        (config_dir / "link.json").symlink_to("model_proxy_config.json")
        (self.root / "config-parent").symlink_to(config_dir, target_is_directory=True)
        (self.root / "pivot").symlink_to(config_dir / "inner", target_is_directory=True)
        # Lexical collapse of pivot/.. would select this invalid decoy instead.
        decoy = self.root / "model_proxy_config.json"
        decoy.write_text('{"invalid-decoy":'); decoy.chmod(0o600)
        spellings = ("config/link.json", "config-parent/model_proxy_config.json",
                     "pivot/../model_proxy_config.json")
        with self._managed_environment():
            self._start_interactively()
            self.assertEqual(self._run("off")[0], 0)
            for spelling in spellings:
                with self.subTest(config=spelling):
                    stderr_file = self.home / "linked-direct-stderr.log"
                    env = dict(self.env, MODEL_PROXY_CONFIG=spelling)
                    with stderr_file.open("wb") as err:
                        child = subprocess.Popen([sys.executable, "core/cli/model_proxy.py"],
                                                 cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=err)
                    active_file = self.root / ".runtime/active.json"
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if child.poll() is not None:
                            self.fail("linked direct child exited: " +
                                      stderr_file.read_text(errors="replace").replace(
                                          "fake-live-admin-token", "[REDACTED]"))
                        try:
                            active = json.loads(active_file.read_text())
                            if active.get("pid") == child.pid and active.get("state") == "READY":
                                break
                        except (FileNotFoundError, ValueError):
                            pass
                        time.sleep(0.05)
                    else:
                        self.fail("linked direct child did not become ready")
                    self.assertEqual(active["listen"]["port"], self.port)
                    for command in ("status", "reload", "off"):
                        result, out, err = self._run_from_root(command, spelling)
                        self.assertEqual(result, 0, command + ": " + err)
                        self.assertNotIn("fake-live-admin-token", out + err)
                    child.wait(timeout=10)
        self.assertEqual(decoy.read_text(), '{"invalid-decoy":')
        self.assertTrue((config_dir / "link.json").is_symlink())
        self.assertTrue((self.root / "config-parent").is_symlink())
        self.assertTrue((self.root / "pivot").is_symlink())

    def test_unknown_config_protection_fails_before_spawn_or_credentials(self):
        from core.mac_config_path import MacConfigError
        with self._managed_environment(), \
                mock.patch("core.mac_config_path._directory_attributes",
                           side_effect=MacConfigError("directory_attributes_unverified")) as proof, \
                mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                mock.patch.object(platform_process, "signal_verified") as signal, \
                mock.patch.object(socket.socket, "connect") as connect:
            self._start_interactively(expected=1)
            proof.assert_called()
            token.assert_not_called()
            signal.assert_not_called()
            connect.assert_not_called()
            self.assertFalse(self.children)
            self.assertFalse((self.root / ".runtime").exists())
            self.assertFalse(any(Path(path).exists() for path in self.paths.values()))

    def _exercise_config_mutation_during_startup(self, kind):
        """Instrument only the temporary copy to schedule a deterministic race."""
        with self._managed_environment():
            self._start_interactively()
            self.assertEqual(self._run("off")[0], 0)
            state = self.root / ".runtime"
            names = ("install.json", "active.json", "last_good_paths.json")
            before = {name: (state / name).read_bytes() for name in names}
            marker = self.root / "bind-observed"
            replacement = self.root / "config/replacement.json"
            replacement.write_bytes(self.config.read_bytes()); replacement.chmod(0o600)
            server_path = self.root / "core/server.py"
            source = server_path.read_text()
            needle = "        initial_private = _mac_private_config_snapshot(config_path)\n"
            injection = needle + (
                "        if os.environ.get('MP_TEST_MUTATION') == 'permission': os.chmod(config_path, 0o644)\n"
                "        if os.environ.get('MP_TEST_MUTATION') == 'replacement': os.replace(os.environ['MP_TEST_REPLACEMENT'], config_path)\n")
            self.assertEqual(source.count(needle), 1)
            source = source.replace(needle, injection, 1)
            hook = r"""
# Test-only instrumentation in this temporary copy, never production.
_test_real_server = ProxyHTTPServer
def _test_bind(*args, **kwargs):
    Path(os.environ['MP_TEST_BIND_MARKER']).write_text('bound')
    return _test_real_server(*args, **kwargs)
ProxyHTTPServer = _test_bind
_test_real_verify = _verify_migration_guard
_test_checks = 0
def _test_verify(*args, **kwargs):
    global _test_checks
    result = _test_real_verify(*args, **kwargs)
    _test_checks += 1
    if os.environ.get('MP_TEST_MUTATION') == 'before_ready' and _test_checks == 6:
        os.chmod(args[1], 0o644)
    return result
_verify_migration_guard = _test_verify
"""
            source = source.replace('\nif __name__ == "__main__":\n    main()\n', hook + '\nif __name__ == "__main__":\n    main()\n')
            server_path.write_text(source)
            env = dict(self.env, MP_TEST_MUTATION=kind, MP_TEST_REPLACEMENT=str(replacement),
                       MP_TEST_BIND_MARKER=str(marker))
            result = subprocess.run([sys.executable, str(self.root / "core/cli/model_proxy.py")],
                                    cwd=self.root, env=env, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("startup failed", result.stderr)
            expected_code = {"permission": "startup_config_changed",
                             "replacement": "startup_config_changed",
                             "before_ready": "config_read_error"}[kind]
            self.assertIn(expected_code, result.stderr)
            self.assertNotIn("fake-live-admin-token", result.stderr)
            self.assertEqual(marker.exists(), kind == "before_ready")
            self.assertEqual({name: (state / name).read_bytes() for name in names}, before)
            # The child is already dead; bind probes only this isolated port.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", self.port))
            self.config.chmod(0o600)

    def test_permission_change_between_entry_and_guard_never_binds(self):
        self._exercise_config_mutation_during_startup("permission")

    def test_config_replacement_between_entry_and_guard_never_binds(self):
        self._exercise_config_mutation_during_startup("replacement")

    def test_permission_change_after_bind_never_publishes_new_ready(self):
        self._exercise_config_mutation_during_startup("before_ready")

    def test_unsupported_direct_invocations_fail_before_records(self):
        entry = str(self.root / "core/cli/model_proxy.py")
        for args in ([sys.executable, entry, "extra"],
                     [sys.executable, "-I", entry],
                     [sys.executable, "-m", "core.cli.model_proxy"]):
            with self.subTest(args=args):
                result = subprocess.run(args, cwd=self.root, env=self.env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Mac service launch cannot be verified", result.stderr)
                self.assertFalse((self.root / ".runtime").exists())
                self.assertFalse(any(Path(path).exists() for path in self.paths.values()))

    def test_real_tty_on_status_reload_restart_off_and_stale_restart(self):
        with self._managed_environment():
            self._start_interactively()
            self.assertEqual(len(self.children), 1)
            active = json.loads((self.root / ".runtime" / "active.json").read_text())
            self.assertEqual(active["pid"], self.children[-1].pid)
            self.assertEqual(active["state"], "READY")
            result, out, err = self._run("on")
            self.assertEqual(result, 0, err)
            self.assertIn("already running", out)
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            self.assertNotIn("fake-live-admin-token", out + err)
            self.assertIn("service running on port", out)
            self.assertIn("health:", out)
            self.assertIn("active sessions (30min):", out)
            result, out, err = self._run("reload")
            self.assertEqual(result, 0, err)
            self.assertIn("Reload succeeded", out)
            result, out, err = self._run("restart")
            self.assertEqual(result, 0, err)
            self.assertEqual(len(self.children), 2)
            self.assertNotEqual(self.children[0].pid, self.children[1].pid)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(self.children[1].poll())
            # The protected READY record is now stale; a new on must validate
            # process exit and the original locks before spawning.
            result, out, err = self._run("on")
            self.assertEqual(result, 0, err)
            self.assertEqual(len(self.children), 3)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_after_off_revalidates_stale_instance(self):
        with self._managed_environment():
            self._start_interactively()
            old = self.children[-1]
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(old.poll())
            result, out, err = self._run("restart")
            self.assertEqual(result, 0, err)
            self.assertIn("already stopped", out)
            self.assertNotEqual(self.children[-1].pid, old.pid)
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            self.assertIn("active sessions (30min):", out)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_stale_restart_rejects_pid_reuse_or_unknown(self):
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            count = len(self.children)
            for reason in ("process_identity_changed", "process_liveness_unavailable"):
                with self.subTest(reason=reason), \
                        mock.patch.object(platform_process, "process_gone",
                            side_effect=platform_process.ProcessEvidenceError(reason)), \
                        mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    signal.assert_not_called()
                    self.assertEqual(len(self.children), count)

    def test_stale_restart_rejects_real_other_process_with_old_start_identity(self):
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            active = self.root / ".runtime" / "active.json"
            original = active.read_bytes()
            record = json.loads(original)
            script = self.root / "fake-other-process.py"
            script.write_text('import sys; print("ready", flush=True); sys.stdin.readline()\n')
            other = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=self.env)
            self.assertEqual(other.stdout.readline().strip(), "ready")
            self.assertNotEqual(process_start_identity(other.pid), record["process_start"])
            record["pid"] = other.pid
            active.write_text(json.dumps(record))
            try:
                count = len(self.children)
                with mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    signal.assert_not_called()
                    self.assertIsNone(other.poll())
                    self.assertEqual(len(self.children), count)
            finally:
                active.write_bytes(original)
                if other.poll() is None:
                    other.stdin.write("\n")
                    other.stdin.flush()
                self.assertEqual(other.wait(timeout=5), 0)
                other.stdin.close()
                other.stdout.close()

    def test_stale_restart_rejects_old_lock_or_port_conflict(self):
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            count = len(self.children)
            with open(self.paths["lock"], "r+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    signal.assert_not_called()
                    self.assertEqual(len(self.children), count)
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", self.port))
                occupied.listen(1)
                with mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    self.assertIn("port_unavailable", err)
                    signal.assert_not_called()
                    self.assertEqual(len(self.children), count)

    def test_injected_ledger_preflight_failure_does_not_stop_old_process(self):
        # Wiring contract only: this deliberately does not pretend the future
        # server ledger-validation API exists or validate real ledger syntax.
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            ledger = Path(self.paths["totals"])
            before = ledger.read_bytes() if ledger.exists() else None
            sentinel = b"{fake-invalid-ledger"
            ledger.write_bytes(sentinel)
            ledger.chmod(0o600)
            try:
                with mock.patch.object(MacAuthorityLease, "assert_restart_supported",
                        side_effect=PreflightError("ledger_unverified", "fake rejection")), \
                        mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    signal.assert_not_called()
                    self.assertIsNone(child.poll())
                    self.assertEqual(len(self.children), 1)
                    self.assertEqual(ledger.read_bytes(), sentinel)
            finally:
                if before is None:
                    ledger.unlink()
                else:
                    ledger.write_bytes(before)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_accepts_missing_and_valid_v2_v3_ledgers(self):
        with self._managed_environment(capture_stderr=True):
            self._start_interactively()
            ledger = Path(self.paths["totals"])
            for version in (None, 2, 3):
                with self.subTest(version=version):
                    if ledger.exists():
                        ledger.unlink()
                    expected = None if version is None else self._fake_ledger(version)
                    if expected is not None:
                        ledger.write_bytes(expected)
                        ledger.chmod(0o600)
                    previous = self.children[-1]
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 0, err)
                    self.assertIsNotNone(previous.poll())
                    self.assertNotEqual(self.children[-1].pid, previous.pid)
                    self.assertEqual(ledger.read_bytes() if ledger.exists() else None, expected)
                    result, out, err = self._run("status")
                    self.assertEqual(result, 0, err)
                    self.assertNotIn("fake-live-admin-token", out + err)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_bad_or_inaccessible_ledger_preserves_running_instance(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            ledger = Path(self.paths["totals"])
            original = ledger.read_bytes() if ledger.exists() else None
            invalid_schema = self._fake_ledger(3, requests=-1)
            cases = (("invalid-json", b"{fake-bad-json", 0o600),
                     ("invalid-schema", invalid_schema, 0o600),
                     ("unreadable", self._fake_ledger(3), 0o000),
                     ("writable-by-others", self._fake_ledger(3), 0o666))
            try:
                for name, content, mode in cases:
                    with self.subTest(case=name):
                        if ledger.exists():
                            ledger.chmod(0o600)
                        ledger.write_bytes(content)
                        ledger.chmod(mode)
                        before_children = len(self.children)
                        with mock.patch.object(platform_process, "signal_verified") as signal, \
                                mock.patch.object(MacAuthorityLease, "admin_token") as token:
                            result, out, err = self._run("restart")
                            self.assertEqual(result, 1, err)
                            signal.assert_not_called()
                            token.assert_not_called()
                            self.assertIsNone(child.poll())
                            self.assertEqual(len(self.children), before_children)
                        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), mode)
                        ledger.chmod(0o600)
                        self.assertEqual(ledger.read_bytes(), content)
            finally:
                if ledger.exists():
                    ledger.chmod(0o600)
                if original is None:
                    ledger.unlink(missing_ok=True)
                else:
                    ledger.write_bytes(original)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_rechecks_ledger_changed_after_initial_preflight(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            ledger = Path(self.paths["totals"])
            original = ledger.read_bytes() if ledger.exists() else None
            ledger.write_bytes(self._fake_ledger(3))
            ledger.chmod(0o600)
            bad = b"{fake-corrupt-after-preflight"
            real_preflight = MacAuthorityLease.assert_restart_supported
            def corrupt_after_preflight(lease):
                result = real_preflight(lease)
                ledger.write_bytes(bad)
                return result
            try:
                with mock.patch.object(MacAuthorityLease, "assert_restart_supported",
                                       corrupt_after_preflight), \
                        mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    signal.assert_not_called()
                    self.assertIsNone(child.poll())
                    self.assertEqual(ledger.read_bytes(), bad)
            finally:
                if original is None:
                    ledger.unlink(missing_ok=True)
                else:
                    ledger.write_bytes(original)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_retries_proven_ledger_replacement_then_revalidates(self):
        with self._managed_environment():
            self._start_interactively()
            old = self.children[-1]
            ledger = Path(self.paths["totals"])
            ledger.write_bytes(self._fake_ledger(3, requests=3))
            ledger.chmod(0o600)
            replacement = ledger.with_name("fake-replacement")
            newer = self._fake_ledger(3, requests=5)
            replacement.write_bytes(newer)
            replacement.chmod(0o600)
            actual_capture = authority_module._capture_restart_ledger
            calls = []
            def changed_once(snapshot):
                calls.append(snapshot)
                if len(calls) == 1:
                    os.replace(replacement, ledger)
                    raise PreflightError("ledger_changed", "fake observed replacement")
                return actual_capture(snapshot)
            with mock.patch.object(authority_module, "_capture_restart_ledger",
                                   side_effect=changed_once):
                result, out, err = self._run("restart")
            self.assertEqual(result, 0, err)
            self.assertGreaterEqual(len(calls), 2)
            self.assertIsNotNone(old.poll())
            self.assertEqual(ledger.read_bytes(), newer)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_unstable_or_replaced_ledger_never_blindly_signals(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            ledger = Path(self.paths["totals"])
            initial = self._fake_ledger(3, requests=3)
            ledger.write_bytes(initial)
            ledger.chmod(0o600)
            with mock.patch.object(authority_module, "_capture_restart_ledger",
                    side_effect=PreflightError("ledger_changed", "fake instability")) as capture, \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                result, out, err = self._run("restart")
                self.assertEqual(result, 1)
                self.assertEqual(capture.call_count, 4)
                signal.assert_not_called()
                self.assertIsNone(child.poll())
                self.assertEqual(ledger.read_bytes(), initial)

            # Exercise the real server capture with an atomic replacement
            # after its path snapshot. Ambiguous ledger_unverified is not retried.
            from core import server
            actual_capture = server._capture_migration_ledger
            newer = self._fake_ledger(3, requests=7)
            replacement = ledger.with_name("fake-replacement")
            replacement.write_bytes(newer)
            replacement.chmod(0o600)
            calls = []
            def replace_during_capture(snapshot):
                calls.append(snapshot)
                os.replace(replacement, ledger)
                return actual_capture(snapshot)
            with mock.patch.object(server, "_capture_migration_ledger",
                                   side_effect=replace_during_capture), \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                result, out, err = self._run("restart")
                self.assertEqual(result, 1)
                self.assertEqual(len(calls), 1)
                signal.assert_not_called()
                self.assertIsNone(child.poll())
                self.assertEqual(ledger.read_bytes(), newer)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_off_uses_process_identity_without_socket_or_token(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            with mock.patch.object(MacControl, "_connection",
                                   side_effect=OSError("unreachable")) as connect, \
                    mock.patch.object(platform_process, "verify_connected_peer") as peer, \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                    mock.patch.object(platform_process, "signal_verified",
                                      wraps=platform_process.signal_verified) as signal:
                result, out, err = self._run("off")
                self.assertEqual(result, 0, err)
                connect.assert_not_called()
                peer.assert_not_called()
                token.assert_not_called()
                self.assertEqual(signal.call_count, 1)
            self.assertIsNotNone(child.poll())

    def test_off_rechecks_actual_entry_before_any_signal(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            other = self.root / "other-fake-entry.py"
            other.write_text("# fake replacement entry\n")
            actual_launch = platform_process.darwin_launch_evidence
            count = 0

            def changed_entry(pid, start, executable):
                nonlocal count
                count += 1
                args, env = actual_launch(pid, start, executable)
                return ((args[0], str(other)), env) if count >= 3 else (args, env)

            with mock.patch.object(platform_process, "darwin_launch_evidence",
                                   side_effect=changed_entry), \
                    mock.patch.object(platform_process, "signal_verified") as signal, \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token:
                result, out, err = self._run("off")
                self.assertEqual(result, 1)
                self.assertGreaterEqual(count, 3)
                signal.assert_not_called()
                token.assert_not_called()
                self.assertIsNone(child.poll())
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_off_reports_stopped_without_claiming_old_port_free(self):
        with self._managed_environment():
            self._start_interactively()
            with mock.patch.object(MacControl, "_port_free",
                                   side_effect=ControlError("port_unavailable")), \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                    mock.patch.object(platform_process, "signal_verified",
                                      wraps=platform_process.signal_verified) as signal:
                result, out, err = self._run("off")
                self.assertEqual(result, 1, err)
                self.assertIn("port_unavailable", err)
                self.assertIn("process exited; locks released; port occupied", err)
                signal.assert_called_once()
                token.assert_not_called()
            self.assertIsNotNone(self.children[-1].poll())

    def test_off_with_missing_lan_interface_does_not_connect(self):
        # The address is simulated; no LAN socket or DNS is ever used.
        with self._managed_environment():
            self._start_interactively()
            state = self.root / ".runtime"
            for name in ("active.json", "last_good_paths.json"):
                path = state / name
                record = json.loads(path.read_text())
                record["listen"]["host"] = "192.0.2.10"
                path.write_text(json.dumps(record))
            config = json.loads(self.config.read_text())
            config["listen"]["host"] = "192.0.2.10"
            config["allow_hosts"] = ["localhost"]
            self.config.write_text(json.dumps(config))
            with mock.patch.object(platform_process, "is_local_numeric_address", return_value=False), \
                    mock.patch.object(MacControl, "_connection") as connect, \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                    mock.patch.object(platform_process, "signal_verified",
                                      wraps=platform_process.signal_verified) as signal:
                result, out, err = self._run("off")
                self.assertEqual(result, 1, err)
                self.assertIn("port_check_unknown", err)
                self.assertIn("process exited; locks released; port release unknown", err)
                signal.assert_called_once()
                connect.assert_not_called()
                token.assert_not_called()
            self.assertIsNotNone(self.children[-1].poll())

    def test_same_port_wildcard_expansion_refused_before_old_stop(self):
        import signal as signal_module
        with self._managed_environment():
            self._start_interactively()
            old = self.children[-1]
            old_pid = old.pid
            old_start = process_start_identity(old_pid)
            old_image = process_executable(old_pid)
            self.assertIsNotNone(old_start)
            self.assertIsNotNone(old_image)
            platform_process.check_identity(old_pid, old_start, old_image)
            self.child_identity[old] = (old_start, old_image)
            state = self.root / ".runtime"
            records = {name: (state / name).read_bytes()
                       for name in ("install.json", "active.json", "last_good_paths.json")}
            self.assertEqual(json.loads(records["active.json"])["pid"], old_pid)
            ledger = Path(self.paths["totals"])
            ledger.write_bytes(self._fake_ledger(3, requests=7))
            ledger.chmod(0o600)
            before_ledger = ledger.read_bytes()
            child_count = len(self.children)
            real_kill = os.kill
            occupied = None
            try:
                occupied = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                occupied.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                occupied.bind(("::1", self.port))
                occupied.listen(1)
            except OSError:
                if occupied is not None:
                    occupied.close()
                self.skipTest("same-port IPv6 loopback unavailable")
            with occupied:
                original = self.config.read_bytes()
                try:
                    for host in ("::", "0.0.0.0"):
                        with self.subTest(host=host):
                            config = json.loads(original)
                            config["listen"] = {"host": host, "port": self.port}
                            config["allow_hosts"] = ["localhost"]
                            self.config.write_text(json.dumps(config))
                            restart_nonzero = []
                            restart_zero_probes = []
                            def monitor_restart_kill(pid, signum):
                                if signum == 0:
                                    restart_zero_probes.append(pid)
                                    return real_kill(pid, signum)
                                restart_nonzero.append((pid, signum))
                                # Control may catch this error: the list below
                                # remains the independent assertion evidence.
                                raise RuntimeError("unexpected nonzero restart signal")
                            with mock.patch.object(os, "kill", side_effect=monitor_restart_kill), \
                                    mock.patch.object(platform_process, "signal_verified") as signal:
                                os.kill(old_pid, 0)  # Real liveness probe, not a delivered signal.
                                result, out, err = self._run("restart")
                                with self.subTest(check="os_kill_zero_probe"):
                                    self.assertIn(old_pid, restart_zero_probes)
                                with self.subTest(check="os_kill_nonzero"):
                                    self.assertEqual(restart_nonzero, [])
                                with self.subTest(check="signal"):
                                    signal.assert_not_called()
                            with self.subTest(check="children"):
                                self.assertEqual(len(self.children), child_count)
                            with self.subTest(check="old_process"):
                                self.assertIsNone(old.poll())
                                self.assertEqual(json.loads((state / "active.json").read_bytes())["pid"], old_pid)
                                platform_process.check_identity(old_pid, old_start, old_image)
                            with self.subTest(check="records"):
                                self.assertEqual(records, {name: (state / name).read_bytes() for name in records})
                            with self.subTest(check="ledger"):
                                self.assertEqual(ledger.read_bytes(), before_ledger)
                            with self.subTest(check="rejection"):
                                self.assertEqual(result, 1, err)
                                if host == "::":
                                    self.assertIn("mac_ipv6_listen_unsupported", err)
                                    self.assertIn("no signal or credential sent", err)
                                else:
                                    self.assertIn("wildcard_restart_requires_off", err)
                                    self.assertIn("use off then on", err)
                finally:
                    self.config.write_bytes(original)
            off_nonzero = []
            def monitor_off_kill(pid, signum):
                if signum == 0:
                    return real_kill(pid, signum)
                off_nonzero.append((pid, signum))
                if (pid, signum) != (old_pid, signal_module.SIGTERM) or len(off_nonzero) != 1:
                    raise RuntimeError("unexpected nonzero off signal")
                return real_kill(pid, signum)
            with mock.patch.object(os, "kill", side_effect=monitor_off_kill):
                result, out, err = self._run("off")
            self.assertEqual(off_nonzero, [(old_pid, signal_module.SIGTERM)])
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(old.poll())

    def test_status_formats_cc_and_codex_sessions_and_redacts_admin(self):
        with self._managed_environment():
            self._start_interactively()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:23]
            lines = [
                f"{stamp} req_id=fakecc ACCESS ms=1 status=200 token=cc route=fake "
                "tier=sonnet supply=fake session=aaaaaaaa-1111\n",
                f"{stamp} req_id=fakecodex ACCESS ms=1 status=200 token=codex route=fake "
                "tier=sonnet supply=fake session=bbbbbbbb-2222\n",
                f"{stamp} req_id=fakeadmin ACCESS ms=1 status=200 token=fake-live-admin-token "
                "route=fake tier=sonnet supply=fake session=cccccccc-3333\n",
            ]
            with Path(self.paths["log"]).open("a", encoding="utf-8") as stream:
                stream.writelines(lines)
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            self.assertIn("active sessions (30min): 3", out)
            self.assertIn("[cc]", out)
            self.assertIn("[codex]", out)
            self.assertIn("[REDACTED]", out)
            self.assertNotIn("fake-live-admin-token", out + err)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_default_cli_script_status_reload_and_off(self):
        real_popen = subprocess.Popen
        with self._managed_environment():
            self._start_interactively()
            for command in ("status", "reload", "off"):
                child = real_popen([sys.executable, str(self.root / "core/cli/main.py"), command],
                                   cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                out, err = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, command + ": " + err)
                self.assertNotIn("fake-live-admin-token", out + err)
                if command == "status":
                    self.assertIn("service running on port", out)
                    self.assertIn("active sessions (30min):", out)

    def test_insecure_service_entry_is_rejected_before_spawn(self):
        entry = self.root / "core" / "cli" / "model_proxy.py"
        entry.chmod(0o666)
        with self._managed_environment(), contextlib.ExitStack() as stack:
            master, slave = pty.openpty()
            stack.callback(os.close, master)
            stack.callback(os.close, slave)
            stdin = stack.enter_context(os.fdopen(os.dup(slave), "r"))
            stdout = stack.enter_context(os.fdopen(os.dup(slave), "w"))
            os.write(master, b"temporary isolated fixture\nYES\n")
            stderr = io.StringIO()
            with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout), \
                    contextlib.redirect_stderr(stderr):
                result = dispatch.main(["on"], root=self.root)
            self.assertEqual(result, 1)
            self.assertFalse(self.children)

    def test_default_entry_fails_closed_when_authority_dependency_unavailable(self):
        with self._managed_environment(), \
                mock.patch("core.cli.authority.MacInstallationAuthority",
                           side_effect=OSError("fake-secret-detail")), \
                mock.patch.object(platform_process, "signal_verified") as signal, \
                mock.patch.object(platform_process, "verify_connected_peer") as peer:
            for command in ("on", "off", "restart", "status", "reload"):
                result, out, err = self._run(command)
                self.assertEqual(result, 1, command)
                self.assertNotIn("fake-secret-detail", out + err)
            self.assertFalse(self.children)
            signal.assert_not_called()
            peer.assert_not_called()

    def test_exited_child_is_retried_once_after_full_revalidation(self):
        with self._managed_environment():
            capture = subprocess.Popen
            attempts = []
            def fail_first(*args, **kwargs):
                attempts.append("spawn")
                if len(attempts) == 1:
                    args = ([sys.executable, "-I", "-S", "-B", "-c",
                             "import sys; sys.stderr.write('model_proxy: startup failed (protection_changed)\\n'); sys.exit(1)"],)
                return capture(*args, **kwargs)
            with mock.patch("core.cli.control.subprocess.Popen", side_effect=fail_first):
                err = self._start_interactively()
            self.assertEqual(len(attempts), 2)
            self.assertEqual(self.children[0].wait(timeout=5), 1)
            self.assertIn("safe state rechecked, retrying once", err)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_exited_child_is_not_retried_if_config_changed(self):
        with self._managed_environment():
            capture = subprocess.Popen
            attempts = []
            original_retry = MacAuthorityLease.retry_exited_child
            def fail_child(*args, **kwargs):
                attempts.append("spawn")
                args = ([sys.executable, "-I", "-S", "-B", "-c", "import sys; sys.exit(1)"],)
                return capture(*args, **kwargs)
            def change_before_retry(lease, pid, nonce, code):
                config = json.loads(self.config.read_text())
                config["admin_token"] = "other-fake-admin"
                self.config.write_text(json.dumps(config))
                return original_retry(lease, pid, nonce, code)
            with mock.patch("core.cli.control.subprocess.Popen", side_effect=fail_child), \
                    mock.patch.object(MacAuthorityLease, "retry_exited_child", change_before_retry), \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                err = self._start_interactively(expected=1)
                self.assertIn("startup_failed", err)
                self.assertEqual(len(attempts), 1)
                signal.assert_not_called()

    def test_exited_child_is_not_retried_if_config_changes_after_first_recheck(self):
        with self._managed_environment():
            capture = subprocess.Popen
            attempts, checks = [], []
            original_retry = MacAuthorityLease.retry_exited_child
            def fail_child(*args, **kwargs):
                attempts.append("spawn")
                return capture([sys.executable, "-I", "-S", "-B", "-c",
                                "import sys; sys.exit(1)"], **kwargs)
            def change_after_first_check(lease, pid, nonce, code):
                result = original_retry(lease, pid, nonce, code)
                checks.append(result)
                if len(checks) == 1 and result is True:
                    config = json.loads(self.config.read_text())
                    config["admin_token"] = "other-fake-admin"
                    self.config.write_text(json.dumps(config))
                return result
            with mock.patch("core.cli.control.subprocess.Popen", side_effect=fail_child), \
                    mock.patch.object(MacAuthorityLease, "retry_exited_child", change_after_first_check), \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                err = self._start_interactively(expected=1)
                self.assertIn("start_state_changed", err)
                self.assertEqual(checks, [True, False])
                self.assertEqual(len(attempts), 1)
                self.assertNotIn("retrying once", err)
                signal.assert_not_called()

    def test_exited_child_retry_is_bounded_and_other_exit_is_not_retried(self):
        for exit_code, expected_attempts in ((1, 2), (2, 1)):
            with self.subTest(exit_code=exit_code):
                # Each subtest gets a separate project via a nested test instance.
                case = LiveMacCliTests(methodName="runTest")
                case.setUp()
                try:
                    with case._managed_environment():
                        capture = subprocess.Popen
                        attempts = []
                        def fail_child(*args, **kwargs):
                            attempts.append("spawn")
                            args = ([sys.executable, "-I", "-S", "-B", "-c",
                                     f"import sys; sys.exit({exit_code})"],)
                            return capture(*args, **kwargs)
                        with mock.patch("core.cli.control.subprocess.Popen", side_effect=fail_child), \
                                mock.patch.object(platform_process, "signal_verified") as signal:
                            err = case._start_interactively(expected=1)
                            self.assertIn("startup_failed", err)
                            self.assertEqual(len(attempts), expected_attempts)
                            signal.assert_not_called()
                finally:
                    case.doCleanups()

    def test_transient_startup_change_waits_then_revalidates_without_token(self):
        original = MacAuthorityLease.verify_active
        calls = []
        def transient(lease, instance):
            calls.append("verify")
            if len(calls) == 1:
                raise PreflightError("protection_changed", "synthetic transient")
            return original(lease, instance)
        with self._managed_environment(), \
                mock.patch.object(MacAuthorityLease, "verify_active", transient), \
                mock.patch.object(MacAuthorityLease, "admin_token",
                                  side_effect=AssertionError("token must not be read during on")) as token:
            self._start_interactively()
            self.assertGreaterEqual(len(calls), 2)
            token.assert_not_called()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_startup_permission_failure_never_sends_or_signals(self):
        with self._managed_environment(), contextlib.ExitStack() as stack:
            master, slave = pty.openpty()
            stack.callback(os.close, master)
            stack.callback(os.close, slave)
            stdin = stack.enter_context(os.fdopen(os.dup(slave), "r"))
            stdout = stack.enter_context(os.fdopen(os.dup(slave), "w"))
            os.write(master, b"temporary isolated fixture\nYES\n")
            stderr = io.StringIO()
            with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout), \
                    contextlib.redirect_stderr(stderr), \
                    mock.patch.object(MacAuthorityLease, "verify_active",
                                      side_effect=PreflightError("protection_unavailable", "denied")), \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                result = dispatch.main(["on"], root=self.root)
                self.assertEqual(result, 1)
                self.assertIn("protection_unavailable", stderr.getvalue())
                token.assert_not_called()
                signal.assert_not_called()
            # Only the directly owned child created by this test is stopped.
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_forged_record_with_real_other_python_process_never_signals_or_connects(self):
        with self._managed_environment():
            self._start_interactively()
            state = self.root / ".runtime"
            active_path = state / "active.json"
            original = active_path.read_bytes()
            active = json.loads(original)
            fake_script = self.root / "other.py"
            fake_script.write_text('import sys; print("ready",flush=True); sys.stdin.readline()')
            child_env = dict(self.env, MODEL_PROXY_START_NONCE=active["instance_nonce"])
            other = subprocess.Popen([sys.executable, str(fake_script)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=child_env)
            self.assertEqual(other.stdout.readline().strip(), "ready")
            active["pid"] = other.pid
            active["process_start"] = process_start_identity(other.pid)
            active["executable"] = process_executable(other.pid)
            self.assertIsNotNone(active["process_start"])
            active_path.write_text(json.dumps(active))
            try:
                with mock.patch.object(platform_process, "signal_verified",
                                       wraps=platform_process.signal_verified) as signal, \
                        mock.patch.object(platform_process, "verify_connected_peer",
                                          wraps=platform_process.verify_connected_peer) as peer:
                    for command in ("off", "status", "reload"):
                        result, out, err = self._run(command)
                        self.assertEqual(result, 1, command + ": " + err)
                        self.assertNotIn("fake-live-admin-token", out + err)
                    signal.assert_not_called()
                    peer.assert_not_called()
                    self.assertIsNone(other.poll())
            finally:
                active_path.write_bytes(original)
                if other.poll() is None:
                    other.stdin.write("\n")
                    other.stdin.flush()
                self.assertEqual(other.wait(timeout=5), 0)
                other.stdin.close()
                other.stdout.close()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_wrong_connected_peer_receives_no_http_or_token(self):
        with self._managed_environment():
            self._start_interactively()
            state = self.root / ".runtime"
            active_path, good_path = state / "active.json", state / "last_good_paths.json"
            old_active, old_good, old_config = (active_path.read_bytes(), good_path.read_bytes(),
                                                 self.config.read_bytes())
            received = []
            with socket.socket() as impostor:
                impostor.bind(("127.0.0.1", 0))
                impostor.listen(1)
                impostor.settimeout(30)
                fake_port = impostor.getsockname()[1]
                def accept_once():
                    try:
                        for _ in range(2):
                            conn, _ = impostor.accept()
                            with conn:
                                conn.settimeout(20)
                                received.append(conn.recv(4096))
                    except OSError:
                        received.append(b"test-error")
                thread = threading.Thread(target=accept_once, daemon=True)
                thread.start()
                active, good = json.loads(old_active), json.loads(old_good)
                active["listen"]["port"] = good["listen"]["port"] = fake_port
                config = json.loads(old_config)
                config["listen"]["port"] = fake_port
                self.config.write_text(json.dumps(config))
                active_path.write_text(json.dumps(active))
                good_path.write_text(json.dumps(good))
                try:
                    with mock.patch.object(platform_process, "verify_connected_peer",
                                           wraps=platform_process.verify_connected_peer) as peer, \
                            mock.patch.object(platform_process, "signal_verified") as signal:
                        for command in ("status", "reload"):
                            before = peer.call_count
                            result, out, err = self._run(command)
                            self.assertEqual(result, 1, err)
                            self.assertNotIn("fake-live-admin-token", out + err)
                            self.assertGreater(peer.call_count, before,
                                               "peer verification not reached: " + err)
                        signal.assert_not_called()
                    thread.join(timeout=35)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(received, [b"", b""])
                finally:
                    self.config.write_bytes(old_config)
                    active_path.write_bytes(old_active)
                    good_path.write_bytes(old_good)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_entry_change_after_last_peer_check_sends_no_bytes(self):
        with self._managed_environment():
            self._start_interactively()
            fake_entry = self.root / "other-after-exec.py"
            fake_entry.write_text("# fake different entry\n")
            log = Path(self.paths["log"])
            before = log.read_text(errors="replace").count("admin.status")
            actual_launch = platform_process.darwin_launch_evidence
            actual_peer = platform_process.verify_connected_peer
            actual_connection = MacControl._connection
            state = {"peer_checks": 0, "changed": False}
            sent = []

            def launch(pid, start, executable):
                args, env = actual_launch(pid, start, executable)
                if state["changed"]:
                    # Model exec of the same PID/start/Python image to a new entry.
                    args = (args[0], str(fake_entry))
                return args, env

            def peer(*args, **kwargs):
                result = actual_peer(*args, **kwargs)
                state["peer_checks"] += 1
                if state["peer_checks"] == 3:
                    state["changed"] = True
                return result

            class TrackedSocket:
                def __init__(self, sock):
                    self.sock = sock
                def __getattr__(self, name):
                    return getattr(self.sock, name)
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    self.sock.close()
                def sendall(self, data):
                    sent.append(len(data))
                    return self.sock.sendall(data)

            def connection(control, lease, instance):
                return TrackedSocket(actual_connection(control, lease, instance))

            with mock.patch.object(platform_process, "darwin_launch_evidence", side_effect=launch), \
                    mock.patch.object(platform_process, "verify_connected_peer", side_effect=peer), \
                    mock.patch.object(MacControl, "_connection", connection), \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                result, out, err = self._run("status")
                self.assertEqual(result, 1, err)
                self.assertTrue(state["changed"], "final connected-peer check was not reached")
                self.assertEqual(state["peer_checks"], 3)
                self.assertEqual(sent, [])
                self.assertNotIn("fake-live-admin-token", out + err)
                signal.assert_not_called()
            self.assertEqual(log.read_text(errors="replace").count("admin.status"), before)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_config_mismatch_and_reused_pid_identity_fail_before_action(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            other_config = self.root / "config" / "other.json"
            other_config.write_bytes(self.config.read_bytes())
            other_config.chmod(0o600)
            state = self.root / ".runtime"
            active_path = state / "active.json"
            original = active_path.read_bytes()
            with mock.patch.object(platform_process, "signal_verified",
                                   wraps=platform_process.signal_verified) as signal, \
                    mock.patch.object(platform_process, "verify_connected_peer",
                                      wraps=platform_process.verify_connected_peer) as peer, \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token:
                with mock.patch.dict(os.environ, {"MODEL_PROXY_CONFIG": str(other_config)}):
                    for command in ("status", "reload"):
                        result, out, err = self._run(command)
                        self.assertEqual(result, 1, command + ": " + err)
                self.config.chmod(0o644)
                try:
                    for command in ("status", "reload"):
                        result, out, err = self._run(command)
                        self.assertEqual(result, 1, command + ": " + err)
                finally:
                    self.config.chmod(0o600)
                altered = json.loads(original)
                altered["process_start"]["seconds"] += 1
                active_path.write_text(json.dumps(altered))
                try:
                    for command in ("off", "status"):
                        result, out, err = self._run(command)
                        self.assertEqual(result, 1, command + ": " + err)
                finally:
                    active_path.write_bytes(original)
                signal.assert_not_called()
                peer.assert_not_called()
                token.assert_not_called()
                # A plain off uses verified last-good identity, not the changed
                # caller config; it must signal only the owned old child.
                with mock.patch.dict(os.environ, {"MODEL_PROXY_CONFIG": str(other_config)}):
                    result, out, err = self._run("off")
                self.assertEqual(result, 0, err)
                signal.assert_called_once()
                self.assertEqual(signal.call_args.args[0], child.pid)
                peer.assert_not_called()
                token.assert_not_called()
                self.assertIsNotNone(child.poll())

    def test_listen_drift_occupied_new_port_preserves_old_then_off_stops_old_identity(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                reserve.listen(1)
                new_port = reserve.getsockname()[1]
                self.assertNotIn(new_port, (self.port, 18889))
                config = json.loads(self.config.read_text())
                config["listen"]["port"] = new_port
                self.config.write_text(json.dumps(config))
                with mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    self.assertIn("port_unavailable", err)
                    signal.assert_not_called()
            self.assertIsNone(child.poll(), "restart must not stop an instance it cannot migrate")
            with mock.patch.object(platform_process, "verify_connected_peer") as peer, \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                    mock.patch.object(platform_process, "signal_verified",
                                      wraps=platform_process.signal_verified) as signal:
                result, out, err = self._run("off")
                token.assert_not_called()
                peer.assert_not_called()
                self.assertEqual(signal.call_count, 1)
                self.assertEqual(signal.call_args.args[0], child.pid)
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(child.poll())
            previous_children = len(self.children)
            with MacInstallationAuthority().locked(self.root, self.config) as lease:
                self.assertTrue(lease.assert_startable())
            self.assertEqual(len(self.children), previous_children)

    def test_runtime_lock_drift_off_preserves_old_ledger_and_does_not_spawn(self):
        with self._managed_environment():
            self._start_interactively()
            ledger = Path(self.paths["totals"])
            sentinel = b'{"fake-ledger":"preserve"}'
            ledger.write_bytes(sentinel)
            ledger.chmod(0o600)
            changed = dict(self.paths, lock=str(self.root / "runtime" / "new-lock"))
            runtime = self.root / "config" / "runtime_paths.json"
            runtime.write_text(json.dumps(changed))
            with mock.patch.object(platform_process, "signal_verified") as signal:
                result, out, err = self._run("restart")
                self.assertEqual(result, 1)
                self.assertIn("runtime paths changed; migration required", err)
                signal.assert_not_called()
                self.assertIsNone(self.children[-1].poll())
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            self.assertEqual(ledger.read_bytes(), sentinel)
            previous_children = len(self.children)
            result, out, err = self._run("on")
            self.assertEqual(result, 1)
            self.assertIn("configuration_migration_required", err)
            self.assertEqual(ledger.read_bytes(), sentinel)
            self.assertEqual(len(self.children), previous_children)

    def test_ledger_path_drift_does_not_overwrite_or_discard_old_ledger(self):
        with self._managed_environment():
            self._start_interactively()
            old_ledger = Path(self.paths["totals"])
            sentinel = b'{"fake-ledger":"must-not-be-lost"}'
            old_ledger.write_bytes(sentinel)
            old_ledger.chmod(0o600)
            new_ledger = self.root / "runtime" / "new-totals"
            runtime = self.root / "config" / "runtime_paths.json"
            runtime.write_text(json.dumps(dict(self.paths, totals=str(new_ledger))))
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            previous_children = len(self.children)
            result, out, err = self._run("on")
            self.assertEqual(result, 1)
            self.assertIn("configuration_migration_required", err)
            self.assertEqual(old_ledger.read_bytes(), sentinel)
            self.assertFalse(new_ledger.exists())
            self.assertEqual(len(self.children), previous_children)

    def test_listen_change_after_off_does_not_mutate_old_record(self):
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            state = self.root / ".runtime"
            files = [state / name for name in ("install.json", "active.json", "last_good_paths.json")]
            before = [path.read_bytes() for path in files]
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                new_port = reserve.getsockname()[1]
            self.assertNotIn(new_port, (self.port, 18889))
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = new_port
            self.config.write_text(json.dumps(config))
            previous_children = len(self.children)
            with MacInstallationAuthority().locked(self.root, self.config) as lease:
                self.assertTrue(lease.assert_startable())
            self.assertEqual([path.read_bytes() for path in files], before)
            self.assertEqual(len(self.children), previous_children)

    def test_listen_change_after_off_with_explicit_server_stub(self):
        self._install_listen_migration_stub()
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            state = self.root / ".runtime"
            old = json.loads((state / "active.json").read_text())
            install_id = old["install_id"]
            new_port = self._new_loopback_port()
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = new_port
            self.config.write_text(json.dumps(config))
            ledger = Path(self.paths["totals"])
            ledger.write_text(json.dumps({"version": 3, "total": {"requests": 7, "ok": 7,
                               "fail": 0, "sum_ms": 0, "max_ms": 0, "combos": {}},
                               "days": {}, "months_archive": {}}))
            ledger.chmod(0o600)
            before_ledger = ledger.read_bytes()
            result, out, err = self._run("on")
            self.assertEqual(result, 0, err)
            active = json.loads((state / "active.json").read_text())
            good = json.loads((state / "last_good_paths.json").read_text())
            self.assertEqual(active["listen"]["port"], new_port)
            self.assertEqual(good["listen"], active["listen"])
            self.assertEqual(active["install_id"], install_id)
            self.assertEqual(active["runtime_paths"], old["runtime_paths"])
            self.assertEqual(ledger.read_bytes(), before_ledger)
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_live_listen_restart_with_explicit_server_stub(self):
        self._install_listen_migration_stub()
        with self._managed_environment():
            self._start_interactively()
            old_child = self.children[-1]
            old = json.loads((self.root / ".runtime" / "active.json").read_text())
            new_port = self._new_loopback_port()
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = new_port
            self.config.write_text(json.dumps(config))
            result, out, err = self._run("restart")
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(old_child.poll())
            active = json.loads((self.root / ".runtime" / "active.json").read_text())
            self.assertNotEqual(active["pid"], old["pid"])
            self.assertEqual(active["listen"]["port"], new_port)
            self.assertEqual(active["install_id"], old["install_id"])
            self.assertEqual(active["runtime_paths"], old["runtime_paths"])
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_listen_restart_to_ipv6_loopback_with_explicit_server_stub(self):
        self._assert_ipv6_restart_rejected(self._new_loopback_port(), install_stub=True)

    def test_old_port_taken_after_stop_blocks_new_start_without_record_change(self):
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            state = self.root / ".runtime"
            files = [state / name for name in ("install.json", "active.json", "last_good_paths.json")]
            before = [path.read_bytes() for path in files]
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = self._new_loopback_port()
            self.config.write_text(json.dumps(config))
            count = len(self.children)
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", self.port))
                occupied.listen(1)
                result, out, err = self._run("on")
                self.assertEqual(result, 1)
                self.assertIn("port_unavailable", err)
            self.assertEqual(len(self.children), count)
            self.assertEqual([path.read_bytes() for path in files], before)

    def test_restart_config_change_after_preflight_never_signals_old_process(self):
        with self._managed_environment():
            self._start_interactively()
            old_child = self.children[-1]
            original = self.config.read_bytes()
            config = json.loads(original)
            config["listen"]["port"] = self._new_loopback_port()
            self.config.write_text(json.dumps(config))
            real_check = MacAuthorityLease.assert_restart_supported
            def change_after_check(lease):
                result = real_check(lease)
                updated = json.loads(self.config.read_text())
                updated["admin_token"] = "other-fake-admin"
                self.config.write_text(json.dumps(updated))
                return result
            try:
                with mock.patch.object(MacAuthorityLease, "assert_restart_supported", change_after_check), \
                        mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    self.assertIn("start_state_changed", err)
                    self.assertIsNone(old_child.poll())
                    signal.assert_not_called()
            finally:
                self.config.write_bytes(original)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_restart_record_change_after_preflight_never_signals_new_identity(self):
        with self._managed_environment():
            self._start_interactively()
            old_child = self.children[-1]
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = self._new_loopback_port()
            self.config.write_text(json.dumps(config))
            state = self.root / ".runtime"
            files = [state / name for name in ("install.json", "active.json", "last_good_paths.json")]
            originals = [path.read_bytes() for path in files]
            real_check = MacAuthorityLease.assert_restart_supported
            def change_after_check(lease):
                result = real_check(lease)
                install, active, good = [json.loads(content) for content in originals]
                nonce = "f" * 32 if active["instance_nonce"] != "f" * 32 else "e" * 32
                install["last_instance_nonce"] = nonce
                active["instance_nonce"] = good["instance_nonce"] = nonce
                for path, value in zip(files, (install, active, good)):
                    path.write_text(json.dumps(value))
                return result
            try:
                with mock.patch.object(MacAuthorityLease, "assert_restart_supported", change_after_check), \
                        mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    self.assertIn("instance_changed", err)
                    self.assertIsNone(old_child.poll())
                    signal.assert_not_called()
            finally:
                for path, content in zip(files, originals):
                    path.write_bytes(content)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_real_server_accepts_new_listener_against_old_stale_records(self):
        with self._managed_environment():
            self._start_interactively()
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)
            state = self.root / ".runtime"
            files = [state / name for name in ("install.json", "active.json", "last_good_paths.json")]
            before = [path.read_bytes() for path in files]
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                new_port = reserve.getsockname()[1]
            self.assertNotIn(new_port, (self.port, 18889))
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = new_port
            self.config.write_text(json.dumps(config))
            result, out, err = self._run("on")
            self.assertEqual(result, 0, err)
            after = [json.loads(path.read_text()) for path in files]
            self.assertEqual(after[0]["install_id"], json.loads(before[0])["install_id"])
            self.assertEqual(after[1]["listen"]["port"], new_port)
            self.assertEqual(after[2]["listen"]["port"], new_port)
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_real_server_off_during_listen_drift_then_on_new_listener(self):
        with self._managed_environment():
            self._start_interactively()
            old_child = self.children[-1]
            state = self.root / ".runtime"
            old = json.loads((state / "active.json").read_text())
            new_port = self._new_loopback_port()
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = new_port
            self.config.write_text(json.dumps(config))
            with mock.patch.object(platform_process, "verify_connected_peer") as peer, \
                    mock.patch.object(MacAuthorityLease, "admin_token") as token:
                result, out, err = self._run("off")
                self.assertEqual(result, 0, err)
                token.assert_not_called()
                peer.assert_not_called()
            self.assertIsNotNone(old_child.poll())
            result, out, err = self._run("on")
            self.assertEqual(result, 0, err)
            active = json.loads((state / "active.json").read_text())
            self.assertEqual(active["listen"]["port"], new_port)
            self.assertEqual(active["install_id"], old["install_id"])
            self.assertEqual(active["runtime_paths"], old["runtime_paths"])
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_real_server_restart_migrates_only_listener(self):
        with self._managed_environment():
            self._start_interactively()
            old_child = self.children[-1]
            state = self.root / ".runtime"
            old = json.loads((state / "active.json").read_text())
            ledger = Path(self.paths["totals"])
            ledger.write_text(json.dumps({"version": 3, "total": {"requests": 7, "ok": 7,
                               "fail": 0, "sum_ms": 0, "max_ms": 0, "combos": {}},
                               "days": {}, "months_archive": {}}))
            ledger.chmod(0o600)
            before_ledger = ledger.read_bytes()
            new_port = self._new_loopback_port()
            config = json.loads(self.config.read_text())
            config["listen"]["port"] = new_port
            self.config.write_text(json.dumps(config))
            result, out, err = self._run("restart")
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(old_child.poll())
            active = json.loads((state / "active.json").read_text())
            good = json.loads((state / "last_good_paths.json").read_text())
            self.assertNotEqual(active["pid"], old["pid"])
            self.assertEqual(active["listen"]["port"], new_port)
            self.assertEqual(good["listen"], active["listen"])
            self.assertEqual(active["install_id"], old["install_id"])
            self.assertEqual(active["runtime_paths"], old["runtime_paths"])
            self.assertEqual(ledger.read_bytes(), before_ledger)
            result, out, err = self._run("status")
            self.assertEqual(result, 0, err)
            self.assertIn("active sessions", out)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_repeated_real_listener_migrations_and_lifecycle(self):
        # Exercise repeated READY -> stop -> stale-record -> start transitions
        # with the real server and explicitly temporary ports and paths.
        with self._managed_environment(capture_stderr=True):
            def detail(err):
                log = self.home / "fake-child-stderr.log"
                text = log.read_text(errors="replace") if log.exists() else ""
                return (err + "\nchild exits: " + repr([c.poll() for c in self.children])
                        + "\nfake child stderr: " + text.replace("fake-live-admin-token", "[REDACTED]"))

            self._start_interactively()
            state = self.root / ".runtime"
            initial = json.loads((state / "active.json").read_text())
            for cycle in range(3):
                if cycle:
                    result, out, err = self._run("on")
                    self.assertEqual(result, 0, detail(err))
                old = json.loads((state / "active.json").read_text())
                old_child = self.children[-1]
                new_port = self._new_loopback_port()
                config = json.loads(self.config.read_text())
                config["listen"]["port"] = new_port
                self.config.write_text(json.dumps(config))
                result, out, err = self._run("restart")
                self.assertEqual(result, 0, detail(err))
                self.assertIsNotNone(old_child.poll(), detail(err))
                active = json.loads((state / "active.json").read_text())
                self.assertEqual(active["listen"]["port"], new_port)
                self.assertEqual(active["install_id"], initial["install_id"])
                self.assertEqual(active["runtime_paths"], initial["runtime_paths"])
                self.assertNotEqual(active["pid"], old["pid"])
                result, out, err = self._run("status")
                self.assertEqual(result, 0, detail(err))
                self.assertIn("active sessions (30min):", out)
                self.assertNotIn("fake-live-admin-token", out + err)
                result, out, err = self._run("off")
                self.assertEqual(result, 0, detail(err))
                self.assertIsNotNone(self.children[-1].poll(), detail(err))

    def test_real_server_restart_migrates_to_ipv6_loopback(self):
        self._assert_ipv6_restart_rejected(self._new_loopback_port())

    def test_real_server_restart_migrates_to_ipv6_same_port(self):
        self._assert_ipv6_restart_rejected(self.port, check_recovery=True)

    def test_ipv6_first_on_rejects_before_install_record_or_spawn(self):
        config = json.loads(self.config.read_text())
        config["listen"] = {"host": "::1", "port": self.port}
        self.config.write_text(json.dumps(config))
        with self._managed_environment(), \
             mock.patch.object(authority_module, "initialize_installation",
                               wraps=authority_module.initialize_installation) as initialize, \
             mock.patch.object(platform_process, "signal_verified") as signal:
            err = self._start_interactively(expected=1)
            self.assertIn("mac_ipv6_listen_unsupported", err)
            initialize.assert_not_called()
            signal.assert_not_called()
            self.assertEqual(self.children, [])
        state = self.root / ".runtime"
        for name in ("install.json", "active.json", "last_good_paths.json"):
            self.assertFalse((state / name).exists())
        self.assertFalse(Path(self.paths["totals"]).exists())

    def test_localhost_ipv4_start_status_and_off_remain_available(self):
        config = json.loads(self.config.read_text())
        config["listen"] = {"host": "localhost", "port": self.port}
        self.config.write_text(json.dumps(config))
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            start = process_start_identity(child.pid)
            image = process_executable(child.pid)
            self.assertIsNotNone(start); self.assertIsNotNone(image)
            platform_process.check_identity(child.pid, start, image)
            self.child_identity[child] = (start, image)
            active = json.loads((self.root / ".runtime/active.json").read_text())
            self.assertEqual(active["listen"], {"host": "127.0.0.1", "port": self.port})
            result, _, err = self._run("status")
            self.assertEqual(result, 0, err)
            result, _, err = self._run("off")
            self.assertEqual(result, 0, err)
            self.assertIsNotNone(child.poll())

    def _assert_ipv6_restart_rejected(self, new_port, *, install_stub=False, check_recovery=False):
        if install_stub:
            self._install_listen_migration_stub()
        with self._managed_environment():
            self._start_interactively()
            self.assertGreaterEqual(self.port, 1024)
            self.assertGreaterEqual(new_port, 1024)
            self.assertNotEqual(new_port, 18889)
            old_child = self.children[-1]
            old_start = process_start_identity(old_child.pid)
            old_image = process_executable(old_child.pid)
            self.assertIsNotNone(old_start)
            self.assertIsNotNone(old_image)
            # Capture the READY child, not the transient pre-exec image that
            # the spawn callback may have observed.
            platform_process.check_identity(old_child.pid, old_start, old_image)
            self.child_identity[old_child] = (old_start, old_image)
            state = self.root / ".runtime"
            records = {name: (state / name).read_bytes()
                       for name in ("install.json", "active.json", "last_good_paths.json")}
            self.assertEqual(json.loads(records["active.json"])["pid"], old_child.pid)
            ledger = Path(self.paths["totals"])
            ledger.write_bytes(self._fake_ledger(3, requests=7))
            ledger.chmod(0o600)
            before_ledger = ledger.read_bytes()
            child_count = len(self.children)
            config = json.loads(self.config.read_text())
            config["listen"] = {"host": "::1", "port": new_port}
            self.config.write_text(json.dumps(config))
            with mock.patch.object(platform_process, "signal_verified") as signal:
                result, out, err = self._run("restart")
                self.assertEqual(result, 1)
                self.assertIn("mac_ipv6_listen_unsupported", err)
                signal.assert_not_called()
            self.assertEqual(len(self.children), child_count)
            self.assertIsNone(old_child.poll())
            platform_process.check_identity(old_child.pid, old_start, old_image)
            self.assertEqual(records, {name: (state / name).read_bytes() for name in records})
            self.assertEqual(ledger.read_bytes(), before_ledger)
            if check_recovery:
                # Current config drift must not grant status credential access;
                # off still targets the recorded IPv4 instance, not IPv6.
                with mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                     mock.patch.object(MacControl, "_connection") as connection:
                    result, _, _ = self._run("status")
                    self.assertEqual(result, 1)
                    token.assert_not_called()
                    connection.assert_not_called()
                with mock.patch.object(platform_process, "signal_verified",
                                       wraps=platform_process.signal_verified) as signal:
                    result, _, err = self._run("off")
                    self.assertEqual(result, 0, err)
                    signal.assert_called_once()
                    self.assertEqual(signal.call_args.args[0], old_child.pid)
                self.assertIsNotNone(old_child.poll())

    def test_occupied_ipv6_same_port_rejects_restart_before_signal(self):
        with self._managed_environment():
            self._start_interactively()
            old_child = self.children[-1]
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as occupied:
                occupied.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    occupied.bind(("::1", self.port))
                except OSError:
                    self.skipTest("same-port IPv6 loopback unavailable")
                occupied.listen(1)
                config = json.loads(self.config.read_text())
                config["listen"] = {"host": "::1", "port": self.port}
                self.config.write_text(json.dumps(config))
                with mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    self.assertIn("mac_ipv6_listen_unsupported", err)
                    signal.assert_not_called()
                self.assertIsNone(old_child.poll())
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_bad_new_config_never_signals_old_instance(self):
        with self._managed_environment():
            self._start_interactively()
            child = self.children[-1]
            original = self.config.read_bytes()
            self.config.write_text("{bad-json")
            try:
                with mock.patch.object(platform_process, "signal_verified") as signal:
                    result, out, err = self._run("restart")
                    self.assertEqual(result, 1)
                    signal.assert_not_called()
                    self.assertIsNone(child.poll())
            finally:
                self.config.write_bytes(original)
            with mock.patch.dict(os.environ, {"MODEL_PROXY_PORT": "invalid"}), \
                    mock.patch.object(platform_process, "signal_verified") as signal:
                result, out, err = self._run("restart")
                self.assertEqual(result, 1)
                signal.assert_not_called()
                self.assertIsNone(child.poll())
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_last_good_off_recovers_without_business_config_or_credentials(self):
        with self._managed_environment():
            self._start_interactively()
            original = self.config.read_bytes()
            for index, kind in enumerate(("invalid_json", "unsafe_permissions", "invalid_port")):
                with self.subTest(kind=kind):
                    child = self.children[-1]
                    if kind == "invalid_json":
                        self.config.write_text("{bad-json")
                    elif kind == "unsafe_permissions":
                        self.config.chmod(0o644)
                    override = {"MODEL_PROXY_PORT": "invalid"} if kind == "invalid_port" else {}
                    try:
                        with mock.patch.dict(os.environ, override), \
                                mock.patch.object(platform_process, "signal_verified",
                                                  wraps=platform_process.signal_verified) as signal, \
                                mock.patch.object(MacAuthorityLease, "admin_token") as token, \
                                mock.patch.object(MacControl, "_connection") as connect:
                            result, out, err = self._run("off")
                            self.assertEqual(result, 0, err)
                            signal.assert_called_once()
                            self.assertEqual(signal.call_args.args[0], child.pid)
                            token.assert_not_called()
                            connect.assert_not_called()
                            self.assertIsNotNone(child.poll())
                    finally:
                        self.config.chmod(0o600)
                        self.config.write_bytes(original)
                    if index < 2:
                        result, out, err = self._run("on")
                        self.assertEqual(result, 0, err)

    def test_consistent_forged_nonce_is_rejected_against_process_environment(self):
        with self._managed_environment():
            self._start_interactively()
            state = self.root / ".runtime"
            files = [state / name for name in ("install.json", "active.json", "last_good_paths.json")]
            originals = [path.read_bytes() for path in files]
            install, active, good = [json.loads(value) for value in originals]
            nonce = "f" * 32 if active["instance_nonce"] != "f" * 32 else "e" * 32
            install["last_instance_nonce"] = nonce
            active["instance_nonce"] = good["instance_nonce"] = nonce
            try:
                for path, value in zip(files, (install, active, good)):
                    path.write_text(json.dumps(value))
                with mock.patch.object(platform_process, "signal_verified",
                                       wraps=platform_process.signal_verified) as signal, \
                        mock.patch.object(platform_process, "verify_connected_peer",
                                          wraps=platform_process.verify_connected_peer) as peer:
                    for command in ("off", "status", "reload"):
                        result, out, err = self._run(command)
                        self.assertEqual(result, 1, command + ": " + err)
                        self.assertNotIn("fake-live-admin-token", out + err)
                    signal.assert_not_called()
                    peer.assert_not_called()
            finally:
                for path, content in zip(files, originals):
                    path.write_bytes(content)
            result, out, err = self._run("off")
            self.assertEqual(result, 0, err)

    def test_local_mock_json_reload_and_incremental_sse(self):
        gate = threading.Event()
        calls = []
        class LocalMock(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers.get("x-api-key"),
                              data.get("model"), bool(data.get("stream"))))
                if self.path != "/v1/messages" or self.headers.get("x-api-key") != "fake-upstream":
                    self.send_error(400)
                    return
                if data.get("stream"):
                    body = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"fake",'
                            b'"type":"message","role":"assistant","model":"fake-model","content":[],'
                            b'"stop_reason":null,"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
                            b'event: content_block_start\ndata: {"type":"content_block_start",'
                            b'"index":0,"content_block":{"type":"text","text":""}}\n\n'
                            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                            b'"delta":{"type":"text_delta","text":"fixture reply"}}\n\n'
                            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
                            b'event: message_delta\ndata: {"type":"message_delta",'
                            b'"delta":{"stop_reason":"end_turn","stop_sequence":null},'
                            b'"usage":{"output_tokens":1}}\n\n'
                            b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
                    content_type = "text/event-stream"
                else:
                    body = json.dumps({"id": "fake", "type": "message", "role": "assistant",
                                       "model": "fake-model", "content": [{"type": "text",
                                       "text": "fixture reply"}], "stop_reason": "end_turn",
                                       "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if data.get("stream"):
                    first, rest = body.split(b"\n\n", 1)
                    self.wfile.write(first + b"\n\n")
                    self.wfile.flush()
                    if gate.wait(6):
                        self.wfile.write(rest)
                        self.wfile.flush()
                else:
                    self.wfile.write(body)
                    self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalMock)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = json.loads(self.config.read_text())
            config["supplies"] = [{"id": "mock", "url": f"http://127.0.0.1:{server.server_port}/v1/messages",
                                   "protocol": "anthropic", "appkey": "fake-upstream",
                                   "target_model": "fake-model"}]
            config["routes"] = [{"id": "mock-route", "tiers": {"opus": ["mock"], "sonnet": ["mock"],
                                  "haiku": ["mock"]}, "failover": "on"}]
            config["strategies"] = [{"client_token": "fake-client",
                                     "route_pool": [{"route_id": "mock-route", "weight": 1}]}]
            self.config.write_text(json.dumps(config))
            with self._managed_environment():
                self._start_interactively()
                def request(stream):
                    payload = json.dumps({"model": "claude-sonnet", "max_tokens": 16,
                                          "messages": [{"role": "user", "content": "hello"}],
                                          "stream": stream})
                    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
                    try:
                        conn.request("POST", "/v1/messages", payload,
                                     {"Content-Type": "application/json", "x-api-key": "fake-client",
                                      "anthropic-version": "2023-06-01"})
                        response = conn.getresponse()
                        if response.status != 200:
                            self.fail(f"local request status={response.status} "
                                      f"body={response.read(4096)!r} mock_calls={calls!r}")
                        if stream:
                            first = b""
                            while not first.endswith(b"\n\n"):
                                line = response.readline()
                                self.assertTrue(line)
                                first += line
                            gate.set()  # Upstream cannot send the rest until first event arrives.
                            data = first + response.read()
                            self.assertIn(b"event:", data)
                        else:
                            data = response.read()
                        self.assertIn(b"fixture reply", data)
                    finally:
                        gate.set()
                        conn.close()
                request(False)
                config["supplies"][0]["target_model"] = "fake-model-reloaded"
                self.config.write_text(json.dumps(config))
                result, out, err = self._run("reload")
                self.assertEqual(result, 0, err)
                request(True)
                self.assertEqual(calls, [("/v1/messages", "fake-upstream", "fake-model", False),
                                         ("/v1/messages", "fake-upstream", "fake-model-reloaded", True)])
                result, out, err = self._run("off")
                self.assertEqual(result, 0, err)
        finally:
            gate.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
