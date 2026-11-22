"""Offline contract tests; no business configuration, processes or ports."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.cli import model_proxy as bootstrap
from core.cli import windows_command_line as cmd
from core.cli import windows_control as wc
from core import discovery
from core.runtime_paths import PATH_KEYS


ROOT = r"C:\fixture\项目 space"
ENTRY = ROOT + r"\core\cli\model_proxy.py"
CONFIG = ROOT + r"\config\fake.json"
PATHS = {key: ROOT + "\\runtime\\" + key for key in PATH_KEYS}
RECORD = {
    "schema": 3, "record": "active", "state": "READY", "project_root": ROOT,
    "pid": 4321, "process_start": {"kind": "windows_filetime", "ticks_100ns": 12345},
    "executable": r"C:\Python\python.exe", "entry": ENTRY, "config": CONFIG,
    "instance_nonce": "a" * 32, "listen": {"host": "127.0.0.1", "port": 12345},
    "runtime_paths": PATHS,
}


class SharedWindowsContractTests(unittest.TestCase):
    def test_explicit_bootstrap_is_validated_before_environment_change(self):
        env = {}
        bootstrap._normalize_windows_launch(["--config", CONFIG, "--port", "12345"], env)
        self.assertEqual(env, {"MODEL_PROXY_CONFIG": CONFIG, "MODEL_PROXY_PORT": "12345"})
        for bad in (["--config", CONFIG, "--port", "0"],
                    ["--config", "relative", "--port", "12345"],
                    ["--config", CONFIG, "--config", CONFIG]):
            env = {}
            with self.assertRaises(ValueError):
                bootstrap._normalize_windows_launch(bad, env)
            self.assertEqual(env, {})

    def test_argv_checks_entry_and_explicit_options_not_raw_quoting(self):
        args = (r"C:\Python\python.exe", "-B", ENTRY, "--config", CONFIG, "--port", "12345")
        cmd.verify_shared_launch_argv(args, entry=ENTRY, config=CONFIG, port=12345)
        for bad in (args[:2] + (ROOT + r"\other.py",) + args[3:],
                    args[:-1] + ("12346",),
                    args + ("--extra",)):
            with self.assertRaises(cmd.CommandLineObservationError):
                cmd.verify_shared_launch_argv(bad, entry=ENTRY, config=CONFIG, port=12345)

    def test_single_active_record_does_not_require_install_or_last_good(self):
        state = ROOT + r"\.runtime"
        self.assertTrue(discovery._valid_windows_active(RECORD, state))
        for key, value in (("entry", ROOT + r"\wrong.py"), ("pid", True),
                           ("process_start", {"kind": "windows_filetime", "ticks_100ns": 0})):
            self.assertFalse(discovery._valid_windows_active(dict(RECORD, **{key: value}), state))

    def test_no_credential_is_sent_when_entry_check_fails(self):
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        peer = Mock()
        peer.__enter__ = Mock(return_value=peer)
        peer.__exit__ = Mock(return_value=False)
        peer.endpoint = SimpleNamespace(family=wc.socket.AF_INET,
                                        peer_ip=b"\x7f\x00\x00\x01",
                                        peer_port=RECORD["listen"]["port"])
        with patch.object(wc.socket, "socket", return_value=sock), \
             patch.object(wc.windows_process, "verify_socket_owner", return_value=peer), \
             patch.object(controller, "_verify_entry", side_effect=wc.WindowsControlError("launch_mismatch")):
            with self.assertRaises(wc.WindowsControlError):
                controller._request(RECORD, PATHS, "reload")
        sock.sendall.assert_not_called()

    def test_request_uses_verified_socket_once(self):
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        peer = Mock()
        peer.__enter__ = Mock(return_value=peer)
        peer.__exit__ = Mock(return_value=False)
        peer.endpoint = SimpleNamespace(family=wc.socket.AF_INET,
                                        peer_ip=b"\x7f\x00\x00\x01",
                                        peer_port=RECORD["listen"]["port"])
        snapshot = Mock(exists=True, contents=json.dumps({"admin_token": "fake-only"}).encode())
        response = Mock(status=200, length=0)
        response.read.return_value = b'{"ok":true}'
        with patch.object(wc.socket, "socket", return_value=sock), \
             patch.object(wc.windows_process, "verify_socket_owner", return_value=peer) as verify, \
             patch.object(controller, "_verify_entry") as entry, \
             patch("core.windows_secure_files.capture_target", return_value=contextlib.nullcontext(snapshot)), \
             patch.object(wc.http.client, "HTTPResponse", return_value=response) as http:
            controller._request(RECORD, PATHS, "reload")
        verify.assert_called_once()
        self.assertIs(verify.call_args.args[0], sock)
        self.assertIs(http.call_args.args[0], sock)
        self.assertEqual(entry.call_count, 2)
        sock.sendall.assert_called_once()
        self.assertIn(b"X-Proxy-Admin-Token: fake-only", sock.sendall.call_args.args[0])

    def test_foreign_occupied_port_does_not_spawn_or_request(self):
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        with patch.object(controller, "_active", return_value=None), \
             patch.object(controller, "_probe_locks"), \
             patch.object(controller, "_probe_port", side_effect=OSError("busy")), \
             patch.object(controller, "_request") as request, \
             patch.object(wc.subprocess, "Popen") as spawn:
            with self.assertRaises(OSError):
                controller._on_locked(PATHS, RECORD["listen"])
        request.assert_not_called()
        spawn.assert_not_called()

    def test_reused_pid_is_never_stopped(self):
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        with patch.object(controller, "_active", return_value=RECORD), \
             patch.object(wc.windows_process, "recorded_process_state", return_value="reused"), \
             patch.object(wc.windows_process, "open_verified_process") as process, \
             patch.object(controller, "_idle_readonly"):
            controller._off_locked(PATHS, RECORD["listen"])
        process.assert_not_called()

    def test_restart_does_not_start_after_stop_failure(self):
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_configuration", return_value=(PATHS, RECORD["listen"])), \
             patch.object(controller, "_locator_hint", return_value=True), \
             patch.object(controller, "_active", return_value=None), \
             patch.object(controller, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(controller, "_off_locked", side_effect=wc.WindowsControlError("release_unverified")) as off, \
             patch.object(controller, "_on_locked") as start:
            self.assertEqual(controller.restart(), 1)
        off.assert_called_once_with(PATHS, RECORD["listen"], held_control=None,
                                    current_listen=RECORD["listen"])
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
