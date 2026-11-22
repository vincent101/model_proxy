"""Startup orchestration tests; every path, lock and listener is a local fake."""
import contextlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import discovery, server, windows_process
from core.runtime_paths import PATH_KEYS

_listener_check = server._require_windows_listener_validation


class LightStartupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/tmp/model-proxy-fake-a")
        self.config = self.root / "config" / "fake.json"
        self.paths = {key: self.root / "runtime" / key for key in PATH_KEYS}
        items = {key: SimpleNamespace(canonical=str(value), exists=True)
                 for key, value in self.paths.items()}
        items["state_dir"] = SimpleNamespace(
            canonical=str(self.root / ".runtime"), exists=True)
        self.snapshot = SimpleNamespace(project_root=str(self.root), as_dict=lambda: items)
        self.anchor = SimpleNamespace(snapshot=self.snapshot)
        self.legacy = SimpleNamespace(snapshot=self.snapshot)
        self.listen = {"host": "127.0.0.1", "port": 12345, "host_check_enabled": False,
                       "allow_hosts": []}
        self.calls = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

        def p(target, name, **kwargs):
            return self.stack.enter_context(patch.object(target, name, **kwargs))

        def acquire(_snapshot, role, **_kwargs):
            self.calls.append(role)
            return contextlib.nullcontext(self.anchor if role == "anchor.lock" else self.legacy)

        p(server, "_PACKAGE_DIR", new=self.root)
        p(server, "resolve_runtime_paths", return_value=self.paths)
        p(server, "resolve_listen", return_value=self.listen)
        p(server, "_require_windows_service_launch")
        p(discovery, "prepare_windows_runtime_directories")
        p(discovery, "prepare_windows_log", side_effect=lambda root, paths, snapshot: snapshot)
        p(server, "_check_config_targets")
        # _windows_main 直接调用 _windows_check_config_targets（不走 _check_config_targets）
        p(server, "_windows_check_config_targets")
        p(server, "inspect_runtime_identities", return_value=self.snapshot)
        p(server, "verify_record_directory")
        p(server, "acquire_lock", side_effect=acquire)
        p(server, "verify_held_instance_locks")
        p(server, "_check_legacy_observations")
        p(server, "ConfigStore", return_value=object())
        p(server, "_capture_migration_guard", return_value=("guard",))
        p(server, "_verify_migration_guard")
        p(server, "_require_windows_listener_validation")
        p(server, "process_start_identity", return_value={"kind": "windows_filetime", "ticks_100ns": 1})
        p(server, "process_executable", return_value=r"C:\fake\python.exe")
        p(discovery, "_valid_windows_active", return_value=True)
        self.read = p(discovery, "read_windows_active", return_value=None)
        self.state = p(windows_process, "recorded_process_state", return_value="gone")
        self.write = p(server, "write_instance_record")
        self.bound = p(server, "_run_bound_server")
        self.init = p(server, "initialize_installation")
        listener = SimpleNamespace(server_address=("127.0.0.1", 12345))
        self.bind = p(server, "ProxyHTTPServer", return_value=contextlib.nullcontext(listener))

    def test_uses_both_locks_and_one_active_record_without_installation(self):
        server._windows_main(self.config)
        self.assertEqual(self.calls, ["anchor.lock", "lock"])
        self.bind.assert_called_once()
        self.init.assert_not_called()
        self.write.assert_called_once()
        args = self.write.call_args.args
        self.assertEqual(args[1], "active.json")
        self.assertEqual(args[2]["schema"], 3)
        self.assertEqual(args[2]["state"], "STARTING")
        self.bound.assert_called_once()
        self.assertEqual(self.bound.call_args.args[-2], {"windows_active": args[2]})

    def test_live_previous_process_is_not_replaced_or_signalled(self):
        active = {"pid": 100, "process_start": {}, "executable": "fixture"}
        self.read.return_value = active
        self.state.return_value = "running"
        with self.assertRaises(discovery.PreflightError) as caught:
            server._windows_main(self.config)
        self.assertEqual(caught.exception.code, "previous_process_alive")
        self.bind.assert_not_called()
        self.write.assert_not_called()

    def test_failed_log_snapshot_comparison_never_binds_or_updates_snapshot(self):
        import core.platform_lock as locks
        changed = SimpleNamespace(as_dict=self.snapshot.as_dict)
        with patch.object(discovery, 'prepare_windows_log', return_value=changed), \
             patch.object(locks, '_windows_compare_post', side_effect=server.LockError('preflight_changed')):
            with self.assertRaises(server.LockError) as caught:
                server._windows_main(self.config)
        self.assertEqual(caught.exception.code, 'preflight_changed')
        self.assertIs(self.anchor.snapshot, self.snapshot)
        self.assertIs(self.legacy.snapshot, self.snapshot)
        self.bind.assert_not_called()
        self.write.assert_not_called()

    def test_foreign_busy_listener_causes_no_record_or_logging(self):
        self.bind.side_effect = OSError("busy")
        with patch.object(server, "init_logging") as logging:
            with self.assertRaises(OSError):
                server._windows_main(self.config)
        self.write.assert_not_called()
        self.bound.assert_not_called()
        logging.assert_not_called()

    def test_config_role_conflict_fails_before_log_creation(self):
        with patch.object(server, "_windows_check_config_targets",
                          side_effect=server.LockError("config_path_conflict")), \
             patch.object(discovery, "prepare_windows_log") as log:
            with self.assertRaises(server.LockError):
                server._windows_main(self.config)
        log.assert_not_called()
        self.bind.assert_not_called()
        self.write.assert_not_called()

    def test_listener_capability_is_required_but_not_permanently_blocked(self):
        with patch.object(server, "_require_windows_listener_validation", new=_listener_check), \
             patch.object(server.os, "name", "nt"), \
             patch.object(server.socket, "SO_EXCLUSIVEADDRUSE", 123, create=True):
            server._require_windows_listener_validation()
        with patch.object(server, "_require_windows_listener_validation", new=_listener_check), \
             patch.object(server.os, "name", "nt"), \
             patch.object(server.socket, "SO_EXCLUSIVEADDRUSE", None, create=True):
            with self.assertRaises(discovery.PreflightError) as caught:
                server._require_windows_listener_validation()
        self.assertEqual(caught.exception.code, "windows_listener_exclusivity_unavailable")


