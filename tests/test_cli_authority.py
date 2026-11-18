"""Darwin observation tests; every file and process is under a fake HOME."""
from __future__ import annotations

import ctypes
import contextlib
import fcntl
import io
import ipaddress
import json
import os
import pty
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

_ISOLATION = Path(os.environ.get("MODEL_PROXY_TEST_ISOLATION", "/invalid")).resolve()
if (Path(__file__).resolve().parents[1] != _ISOLATION / "project"
        or not (_ISOLATION / ".model-proxy-isolation").is_file()
        or not Path(os.environ.get("HOME", "/invalid")).resolve().is_relative_to(_ISOLATION)):
    raise RuntimeError("an isolated source snapshot and fake HOME are required")
sys.path.insert(0, str(_ISOLATION / "project"))

from core.cli import authority as authority_module
from core.cli.authority import (PartialMacInstallationAuthority, _capture, locked_observation,
                                observe_lock_conflict, MacAuthorityLease)
from core.cli.control import (MacControl, _valid_record, INSTALL_INITIALIZED,
                              INSTALL_UNINITIALIZED_OR_RECORD_LOST, ControlError, Instance)
from core import discovery, platform_process
from core.config_snapshot import ConfigSnapshotError
from core.darwin_acl import ACLVerificationError
from core.discovery import (
    PreflightError,
    initialize_installation,
    inspect_runtime_identities,
    process_executable,
    process_start_identity,
    write_instance_record,
)
from core.platform_lock import LockError, acquire_lock, verify_local_access
from core.runtime_paths import PATH_KEYS
from core.listen_config import resolve_listen


_ENV = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1"}
_LOCK_CHILD = r"""
import fcntl, os, sys
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
print(f.fileno(), flush=True)
for line in sys.stdin:
    if line.strip() == "unlock":
        fcntl.flock(f, fcntl.LOCK_UN)
        print("unlocked", flush=True)
    elif line.strip() == "quit":
        break
"""
_WAIT_CHILD = "import sys; print('ready',flush=True); sys.stdin.readline()"


class Child:
    def __init__(self, code, *args):
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", code, *map(str, args)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_ENV,
        )
        self.first_line = self.process.stdout.readline().strip()
        if not self.first_line:
            raise AssertionError("test child did not start")

    def command(self, value):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()
        return self.process.stdout.readline().strip()

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write("quit\n")
            self.process.stdin.flush()
        self.process.stdin.close()
        result = self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()
        if result != 0:
            raise AssertionError(f"test child exited {result}")


