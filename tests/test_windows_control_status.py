"""Status against a synthetic protected Windows filesystem, no real service."""
import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from core import discovery, windows_process
from core.cli import dispatch
from core.cli.windows_control import WindowsControl, WindowsControlError
from core.runtime_paths import PATH_KEYS
import core.platform_windows as win
import core.windows_acl as acl
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class WindowsStatusCLITests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    STATE = ROOT + r"\.runtime"

    def setUp(self):
        self.fs = ConfigFS()
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.paths = {key: r"C:\fixture\runtime" + "\\" + key for key in PATH_KEYS}
        self.config = self.ROOT + r"\config\model_proxy_config.json"
        self.listen = {"host": "127.0.0.1", "port": 19431}
        self.active = {
            "schema": 3, "record": "active", "state": "READY", "project_root": self.ROOT,
            "instance_nonce": "a" * 32, "pid": 4321,
            "process_start": {"kind": "windows_filetime", "ticks_100ns": 12345},
            "executable": r"C:\fixture\python.exe",
            "entry": self.ROOT + r"\core\cli\model_proxy.py", "config": self.config,
            "listen": dict(self.listen), "runtime_paths": dict(self.paths),
        }
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(sys, "platform", "win32"),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
            patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("spawn forbidden")),
            patch.object(os, "kill", side_effect=AssertionError("signal forbidden")),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.control = WindowsControl(self.ROOT, config_path=self.config)

    def place(self, name, record, inode):
        self.fs.objects[self.STATE + "\\" + name] = dict(
            self.fs.obj(inode), data=json.dumps(record).encode())

    def run_cli(self, command="status", *, busy=False):
        out, err = io.StringIO(), io.StringIO()
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_configuration", return_value=(self.paths, self.listen)), \
             patch.object(self.control, "_locator_hint", side_effect=lambda:
                          self.STATE + r"\active.json" in self.fs.objects), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_probe_locks"), \
             patch.object(self.control, "_probe_port",
                          side_effect=WindowsControlError("port_busy") if busy else None), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(self.control, "_request", return_value=({"supplies": [], "routes": [], "strategies": [], "cooldown": {}}, "fake-admin-credential")) as request, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.control.out, self.control.err = out, err
            rc = dispatch.main([command], control=self.control, root=self.ROOT)
        self.assertEqual(self.fs.objects, before)
        self.assertFalse(self.fs.handles)
        self.assertNotIn("SECRET", out.getvalue() + err.getvalue())
        return rc, out.getvalue(), err.getvalue(), request

    def test_absent_record_reports_stopped_only_after_lock_and_port_probes(self):
        rc, out, err, request = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("stopped; locks and port verified free", out)
        self.assertEqual(err, "")
        request.assert_not_called()

    def test_old_install_record_is_not_a_status_prerequisite(self):
        self.place("install.json", {"fixture": "SECRET"}, 901)
        rc, out, _, request = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("stopped", out)
        self.assertFalse(self.fs.reads)
        request.assert_not_called()

    def test_ready_locator_must_go_through_control_request(self):
        self.place("active.json", self.active, 902)
        rc, out, _, request = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("running and verified", out)
        request.assert_called_once_with(self.active, self.paths, "status")

    def test_starting_locator_does_not_send_credentials(self):
        active = dict(self.active, state="STARTING")
        with self.assertRaises(WindowsControlError) as caught:
            self.control._request(active, self.paths, "status")
        self.assertEqual(caught.exception.code, "startup_incomplete")

    def test_unsupported_old_active_record_is_unknown_without_disclosing_content(self):
        self.place("active.json", {"schema": 2, "record": "active", "secret": "SECRET"}, 902)
        rc, out, err, request = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown", err)
        request.assert_not_called()

    def test_unsafe_or_missing_state_is_unknown(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.fs.objects.pop(self.STATE, None)
                else:
                    self.fs.objects[self.STATE]["unsafe_group"] = True
                rc, out, err, request = self.run_cli()
                if missing:
                    self.assertEqual(rc, 0)
                    self.assertIn("stopped", out)
                else:
                    self.assertEqual(rc, 1)
                    self.assertEqual(out, "")
                    self.assertIn("unknown", err)
                request.assert_not_called()

    def test_changed_record_during_read_fails_closed(self):
        self.place("active.json", self.active, 902)
        def mutate(_handle, obj):
            obj["data"] += b"SECRET"
            obj["change"] += 1
        self.fs.on_read = mutate
        before = copy.deepcopy(self.fs.objects)
        rc, out, err, request = self.run_cli_allow_mutation()
        self.assertNotEqual(self.fs.objects, before)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown", err)
        self.assertNotIn("SECRET", err)
        request.assert_not_called()

    def run_cli_allow_mutation(self):
        # The fake backend deliberately mutates during the read in this case.
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.control, "_configuration", return_value=(self.paths, self.listen)), \
             patch.object(self.control, "_locator_hint", return_value=True), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_request", return_value=({"supplies": [], "routes": [], "strategies": [], "cooldown": {}}, "fake-admin-credential")) as request, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.control.out, self.control.err = out, err
            rc = dispatch.main(["status"], control=self.control, root=self.ROOT)
        self.assertFalse(self.fs.handles)
        return rc, out.getvalue(), err.getvalue(), request

    def test_unexpected_record_error_is_sanitized(self):
        self.place("active.json", self.active, 902)
        with patch.object(discovery, "read_windows_active", side_effect=RuntimeError(r"C:\private\SECRET")):
            rc, out, err, request = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown", err)
        request.assert_not_called()

    def test_foreign_listener_is_busy_not_running(self):
        rc, out, err, request = self.run_cli(busy=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("port_busy", err)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