class LightWindowsLockTests(unittest.TestCase):
    def test_light_preflight_fails_closed_on_untrusted_record_evidence(self):
        """记录文件不可信时，light 预检也必须 fail-closed（当前契约）。

        历史背景：本用例原名
        test_untrusted_legacy_record_files_are_not_a_light_preflight_dependency，
        当时把不可信的 install.json / last_good_paths.json 放在**旧目录名**
        （`.model_proxy_runtime`）下——与真实固定目录无关，所以断言自然成立，
        但并不能证明任何行为。固定目录自 v0.14 起统一为 `.runtime`（`core/`
        已无旧名引用），修正路径后暴露出真实契约：light 模式**仍然会** open
        所有固定记录名（防「运行角色伪装成固定记录」的别名攻击），因此证据
        不可信时必须拒绝，而不是静默跳过。

        真正被 light 模式豁免的只有「快照里不含 aux 记录」，这一点在可读时
        仍然成立（见下半段）。
        """
        from core import platform_lock
        from tests.test_windows_discovery import WindowsDiscoveryLockTests
        fixture = WindowsDiscoveryLockTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for index, name in enumerate(("install.json", "last_good_paths.json"), 500):
            obj = fixture.fs.obj(index)
            obj["unsafe_group"] = True
            fixture.fs.objects[fixture.STATE + "\\" + name] = obj
        with self.assertRaises(discovery.PreflightError):
            discovery.inspect_runtime_identities(fixture.ROOT, fixture.paths)
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery.inspect_runtime_identities(fixture.ROOT, fixture.paths,
                                                 windows_light=True)
        self.assertEqual(caught.exception.code, "windows_evidence_unavailable")

    def test_light_snapshot_omits_aux_records_but_still_verifies_locks(self):
        """可读时 light 快照不含 aux 记录，且锚点/legacy 锁仍可核验。"""
        from core import platform_lock
        from tests.test_windows_discovery import WindowsDiscoveryLockTests
        fixture = WindowsDiscoveryLockTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for index, name in enumerate(("install.json", "last_good_paths.json"), 500):
            fixture.fs.objects[fixture.STATE + "\\" + name] = fixture.fs.obj(index)
        snapshot = discovery.inspect_runtime_identities(fixture.ROOT, fixture.paths,
                                                        windows_light=True)
        self.assertNotIn("install.json", snapshot.as_dict())
        self.assertNotIn("last_good_paths.json", snapshot.as_dict())
        with platform_lock.acquire_lock(snapshot, "anchor.lock", create=True) as anchor:
            with platform_lock.acquire_lock(anchor.snapshot, "lock", create=True,
                                            anchor=anchor) as legacy:
                discovery.verify_held_instance_locks(anchor, legacy)

if __name__ == "__main__":
    unittest.main()