@unittest.skipUnless(sys.platform == "darwin", "Darwin evidence only")
class AuthorityObservationTests(unittest.TestCase):
    def setUp(self):
        resolve = socket.getaddrinfo
        def local_numeric_only(host, port, family=0, type=0, proto=0, flags=0):
            if host not in ("127.0.0.1", "::1", "0.0.0.0", "::"):
                raise AssertionError("external or named resolution is forbidden in this test")
            return resolve(host, port, family, type, proto,
                           flags | socket.AI_NUMERICHOST | socket.AI_NUMERICSERV)
        resolver = mock.patch.object(socket, "getaddrinfo", side_effect=local_numeric_only)
        resolver.start()
        self.addCleanup(resolver.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="authority-", dir=_ISOLATION)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "installation"
        self.root.mkdir(mode=0o700)
        self.state = self.root / ".runtime"
        self.state.mkdir(mode=0o700)
        (self.root / "config").mkdir(mode=0o700)
        self.paths = {key: str(self.root / ("fake-" + key)) for key in PATH_KEYS}
        runtime = self.root / "config" / "runtime_paths.json"
        runtime.write_text(json.dumps(self.paths), encoding="utf-8")
        runtime.chmod(0o600)
        config = self.root / "config" / "model_proxy_config.json"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.assertNotEqual(self.port, 18889)
        config.write_text(json.dumps({"admin_token": "fake-only-test-token",
                                      "listen": {"host": "127.0.0.1", "port": self.port}}),
                          encoding="utf-8")
        config.chmod(0o600)
        self.config = config

    @contextlib.contextmanager
    def _terminal(self, answers):
        with contextlib.ExitStack() as stack:
            master, slave = pty.openpty()
            stack.callback(os.close, master)
            stack.callback(os.close, slave)
            stdin = stack.enter_context(os.fdopen(os.dup(slave), "r"))
            stdout = stack.enter_context(os.fdopen(os.dup(slave), "w"))
            os.write(master, answers.encode())
            yield stdin, stdout

    def _initialize(self, anchor, legacy, listener):
        host, port = listener.getsockname()[:2]
        listen = {"host": host, "port": port}
        self.config.write_text(json.dumps({"admin_token": "fake-only-test-token",
                                           "listen": listen}), encoding="utf-8")
        runtime = self.root / "config" / "runtime_paths.json"
        config_snapshot = _capture(self.config, private=True)
        runtime_snapshot = _capture(runtime, private=False)
        def recheck():
            if (_capture(self.config, private=True) != config_snapshot
                    or _capture(runtime, private=False) != runtime_snapshot):
                raise PreflightError("preflight_changed", "Test fixture configuration changed")
            current = resolve_listen(self.config)
            if (current["host"], current["port"]) != (host, port):
                raise PreflightError("preflight_changed", "Test fixture listener changed")
        return initialize_installation(
            anchor, legacy, listener, listen=listen, precommit_check=recheck,
            source="temporary isolated test fixture", confirmed=True,
        )

    def test_lan_initialization_bind_is_mocked_and_targets_local_address(self):
        local = {ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("fd00::10")}
        with mock.patch.object(platform_process, "_darwin_interface_addresses", return_value=local), \
                mock.patch("socket.socket") as factory, \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS prohibited")):
            fake = factory.return_value.__enter__.return_value
            with authority_module._bound_listener({"host": "192.0.2.10", "port": 32001}):
                fake.bind.assert_called_with(("192.0.2.10", 32001))
            fake.reset_mock()
            with authority_module._bound_listener({"host": "fd00::10", "port": 32001}):
                fake.bind.assert_called_with(("fd00::10", 32001))
            fake.reset_mock()
            with authority_module._bound_listener({"host": "0.0.0.0", "port": 32001}):
                fake.bind.assert_called_with(("0.0.0.0", 32001))
            fake.reset_mock()
            with authority_module._bound_listener({"host": "0:0:0:0:0:0:0:0", "port": 32001}):
                fake.bind.assert_called_with(("::", 32001))
                fake.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            fake.reset_mock()
            with authority_module._bound_listener({"host": "::", "port": 32001}):
                fake.bind.assert_called_with(("::", 32001))
                fake.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)

    def test_absence_and_fresh_install_are_observations_not_authority(self):
        with locked_observation(self.root, self.paths) as observation:
            self.assertIsNone(observation.record_set())
            self.assertEqual(observation.records(), (None, None))
            self.assertFalse(hasattr(observation, "verify_active"))
            self.assertFalse(hasattr(observation, "admin_token"))
            self.assertFalse(hasattr(observation, "assert_startable"))
            snapshot = inspect_runtime_identities(self.root, self.paths)
            with acquire_lock(snapshot, "anchor.lock", create=True,
                              access_verifier=verify_local_access) as anchor:
                with acquire_lock(anchor.snapshot, "lock", create=True,
                                  access_verifier=verify_local_access, anchor=anchor) as legacy:
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        install = self._initialize(anchor, legacy, listener)
                        result = observation.record_set()
                        self.assertEqual(result["install"], install)
                        self.assertIsNone(result["active"])
                        self.assertIsNone(result["last_good_paths"])
        with self.assertRaises(PreflightError):
            observation.records()

    def test_partial_record_is_unknown_not_fresh_install(self):
        (self.state / "active.json").write_text('{"bad": true}', encoding="utf-8")
        (self.state / "active.json").chmod(0o600)
        with locked_observation(self.root, self.paths) as observation:
            with self.assertRaises(PreflightError):
                observation.records()

    def test_missing_state_interactive_initialization_and_lock_release(self):
        self.state.rmdir()
        callbacks = []
        def track_precommit(*args, **kwargs):
            callback = kwargs["precommit_check"]
            def run_callback():
                callback()
                callbacks.append("passed")
            kwargs["precommit_check"] = run_callback
            return initialize_installation(*args, **kwargs)
        with locked_observation(self.root, self.paths, self.config) as observation:
            self.assertEqual(observation.installation_state(),
                             INSTALL_UNINITIALIZED_OR_RECORD_LOST)
            with self._terminal("isolated fresh install\nYES\n") as (stdin, stdout):
                with mock.patch("core.cli.authority.initialize_installation",
                                side_effect=track_precommit):
                    self.assertTrue(observation.initialize_interactively(stdin, stdout))
            self.assertEqual(callbacks, ["passed"])
            self.assertEqual(observation.installation_state(), INSTALL_INITIALIZED)
            records = observation.record_set()
            self.assertFalse(records["install"]["ever_started"])
            self.assertEqual(records["install"]["initialization_source"], "isolated fresh install")
            self.assertIsNone(records["active"])
            self.assertIsNone(records["last_good_paths"])
            snapshot = inspect_runtime_identities(self.root, self.paths)
            with acquire_lock(snapshot, "anchor.lock", access_verifier=verify_local_access) as anchor:
                with acquire_lock(anchor.snapshot, "lock", access_verifier=verify_local_access,
                                  anchor=anchor):
                    pass
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", self.port))

    def test_initialization_requires_real_tty_and_explicit_yes(self):
        with locked_observation(self.root, self.paths, self.config) as observation:
            with self.assertRaises(PreflightError):
                observation.initialize_interactively(io.StringIO("fake\nYES\n"), io.StringIO())
            for answers in ("fake\nNO\n", "\nYES\n", "fake\nyes\n"):
                with self.subTest(answers=answers), self._terminal(answers) as (stdin, stdout):
                    self.assertFalse(observation.initialize_interactively(stdin, stdout))
                    self.assertIsNone(observation.record_set())

    def test_real_maccontrol_runs_tty_initialization_but_never_spawns_unverified_child(self):
        out, err = io.StringIO(), io.StringIO()
        authority = PartialMacInstallationAuthority()
        self.state.rmdir()
        with self._terminal("isolated source\nYES\n") as (stdin, stdout):
            control = MacControl(self.root, config_path=self.config, authority=authority,
                                 terminal_in=stdin, terminal_out=stdout, out=out, err=err)
            with mock.patch("core.cli.control.subprocess.Popen") as spawn, \
                    mock.patch("core.platform_process.signal_verified") as signal, \
                    mock.patch("core.platform_process.verify_connected_peer") as peer:
                self.assertEqual(control.on(), 1)
                self.assertIn("installation_identity_unknown", err.getvalue())
                self.assertEqual(control.on(), 1)
                self.assertEqual(control.off(), 1)
                self.assertEqual(control.status(), 1)
                self.assertEqual(control.reload(), 1)
                spawn.assert_not_called()
                signal.assert_not_called()
                peer.assert_not_called()
        self.assertNotIn("fake-only-test-token", out.getvalue() + err.getvalue())
        with authority.locked(self.root, self.config) as lease:
            self.assertEqual(lease.installation_state(), INSTALL_INITIALIZED)
            self.assertEqual(lease.records(), (None, None))

    def test_uninitialized_anchor_conflict_and_listener_conflict_write_no_record(self):
        anchor = self.state / "anchor.lock"
        anchor.touch(mode=0o600)
        child = Child(_LOCK_CHILD, anchor)
        with locked_observation(self.root, self.paths, self.config) as observation:
            with self.assertRaises(LockError) as caught:
                observation.installation_state()
            self.assertEqual(caught.exception.code, "lock_conflict")
            self.assertIsNone(observation.record_set())
        child.close()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.port))
            occupied.listen(1)
            with locked_observation(self.root, self.paths, self.config) as observation:
                with self._terminal("fake\nYES\n") as (stdin, stdout):
                    with self.assertRaises(OSError):
                        observation.initialize_interactively(stdin, stdout)
                self.assertIsNone(observation.record_set())

    def test_legacy_pid_or_locked_legacy_refuses_before_record(self):
        pid = Path(self.paths["pid"])
        pid.write_text("987654\n", encoding="ascii")  # Fabricated; never probed.
        with locked_observation(self.root, self.paths, self.config) as observation:
            with self._terminal("fake\nYES\n") as (stdin, stdout):
                with self.assertRaises(PreflightError):
                    observation.initialize_interactively(stdin, stdout)
            self.assertIsNone(observation.record_set())
        pid.unlink()
        legacy = Path(self.paths["lock"])
        legacy.touch(mode=0o600)
        child = Child(_LOCK_CHILD, legacy)
        self.addCleanup(child.close)
        with locked_observation(self.root, self.paths, self.config) as observation:
            with self._terminal("fake\nYES\n") as (stdin, stdout):
                with self.assertRaises(LockError) as caught:
                    observation.initialize_interactively(stdin, stdout)
                self.assertEqual(caught.exception.code, "lock_conflict")
            self.assertIsNone(observation.record_set())

    def test_config_change_during_confirmation_never_writes_record(self):
        config = self.config
        with locked_observation(self.root, self.paths, config) as observation:
            with self._terminal("fake\nYES\n") as (stdin, stdout):
                class ChangingInput:
                    def __init__(self, stream):
                        self.stream, self.count = stream, 0
                    def fileno(self):
                        return self.stream.fileno()
                    def readline(self, limit):
                        value = self.stream.readline(limit)
                        self.count += 1
                        if self.count == 1:
                            config.write_text(json.dumps({"admin_token": "changed-fake-token",
                                                          "listen": {"host": "127.0.0.1",
                                                                     "port": self_port}}))
                        return value
                self_port = self.port
                with self.assertRaises(PreflightError):
                    observation.initialize_interactively(ChangingInput(stdin), stdout)
            self.assertIsNone(observation.record_set())

    def test_changes_after_confirmation_at_core_precommit_never_write_record(self):
        runtime = self.root / "config" / "runtime_paths.json"
        config_before = self.config.read_bytes()
        runtime_before = runtime.read_bytes()

        def change_business():
            self.config.write_text(json.dumps({
                "admin_token": "changed-fake-token",
                "listen": {"host": "127.0.0.1", "port": self.port},
            }), encoding="utf-8")

        def change_runtime():
            paths = dict(self.paths, log=str(self.root / "other-fake-log"))
            runtime.write_text(json.dumps(paths), encoding="utf-8")

        def change_listener():
            self.config.write_text(json.dumps({
                "admin_token": "fake-only-test-token",
                "listen": {"host": "127.0.0.1",
                           "port": 30001 if self.port != 30001 else 30002},
            }), encoding="utf-8")

        for name, mutation in (("business", change_business), ("runtime", change_runtime),
                               ("listener", change_listener), ("bound socket", None)):
            with self.subTest(input=name):
                calls = []
                def race_at_precommit(*args, **kwargs):
                    callback = kwargs["precommit_check"]
                    self.assertTrue(callable(callback))
                    def run_callback():
                        calls.append("core-precommit")
                        if mutation is None:
                            args[2].close()  # Only this test's bound temporary listener.
                        else:
                            mutation()
                        callback()
                    kwargs["precommit_check"] = run_callback
                    return initialize_installation(*args, **kwargs)
                with locked_observation(self.root, self.paths, self.config) as observation:
                    with self._terminal("fake source\nYES\n") as (stdin, stdout):
                        with mock.patch("core.cli.authority.initialize_installation",
                                        side_effect=race_at_precommit):
                            with self.assertRaises(PreflightError):
                                observation.initialize_interactively(stdin, stdout)
                    self.assertEqual(calls, ["core-precommit"])
                    self.assertIsNone(observation.record_set())
                    self.assertFalse((self.state / "install.json").exists())
                    self.assertFalse((self.state / "active.json").exists())
                    self.assertFalse((self.state / "last_good_paths.json").exists())
                self.config.write_bytes(config_before)
                runtime.write_bytes(runtime_before)

    def test_ledger_change_during_confirmation_never_writes_record(self):
        ledger = Path(self.paths["totals"])
        ledger.write_text('{"fake":1}', encoding="utf-8")
        ledger.chmod(0o600)
        with locked_observation(self.root, self.paths, self.config) as observation:
            with self._terminal("fake\nYES\n") as (stdin, stdout):
                class ChangingInput:
                    def __init__(self, stream):
                        self.stream, self.count = stream, 0
                    def fileno(self):
                        return self.stream.fileno()
                    def readline(self, limit):
                        value = self.stream.readline(limit)
                        self.count += 1
                        if self.count == 1:
                            ledger.write_text('{"fake":2}', encoding="utf-8")
                        return value
                with self.assertRaises(PreflightError):
                    observation.initialize_interactively(ChangingInput(stdin), stdout)
            self.assertIsNone(observation.record_set())

    def test_change_after_precommit_rolls_back_only_own_install_record(self):
        config_before = self.config.read_bytes()
        runtime = self.root / "config" / "runtime_paths.json"
        runtime_before = runtime.read_bytes()
        real_write = discovery.write_instance_record

        def mutate_business():
            self.config.write_text(json.dumps({
                "admin_token": "changed-fake-token",
                "listen": {"host": "127.0.0.1", "port": self.port},
            }), encoding="utf-8")

        def mutate_runtime():
            runtime.write_text(json.dumps(dict(self.paths, log=str(self.root / "other-fake-log"))),
                               encoding="utf-8")

        for name, mutation in (("business", mutate_business), ("runtime", mutate_runtime)):
            for phase in ("before-write", "after-write"):
                with self.subTest(input=name, phase=phase):
                    writes = []
                    def write_then_change(state, record_name, record, **kwargs):
                        if record_name == "install.json" and phase == "before-write":
                            mutation()
                        result = real_write(state, record_name, record, **kwargs)
                        if record_name == "install.json":
                            writes.append("installed")
                            if phase == "after-write":
                                mutation()
                        return result
                    with locked_observation(self.root, self.paths, self.config) as observation:
                        with self._terminal("fake source\nYES\n") as (stdin, stdout):
                            with mock.patch("core.discovery.write_instance_record",
                                            side_effect=write_then_change):
                                with self.assertRaises(PreflightError):
                                    observation.initialize_interactively(stdin, stdout)
                        self.assertEqual(writes, ["installed"])
                        self.assertIsNone(observation.record_set())
                        self.assertFalse((self.state / "install.json").exists())
                    self.config.write_bytes(config_before)
                    runtime.write_bytes(runtime_before)

    def test_postcommit_cleanup_failure_reports_unknown_and_preserves_record(self):
        real_write = discovery.write_instance_record
        def write_then_change(state, record_name, record, **kwargs):
            result = real_write(state, record_name, record, **kwargs)
            if record_name == "install.json":
                self.config.write_text(json.dumps({
                    "admin_token": "changed-fake-token",
                    "listen": {"host": "127.0.0.1", "port": self.port},
                }), encoding="utf-8")
            return result
        with locked_observation(self.root, self.paths, self.config) as observation:
            with self._terminal("fake source\nYES\n") as (stdin, stdout):
                with mock.patch("core.discovery.write_instance_record", side_effect=write_then_change), \
                        mock.patch("core.cli.authority.delete_instance_record_if_matches",
                                   side_effect=PreflightError("test_cleanup", "synthetic failure")):
                    with self.assertRaises(PreflightError) as caught:
                        observation.initialize_interactively(stdin, stdout)
            self.assertEqual(caught.exception.code, "initialization_cleanup_failed")
            self.assertIsNotNone(observation.record_set())
            self.assertTrue((self.state / "install.json").exists())

    def test_insecure_business_config_fails_before_state_or_lock_creation(self):
        self.state.rmdir()
        self.config.chmod(0o644)
        with self.assertRaises(PreflightError):
            with locked_observation(self.root, self.paths, self.config):
                self.fail("non-private token-bearing configuration accepted")
        self.assertFalse(self.state.exists())

    def test_writable_runtime_config_fails_before_state_creation(self):
        self.state.rmdir()
        runtime = self.root / "config" / "runtime_paths.json"
        runtime.chmod(0o666)
        with self.assertRaises(PreflightError):
            with locked_observation(self.root, self.paths, self.config):
                self.fail("unprotected runtime configuration accepted")
        self.assertFalse(self.state.exists())

    def test_overlapping_or_linked_lock_is_rejected_before_creation(self):
        # `.runtime/` is the shared container, so a role inside it is legal
        # (all six defaults live there). Overlap means a role *is* the
        # container, or aliases another protected role.
        # A role pointing at the container itself must be rejected. The log
        # role must be a file, so the file/type check fires before the
        # state-directory overlap check; either code is a fail-closed refusal.
        overlap = dict(self.paths, log=str(self.state))
        with self.assertRaises(PreflightError) as caught:
            with locked_observation(self.root, overlap):
                self.fail("overlapping runtime target was accepted")
        self.assertIn(caught.exception.code, {"invalid_type", "state_conflict"})
        self.assertFalse((self.state / "control.lock").exists())
        (self.state / "control.lock").symlink_to(self.root / "fake-log")
        with self.assertRaises(PreflightError):
            with locked_observation(self.root, self.paths):
                self.fail("linked fixed control lock was accepted")

    def test_control_lock_serializes_and_replacement_is_detected(self):
        with locked_observation(self.root, self.paths) as observation:
            with self.assertRaises(LockError) as caught:
                with locked_observation(self.root, self.paths):
                    self.fail("second fixed control lock unexpectedly acquired")
            self.assertEqual(caught.exception.code, "lock_conflict")
            control = self.state / "control.lock"
            control.rename(self.state / "old-control.lock")
            control.write_bytes(b"")
            control.chmod(0o600)
            with self.assertRaises(PreflightError):
                observation.records()
            # Context-exit verification must also reject the replacement.
            with self.assertRaises(PreflightError):
                observation._check()
            control.unlink()
            (self.state / "old-control.lock").rename(control)

    def test_transient_protection_change_rechecks_but_permission_denial_does_not(self):
        with locked_observation(self.root, self.paths) as observation:
            real_verify = authority_module.verify_record_directory
            attempts = []
            def once_changed(path):
                attempts.append("check")
                if len(attempts) == 1:
                    raise PreflightError("protection_changed", "synthetic transient")
                return real_verify(path)
            with mock.patch.object(authority_module, "verify_record_directory",
                                   side_effect=once_changed):
                self.assertEqual(observation.records(), (None, None))
            self.assertGreaterEqual(len(attempts), 2)
            with mock.patch.object(authority_module, "verify_record_directory",
                                   side_effect=PreflightError("protection_unavailable", "denied")) as denied:
                with self.assertRaises(PreflightError) as caught:
                    observation.records()
                self.assertEqual(caught.exception.code, "protection_unavailable")
                self.assertEqual(denied.call_count, 1)

    def test_config_capture_retries_changed_identity_but_not_acl_failure(self):
        original = authority_module.mac_private_protection
        calls = []
        def transient(path, info):
            calls.append("check")
            if len(calls) == 1:
                raise authority_module.SecureSnapshotError("identity_changed")
            return original(path, info)
        with mock.patch.object(authority_module, "mac_private_protection", side_effect=transient):
            self.assertTrue(_capture(self.config, private=True).exists)
        self.assertGreater(len(calls), 1)
        with mock.patch.object(authority_module, "mac_private_protection",
                               side_effect=authority_module.SecureSnapshotError("acl_unverified")) as denied:
            with self.assertRaises(PreflightError) as caught:
                _capture(self.config, private=True)
            self.assertEqual(caught.exception.code, "config_unverified")
            self.assertEqual(denied.call_count, 1)

    def test_only_proven_acl_snapshot_change_is_retried(self):
        with locked_observation(self.root, self.paths) as observation:
            real_verify = authority_module.verify_record_directory
            calls = []
            def changed_once(path):
                calls.append("check")
                if len(calls) == 1:
                    try:
                        raise ACLVerificationError("acl_changed")
                    except ACLVerificationError:
                        raise PreflightError("protection_unavailable", "ACL unknown") from None
                return real_verify(path)
            with mock.patch.object(authority_module, "verify_record_directory", side_effect=changed_once):
                self.assertEqual(observation.records(), (None, None))
            self.assertGreaterEqual(len(calls), 2)

            for reason in ("acl_unavailable", "identity_unverified", "file_acl_present"):
                with self.subTest(reason=reason):
                    calls.clear()
                    def unavailable(path):
                        calls.append("check")
                        try:
                            raise ACLVerificationError(reason)
                        except ACLVerificationError:
                            raise PreflightError("protection_unavailable", "ACL unknown") from None
                    with mock.patch.object(authority_module, "verify_record_directory", side_effect=unavailable):
                        with self.assertRaises(PreflightError):
                            observation.records()
                    self.assertEqual(len(calls), 1)

    def test_unlinked_file_and_pipe_share_acl_identity_error(self):
        # The error code alone cannot distinguish a replaced record from an
        # unexpected descriptor type. Never treat it as generic retry evidence.
        from core.darwin_acl import read_acl_snapshot
        target = self.state / "fake-record"
        replacement = self.state / "fake-replacement"
        target.write_text("{}")
        replacement.write_text("{}")
        target.chmod(0o600)
        replacement.chmod(0o600)
        fd = os.open(target, os.O_RDONLY)
        try:
            before = os.fstat(fd)
            os.replace(replacement, target)
            after = os.fstat(fd)
            current = target.stat()
            self.assertTrue(stat.S_ISREG(before.st_mode))
            self.assertTrue(stat.S_ISREG(after.st_mode))
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(before.st_nlink, 1)
            self.assertEqual(after.st_nlink, 0)
            self.assertNotEqual((after.st_dev, after.st_ino), (current.st_dev, current.st_ino))
            with self.assertRaises(ACLVerificationError) as caught:
                read_acl_snapshot(fd)
            self.assertEqual(caught.exception.code, "identity_unverified")
        finally:
            os.close(fd)
        read_fd, write_fd = os.pipe()
        try:
            self.assertTrue(stat.S_ISFIFO(os.fstat(read_fd).st_mode))
            with self.assertRaises(ACLVerificationError) as caught:
                read_acl_snapshot(read_fd)
            self.assertEqual(caught.exception.code, "identity_unverified")
        finally:
            os.close(read_fd)
            os.close(write_fd)
        with self.assertRaises(ACLVerificationError) as caught:
            read_acl_snapshot(read_fd)
        self.assertEqual(caught.exception.code, "acl_unavailable")

    def test_record_changed_read_retry_is_bounded(self):
        attempts = []

        def changed_once():
            attempts.append("read")
            if len(attempts) == 1:
                raise PreflightError("record_changed", "test replacement")
            return "fresh read"

        with mock.patch.object(authority_module.time, "sleep") as sleep:
            self.assertEqual(authority_module._read_retry(changed_once), "fresh read")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleep.call_count, 1)

        attempts.clear()

        def always_changed():
            attempts.append("read")
            raise PreflightError("record_changed", "test replacement")

        with mock.patch.object(authority_module.time, "sleep") as sleep:
            with self.assertRaises(PreflightError) as caught:
                authority_module._read_retry(always_changed)
        self.assertEqual(caught.exception.code, "record_changed")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(sleep.call_count, 3)

    def test_config_capture_retries_only_explicit_acl_snapshot_change(self):
        original = authority_module.mac_private_protection
        calls = []
        def changed_once(path, info):
            calls.append("check")
            if len(calls) == 1:
                raise authority_module.SecureSnapshotError("acl_changed")
            return original(path, info)
        with mock.patch.object(authority_module, "mac_private_protection", side_effect=changed_once):
            self.assertTrue(_capture(self.config, private=True).exists)
        self.assertGreaterEqual(len(calls), 2)
        with mock.patch.object(authority_module, "mac_private_protection",
                               side_effect=authority_module.SecureSnapshotError("acl_unavailable")) as denied:
            with self.assertRaises(PreflightError):
                _capture(self.config, private=True)
            self.assertEqual(denied.call_count, 1)

    def test_retry_refuses_record_published_by_exited_child(self):
        nonce = "a" * 32
        record = {"active": {"pid": 12345, "instance_nonce": nonce}}
        observation = mock.Mock()
        observation.record_set.return_value = record
        lease = MacAuthorityLease(observation)
        lease.watch_start(12345, nonce)
        lease._launch_baseline = (None, None, None, None, record)
        with mock.patch.object(lease, "assert_startable") as startable:
            self.assertFalse(lease.retry_exited_child(12345, nonce, 1))
            startable.assert_not_called()

    def _mock_stale_lease(self, *, changed_path=False):
        paths = dict(self.paths)
        recorded = dict(paths)
        if changed_path:
            paths["totals"] = str(self.root / "new-fake-totals")
        active = {"runtime_paths": recorded, "pid": 12345, "instance_nonce": "a" * 32}
        good = {"fake": "last-good"}
        records = {"install": {"fake": "install"}, "active": active, "last_good_paths": good}
        obs = mock.Mock()
        obs._root = self.root
        obs._paths = paths
        obs._config_path = self.config
        obs.record_set.return_value = records
        lease = MacAuthorityLease(obs)
        instance = Instance(12345, {"kind": "fake"}, "/fake/python", "a" * 32,
                            "127.0.0.1", 32001, active, good)
        # The restart preflight requires the real identity and writable open
        # of an existing log, not a fabricated canonical-path-only object.
        log_file = Path(paths["log"])
        log_file.write_bytes(b"fake isolated test log\n")
        log_file.chmod(0o600)
        identities = inspect_runtime_identities(self.root, paths).as_dict()
        self.assertTrue(identities["log"].exists)
        self.assertEqual((identities["log"].device, identities["log"].inode),
                         (log_file.stat().st_dev, log_file.stat().st_ino))
        snapshot = mock.Mock()
        snapshot.as_dict.return_value = identities
        listen = {"host": "127.0.0.1", "port": 32002}
        # A real protected fake snapshot exercises the server's startup
        # business-config validator; opaque object() is no longer evidence.
        self.config.write_text(json.dumps({
            "admin_token": "fake-only-test-token", "listen": listen,
            "supplies": [{"id": "fake-supply", "appkey": "fake-only-upstream-key",
                          "extra_headers": {"x-fake-header": "fake-value"}}],
            "routes": [], "strategies": [],
        }), encoding="utf-8")
        self.config.chmod(0o600)
        config = authority_module._capture_business(self.config)
        runtime = _capture(self.root / "config" / "runtime_paths.json", private=False)
        return lease, instance, records, snapshot, config, runtime, listen

    def test_listen_only_stale_lease_checks_old_port_without_mutating_records(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        entry = mock.Mock(exists=True)
        def capture(path, *, private):
            return config if Path(path) == self.config else runtime if Path(path).name == "runtime_paths.json" else entry
        with mock.patch.object(authority_module, "_valid_record", return_value=instance), \
                mock.patch.object(lease, "_configuration", return_value=(config, runtime, listen, snapshot)), \
                mock.patch.object(lease, "_locks_free") as locks, \
                mock.patch.object(authority_module, "_capture", side_effect=capture), \
                mock.patch.object(platform_process, "process_gone", return_value=True), \
                mock.patch.object(MacControl, "_port_free") as port:
            self.assertTrue(lease.assert_startable())
            locks.assert_called_once_with(records)
            port.assert_called_once_with("127.0.0.1", 32001)
            self.assertEqual(lease._observation.record_set.return_value, records)

    def test_runtime_drift_is_explicitly_rejected_before_process_probe(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease(changed_path=True)
        entry = mock.Mock(exists=True)
        with mock.patch.object(authority_module, "_valid_record", return_value=instance), \
                mock.patch.object(lease, "_configuration", return_value=(config, runtime, listen, snapshot)), \
                mock.patch.object(authority_module, "_capture", return_value=entry), \
                mock.patch.object(authority_module, "_capture_restart_ledger") as ledger, \
                mock.patch.object(platform_process, "process_gone") as gone, \
                mock.patch.object(platform_process, "signal_verified") as signal:
            with self.assertRaises(ControlError) as caught:
                lease.assert_startable()
            self.assertEqual(caught.exception.code, "configuration_migration_required")
            self.assertIn("runtime paths changed", caught.exception.detail)
            self.assertFalse(lease.assert_restart_supported())
            ledger.assert_not_called()
            gone.assert_not_called()
            signal.assert_not_called()

    def test_restart_preflights_new_listener_without_signalling(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        with mock.patch.object(authority_module, "_valid_record", return_value=instance), \
                mock.patch.object(lease, "_configuration", return_value=(config, runtime, listen, snapshot)), \
                mock.patch.object(lease, "_preflight_restart_ledger") as ledger, \
                mock.patch.object(MacControl, "_port_free") as port, \
                mock.patch.object(platform_process, "signal_verified") as signal:
            self.assertTrue(lease.assert_restart_supported())
            ledger.assert_called_once_with((config, runtime, listen, snapshot), records)
            port.assert_called_once_with("127.0.0.1", 32002)
            self.assertEqual(lease._restart_baseline, (config, runtime, listen))
            signal.assert_not_called()

    def test_restart_invalid_business_config_blocks_preflight_before_signal(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        value = json.loads(config.contents.decode("utf-8"))
        value["supplies"][0]["extra_headers"] = ["invalid-type"]
        self.config.write_text(json.dumps(value), encoding="utf-8")
        self.config.chmod(0o600)
        bad_config = _capture(self.config, private=True)
        lease._restart_baseline = ("old", "old", "old")
        lease._restart_records = {"fake": "old"}
        with mock.patch.object(authority_module, "_valid_record", return_value=instance), \
                mock.patch.object(lease, "_configuration",
                                  return_value=(bad_config, runtime, listen, snapshot)), \
                mock.patch.object(authority_module, "_capture_restart_ledger") as ledger, \
                mock.patch.object(MacControl, "_port_free") as port, \
                mock.patch.object(platform_process, "signal_verified") as signal:
            with self.assertRaises(PreflightError) as caught:
                lease.assert_restart_supported()
            self.assertEqual(caught.exception.code, "config_unverified")
            self.assertNotIn("fake-only-upstream-key", str(caught.exception))
            ledger.assert_not_called()
            port.assert_not_called()
            signal.assert_not_called()
            self.assertIsNone(lease._restart_baseline)
            self.assertIsNone(lease._restart_records)

    def test_restart_ledger_preflight_retries_only_proven_instability(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        current = (config, runtime, listen, snapshot)
        with mock.patch.object(lease, "_configuration", return_value=current), \
                mock.patch.object(authority_module, "_capture_restart_ledger",
                    side_effect=[PreflightError("ledger_changed", "fake change"), None]) as capture, \
                mock.patch.object(authority_module.time, "sleep") as sleep:
            lease._preflight_restart_ledger(current, records)
            self.assertEqual(capture.call_count, 2)
            sleep.assert_called_once_with(0.01)
            self.assertTrue(all(call.args == (snapshot,) for call in capture.call_args_list))

        with mock.patch.object(lease, "_configuration", return_value=current), \
                mock.patch.object(authority_module, "_capture_restart_ledger",
                    side_effect=PreflightError("ledger_changed", "fake change")) as capture, \
                mock.patch.object(authority_module.time, "sleep") as sleep:
            with self.assertRaises(PreflightError) as caught:
                lease._preflight_restart_ledger(current, records)
            self.assertEqual(caught.exception.code, "ledger_changed")
            self.assertEqual(capture.call_count, 4)
            self.assertEqual(sleep.call_count, 3)

        calls = []
        def unstable_then_stable(snapshot_arg):
            calls.append(snapshot_arg)
            if len(calls) == 1:
                try:
                    raise ConfigSnapshotError("config_unstable")
                except ConfigSnapshotError:
                    raise PreflightError("ledger_unverified", "fake unstable read") from None
            return None
        with mock.patch.object(lease, "_configuration", return_value=current), \
                mock.patch.object(authority_module, "_capture_restart_ledger",
                                  side_effect=unstable_then_stable), \
                mock.patch.object(authority_module.time, "sleep") as sleep:
            lease._preflight_restart_ledger(current, records)
            self.assertEqual(calls, [snapshot, snapshot])
            sleep.assert_called_once_with(0.01)

    def test_restart_ledger_preflight_rechecks_state_after_capture(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        current = (config, runtime, listen, snapshot)
        with mock.patch.object(lease, "_configuration",
                               return_value=(object(), runtime, listen, snapshot)), \
                mock.patch.object(authority_module, "_capture_restart_ledger", return_value=None):
            with self.assertRaises(ControlError) as caught:
                lease._preflight_restart_ledger(current, records)
            self.assertEqual(caught.exception.code, "start_state_changed")

        changed_records = dict(records, active={"fake": "different"})
        lease._observation.record_set.side_effect = [records, changed_records]
        with mock.patch.object(lease, "_configuration", return_value=current), \
                mock.patch.object(authority_module, "_capture_restart_ledger", return_value=None):
            with self.assertRaises(ControlError) as caught:
                lease._preflight_restart_ledger(current, records)
            self.assertEqual(caught.exception.code, "instance_changed")

    def test_restart_ledger_preflight_retries_fresh_replaced_identity(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        old_ids = dict(snapshot.as_dict())
        old_ids["totals"] = SimpleNamespace(canonical=self.paths["totals"], inode=1)
        snapshot.as_dict.return_value = old_ids
        new_snapshot = mock.Mock()
        new_ids = dict(old_ids)
        new_ids["totals"] = SimpleNamespace(canonical=self.paths["totals"], inode=2)
        new_snapshot.as_dict.return_value = new_ids
        old = (config, runtime, listen, snapshot)
        new = (config, runtime, listen, new_snapshot)
        with mock.patch.object(lease, "_configuration", return_value=new), \
                mock.patch.object(authority_module, "_capture_restart_ledger",
                                  return_value=None) as capture, \
                mock.patch.object(authority_module.time, "sleep") as sleep:
            lease._preflight_restart_ledger(old, records)
            self.assertEqual(capture.call_args_list, [mock.call(snapshot), mock.call(new_snapshot)])
            sleep.assert_called_once_with(0.01)

    def test_restart_ledger_preflight_never_retries_unknown_or_acl_failure(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        current = (config, runtime, listen, snapshot)
        for code in ("ledger_unverified", "protection_unavailable", "filesystem_unverified"):
            with self.subTest(code=code), \
                    mock.patch.object(lease, "_configuration", return_value=current), \
                    mock.patch.object(authority_module, "_capture_restart_ledger",
                        side_effect=PreflightError(code, "fake denial")) as capture, \
                    mock.patch.object(authority_module.time, "sleep") as sleep:
                with self.assertRaises(PreflightError) as caught:
                    lease._preflight_restart_ledger(current, records)
                self.assertEqual(caught.exception.code, code)
                capture.assert_called_once_with(snapshot)
                sleep.assert_not_called()

        calls = []
        def unavailable(snapshot_arg):
            calls.append(snapshot_arg)
            try:
                raise ConfigSnapshotError("filesystem_unverified")
            except ConfigSnapshotError:
                raise PreflightError("ledger_unverified", "fake filesystem denial") from None
        with mock.patch.object(lease, "_configuration", return_value=current), \
                mock.patch.object(authority_module, "_capture_restart_ledger", side_effect=unavailable), \
                mock.patch.object(authority_module.time, "sleep") as sleep:
            with self.assertRaises(PreflightError):
                lease._preflight_restart_ledger(current, records)
            self.assertEqual(calls, [snapshot])
            sleep.assert_not_called()

    def test_restart_ledger_rejection_clears_old_authorization_baseline(self):
        lease, instance, records, snapshot, config, runtime, listen = self._mock_stale_lease()
        lease._restart_baseline = ("old", "old", "old")
        lease._restart_records = {"fake": "old"}
        with mock.patch.object(authority_module, "_valid_record", return_value=instance), \
                mock.patch.object(lease, "_configuration",
                                  return_value=(config, runtime, listen, snapshot)), \
                mock.patch.object(authority_module, "_capture_restart_ledger",
                    side_effect=PreflightError("ledger_unverified", "fake denial")) as capture, \
                mock.patch.object(MacControl, "_port_free") as port, \
                mock.patch.object(platform_process, "signal_verified") as signal:
            with self.assertRaises(PreflightError):
                lease.assert_restart_supported()
            capture.assert_called_once_with(snapshot)
            port.assert_not_called()
            signal.assert_not_called()
            self.assertIsNone(lease._restart_baseline)
            self.assertIsNone(lease._restart_records)

    def test_restart_ledger_import_does_not_read_config_or_open_network(self):
        # A fresh, independently owned interpreter makes the import check
        # meaningful even if another test has already imported core.server.
        script = r'''
import os, socket, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
forbidden = {
    str(Path(os.environ["MODEL_PROXY_CONFIG"])),
    str(Path(sys.argv[1]) / "config" / "runtime_paths.json"),
    "/tmp/model_proxy.lock", "/tmp/model_proxy.pid",
    "/tmp/model_proxy_ensure.log", "/tmp/model_proxy_start.lock",
}
def guard_path(path):
    if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) in forbidden:
        raise AssertionError("import attempted to access a protected/default path")
def audit(event, args):
    if event == "open" and args:
        guard_path(args[0])
    if event.startswith("socket.") or event == "subprocess.Popen":
        raise AssertionError("import attempted network or subprocess")
sys.addaudithook(audit)
old_stat, old_lstat = os.stat, os.lstat
def checked_stat(path, *args, **kwargs):
    guard_path(path)
    return old_stat(path, *args, **kwargs)
def checked_lstat(path, *args, **kwargs):
    guard_path(path)
    return old_lstat(path, *args, **kwargs)
os.stat, os.lstat = checked_stat, checked_lstat
from core.cli.authority import _capture_restart_ledger
import core.server
assert callable(_capture_restart_ledger)
assert core.server.usage_totals is None
print("safe-import")
'''
        env = dict(_ENV, TMPDIR=str(self.root), MODEL_PROXY_CONFIG=str(self.config),
                   MODEL_PROXY_PORT=str(self.port))
        child = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(_ISOLATION / "project")],
            env=env, cwd=self.root, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "safe-import")

    def test_restart_ledger_missing_server_contract_fails_closed(self):
        unavailable = ModuleType("core.server")
        with mock.patch.dict(sys.modules, {"core.server": unavailable}):
            with self.assertRaises(PreflightError) as caught:
                authority_module._capture_restart_ledger(mock.Mock())
        self.assertEqual(caught.exception.code, "ledger_unverified")

    def test_consistent_records_can_name_non_owner_and_remain_after_exit(self):
        child = Child(_WAIT_CHILD)
        self.addCleanup(child.close)
        self.assertEqual(child.first_line, "ready")
        start = process_start_identity(child.process.pid)
        image = process_executable(child.process.pid)
        self.assertIsNotNone(start)
        self.assertIsNotNone(image)
        with locked_observation(self.root, self.paths) as observation:
            snapshot = inspect_runtime_identities(self.root, self.paths)
            with acquire_lock(snapshot, "anchor.lock", create=True,
                              access_verifier=verify_local_access) as anchor:
                with acquire_lock(anchor.snapshot, "lock", create=True,
                                  access_verifier=verify_local_access, anchor=anchor) as legacy:
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        install = self._initialize(anchor, legacy, listener)
                        addr = listener.getsockname()
                        common = {
                            "schema": 2, "project_root": str(self.root),
                            "install_id": install["install_id"],
                            "instance_nonce": "a" * 32,
                            "listen": {"host": addr[0], "port": addr[1]},
                            "runtime_paths": self.paths,
                        }
                        identities = {}
                        for name, lock in (("anchor", anchor), ("legacy", legacy)):
                            info = os.fstat(lock.fileno())
                            identities[name] = {"device": info.st_dev, "inode": info.st_ino}
                        active = dict(common, record="active", state="READY",
                                      pid=child.process.pid, process_start=start,
                                      executable=image, locks=identities)
                        last = dict(common, record="last_good_paths")
                        updated = dict(install, ever_started=True,
                                       last_instance_nonce=common["instance_nonce"])
                        for name, record in (("last_good_paths.json", last),
                                             ("active.json", active), ("install.json", updated)):
                            write_instance_record(self.state, name, record,
                                                  anchor=anchor, legacy=legacy)
                        # The child owns neither lock despite the consistent PID record.
                        self.assertEqual(observation.record_set()["active"], active)
                        self.assertEqual(observation.records(), (active, last))
                        instance = _valid_record(self.root, active, last)
                        platform_process.check_identity(instance.pid, instance.start,
                                                        instance.executable)
                        self.assertEqual(instance.pid, child.process.pid)
            child.close()
            self.assertTrue(platform_process.process_gone(child.process.pid, start, image))
            # A stale record is still only an observation, not proof of stop.
            self.assertEqual(observation.record_set()["active"], active)

    def test_flock_holder_cannot_be_inferred_from_open_and_previous_lock(self):
        lock = self.root / "fake-probe-lock"
        former_owner = Child(_LOCK_CHILD, lock)
        self.addCleanup(former_owner.close)
        former_fd = int(former_owner.first_line)
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_fd = lib.proc_pidfdinfo
        proc_fd.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_void_p, ctypes.c_int]
        proc_fd.restype = ctypes.c_int

        def flags():
            data = ctypes.create_string_buffer(512)
            count = proc_fd(former_owner.process.pid, former_fd, 1, data, len(data))
            self.assertEqual(count, 176)
            return int.from_bytes(data.raw[:4], sys.byteorder)

        was_locked = 0x4000  # Darwin FWASLOCKED, not "currently owns lock".
        self.assertTrue(flags() & was_locked)
        self.assertEqual(former_owner.command("unlock"), "unlocked")
        self.assertTrue(flags() & was_locked)
        with lock.open("r+") as probe:
            self.assertFalse(observe_lock_conflict(probe.fileno()).blocked)
        actual_owner = Child(_LOCK_CHILD, lock)
        self.addCleanup(actual_owner.close)
        self.assertTrue(actual_owner.first_line.isdigit())
        lsof = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-p", str(former_owner.process.pid), "-Fn"],
            capture_output=True, text=True, timeout=5, env=_ENV, check=True,
        )
        self.assertIn("n" + str(lock), lsof.stdout.splitlines())
        with lock.open("r+") as probe:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for candidate in (None, former_owner.process.pid, actual_owner.process.pid,
                              os.getpid()):
                result = observe_lock_conflict(probe.fileno(), candidate_pid=candidate)
                self.assertTrue(result.blocked)
                self.assertIsNone(result.reported_pid)
        # Open FD + historical flag + a conflicting flock still selected the
        # wrong process; no token, signal, or child start is authorized.


if __name__ == "__main__":
    unittest.main()
