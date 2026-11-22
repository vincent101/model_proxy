"""Windows startup safety with fake records only; no service or signal."""
import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import core.server as server
from core.discovery import PreflightError


class WindowsPreviousProcessSafetyTests(unittest.TestCase):
    def test_existing_record_never_probes_or_signals_windows_pid(self):
        record = {"active": {"runtime_paths": {"fake": "fake"}, "locks": {"fake": "fake"},
                              "pid": 4321, "process_start": {"fake": "fake"}}}
        with patch.object(server.os, "name", "nt"), \
             patch.object(server.os, "kill", side_effect=AssertionError("signal forbidden")) as signal, \
             patch.object(server, "process_start_identity") as identity:
            with self.assertRaises(PreflightError) as caught:
                server._ensure_stale_instance(record, {"fake": "fake"}, {}, {"fake": "fake"})
        self.assertEqual(caught.exception.code, "process_unverified")
        signal.assert_not_called()
        identity.assert_not_called()

    def test_commit_failure_reports_observed_states_without_paths(self):
        from core.discovery import WindowsRecordCommitError, WindowsRecordRecovery
        failure = WindowsRecordCommitError("record_commit_unknown", stage="api_failed", winerror=1177,
                                          recovery=WindowsRecordRecovery("missing", "replacement_changed", "unknown"),
                                          temporary_created=True, temporary_may_contain_data=True)
        output = io.StringIO()
        with patch.object(server.os, "name", "nt"), \
             patch.object(server, "resolve_listen", return_value={}), \
             patch.object(server, "resolve_runtime_paths", side_effect=failure), redirect_stderr(output):
            with self.assertRaises(SystemExit):
                server.main()
        message = output.getvalue()
        self.assertIn("winerror=1177", message)
        self.assertIn("target=missing", message)
        self.assertIn("source=replacement_changed", message)
        self.assertIn("old_other_name=unknown", message)
        self.assertNotIn("C:\\", message)

    def test_windows_partial_startup_records_are_never_blindly_rolled_back(self):
        with patch.object(server.os, "name", "nt"), \
             patch.object(server, "read_instance_record", side_effect=AssertionError("read forbidden")), \
             patch.object(server, "delete_instance_record_if_matches", side_effect=AssertionError("delete forbidden")), \
             patch.object(server, "write_instance_record", side_effect=AssertionError("write forbidden")):
            with self.assertRaises(PreflightError) as caught:
                server._restore_prestart_records("fake", {}, {"active.json": [None]}, None, None)
        self.assertEqual(caught.exception.code, "record_cleanup_failed")

    def test_no_active_record_requires_no_process_probe(self):
        with patch.object(server.os, "name", "nt"), \
             patch.object(server.os, "kill", side_effect=AssertionError("signal forbidden")) as signal:
            server._ensure_stale_instance({"active": None}, {}, {}, {})
        signal.assert_not_called()


class WindowsLegacyObservationTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    RUNTIME = r"C:\fixture\runtime"
    def setUp(self):
        from core import discovery, platform_windows, windows_acl
        from core.runtime_paths import PATH_KEYS
        from core.windows_paths import WindowsPathBackend
        from tests.test_windows_adapter import VirtualACL
        from tests.test_windows_config_snapshot import ConfigFS
        from tests.test_windows_discovery import PathWinBridge
        self.fs = ConfigFS()
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.paths = {key: self.RUNTIME + "\\" + key for key in PATH_KEYS}
        # 生产把 runtime_paths.json 放在 <project>\\config\\ 下；夹具补上该目录，
        # 否则 config 角色预检会判定父目录未观测（config_path_unknown）。
        self.fs.objects[self.ROOT + r"\config"] = self.fs.obj(12, directory=True)
        self.fs.objects[self.paths["log"]] = dict(self.fs.obj(701), data=b"fake log")
        for item in (
            patch.object(os, "name", "nt"),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(platform_windows, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(windows_acl, "_NativeBackend", return_value=self.acl),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.discovery = discovery

    def test_existing_role_metadata_is_observed_without_reading_content(self):
        snapshot = self.discovery.inspect_runtime_identities(self.ROOT, self.paths)
        value = dict(server._check_legacy_observations(snapshot))
        self.assertEqual(value["log"].inode, 701)
        self.assertIsNone(value["totals"])
        self.assertEqual(self.fs.reads, [])
        self.assertFalse(self.fs.handles)

    def test_config_and_runtime_roles_are_checked_with_held_path_evidence(self):
        from pathlib import PureWindowsPath
        business = self.ROOT + r"\config.json"
        # 生产读取的是 <project>\config\runtime_paths.json（不是项目根下），
        # 夹具按真实布局创建，否则 _windows_check_config_targets 判定父目录未观测。
        paths_config = self.ROOT + r"\config\runtime_paths.json"
        self.fs.objects[business] = dict(self.fs.obj(710), data=b"{}")
        self.fs.objects[paths_config] = dict(self.fs.obj(711), data=b"{}")
        with patch.object(server, "_PACKAGE_DIR", PureWindowsPath(self.ROOT)), \
             patch.object(server, "_RUNTIME_PATHS_FILE", PureWindowsPath(paths_config)):
            server._check_config_targets(self.paths, business)
            self.assertFalse(self.fs.handles)
            self.paths["log"] = business
            with self.assertRaises(server.LockError) as caught:
                server._check_config_targets(self.paths, business)
            self.assertEqual(caught.exception.code, "config_path_conflict")
        self.assertFalse(self.fs.handles)

    def test_config_hardlink_alias_to_runtime_role_is_rejected(self):
        from pathlib import PureWindowsPath
        business = self.ROOT + r"\config.json"
        # 生产读取的是 <project>\config\runtime_paths.json（不是项目根下），
        # 夹具按真实布局创建，否则 _windows_check_config_targets 判定父目录未观测。
        paths_config = self.ROOT + r"\config\runtime_paths.json"
        obj = dict(self.fs.obj(710), data=b"{}")
        self.fs.objects[business] = obj
        self.fs.objects[self.paths["log"]] = obj
        self.fs.objects[paths_config] = dict(self.fs.obj(711), data=b"{}")
        with patch.object(server, "_PACKAGE_DIR", PureWindowsPath(self.ROOT)), \
             patch.object(server, "_RUNTIME_PATHS_FILE", PureWindowsPath(paths_config)):
            with self.assertRaises(server.LockError) as caught:
                server._check_config_targets(self.paths, business)
            self.assertEqual(caught.exception.code, "config_path_conflict")
        self.assertFalse(self.fs.handles)

    def test_existing_pid_is_unknown_and_never_probed(self):
        self.fs.objects[self.paths["pid"]] = self.fs.obj(702)
        snapshot = self.discovery.inspect_runtime_identities(self.ROOT, self.paths)
        with patch.object(server.os, "kill", side_effect=AssertionError("signal forbidden")) as signal:
            with self.assertRaises(PreflightError) as caught:
                server._check_legacy_observations(snapshot)
        self.assertEqual(caught.exception.code, "legacy_state_unknown")
        signal.assert_not_called()
        self.assertFalse(self.fs.handles)


if __name__ == "__main__":
    unittest.main()
