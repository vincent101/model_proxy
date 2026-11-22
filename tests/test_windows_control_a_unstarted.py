"""First-run and foreign-port contracts, with a synthetic Win32 filesystem."""
import contextlib
import copy
import io
import os
import unittest
from unittest.mock import Mock, patch

from core import discovery, platform_lock, platform_windows, windows_acl, windows_process
from core.cli import windows_control as wc
from core.runtime_paths import LEGACY_DEFAULTS, PATH_KEYS, parse_runtime_paths
from core.windows_paths import PathEvidenceError, WindowsPathBackend
from tests.test_windows_adapter import VirtualACL, VirtualCRT
from tests.test_windows_discovery import PathWinBridge, VirtualFS
from tests.test_windows_control_a_contract import RECORD
from tests.test_windows_process_a_stop import StopBackend


class UnstartedTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    STATE = ROOT + r"\.runtime"
    APPDATA = r"C:\fixture\Local"
    DEFAULT = APPDATA + r"\model_proxy\runtime"

    def setUp(self):
        self.fs = VirtualFS()
        self.fs.objects.pop(self.STATE)
        self.fs.objects[self.APPDATA] = self.fs.obj(55, directory=True)
        self.crt = VirtualCRT(self.fs)
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        # 保留 ResolvedRuntimePaths（dict 子类）：Windows 控制层用
        # windows_legacy_keys 溯源决定「三个 legacy 标记可以合并成一次
        # 缺席观测」。早前这里转成普通 dict，溯源丢失后逐个 inspect 落在
        # 不存在的父目录上，被误判为 boundary_unavailable。
        self.paths = parse_runtime_paths(
            dict(LEGACY_DEFAULTS), project_root=self.ROOT, platform="windows",
            local_appdata=self.APPDATA)
        self.paths = self.paths.map_values(str)
        self.listen = {"host": "127.0.0.1", "port": 19431}
        self.out, self.err = io.StringIO(), io.StringIO()
        self.control = wc.WindowsControl(self.ROOT, out=self.out, err=self.err)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for item in (
            patch.object(os, "name", "nt"),
            patch.dict(os.environ, {"LOCALAPPDATA": self.APPDATA}),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(platform_windows, "_api",
                         side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(platform_windows, "_NativeFDAPI", return_value=self.crt),
            patch.object(windows_acl, "_NativeBackend", return_value=self.acl),
            patch.object(platform_lock, "_windows_release_unknown", False),
            patch.object(self.control, "_configuration", return_value=(self.paths, self.listen)),
            patch.object(self.control, "_locator_hint", return_value=False),
        ):
            self.stack.enter_context(item)

    def assert_quiet(self, before):
        self.assertEqual(self.fs.objects, before)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.crt.mapping)

    def test_first_status_and_off_are_readonly_and_idempotent(self):
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_probe_port") as port, \
             patch.object(self.control, "_request") as request, \
             patch.object(wc.windows_process, "open_verified_process") as process, \
             patch.object(discovery, "prepare_windows_runtime_directories") as prepare, \
             patch.object(wc.subprocess, "Popen") as spawn:
            self.assertEqual(self.control.status(), 0, self.err.getvalue())
            self.assertEqual(self.control.off(), 0, self.err.getvalue())
        self.assertEqual(port.call_count, 4)  # fast occupancy check plus held-evidence check
        request.assert_not_called()
        process.assert_not_called()
        prepare.assert_not_called()
        spawn.assert_not_called()
        self.assert_quiet(before)

    def test_foreign_occupied_port_fails_fast_before_acl_walk_or_credential(self):
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_probe_port", side_effect=wc.WindowsControlError("port_busy")), \
             patch.object(self.control, "_idle_readonly", side_effect=AssertionError("ACL walk forbidden")), \
             patch.object(self.control, "_request") as request, \
             patch.object(wc.windows_process, "open_verified_process") as process, \
             patch.object(discovery, "prepare_windows_runtime_directories") as prepare:
            self.assertEqual(self.control.status(), 1)
            self.assertEqual(self.control.off(), 1)
            self.assertEqual(self.control.restart(), 1)
            self.assertEqual(self.control.on(), 1)
        self.assertIn("port_busy_identity_unknown", self.err.getvalue())
        request.assert_not_called()
        process.assert_not_called()
        prepare.assert_not_called()
        self.assert_quiet(before)

    def test_no_record_configuration_change_cannot_report_stopped(self):
        before = copy.deepcopy(self.fs.objects)
        changed = {"host": "127.0.0.1", "port": 19432}
        for command in ("status", "off"):
            with self.subTest(command=command), \
                 patch.object(self.control, "_configuration",
                              side_effect=[(self.paths, self.listen), (self.paths, changed)]), \
                 patch.object(self.control, "_probe_port"), \
                 patch.object(wc.windows_process, "open_verified_process") as process:
                self.assertEqual(getattr(self.control, command)(), 1)
                self.assertIn("current_configuration_changed", self.err.getvalue())
                self.assertNotIn("stopped", self.out.getvalue())
            process.assert_not_called()
            self.assert_quiet(before)
            self.err.seek(0); self.err.truncate()

    def test_first_restart_checks_absence_before_initializing_and_starting(self):
        events = []
        def probe(_listen):
            events.append("port")
        def prepare(_root, _paths, _appdata):
            events.append("prepare")
            self.assertEqual(events[:2], ["port", "port"])
            self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
            self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
            self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        with patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(discovery, "prepare_windows_runtime_directories", side_effect=prepare), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_active", return_value=None), \
             patch.object(self.control, "_on_locked", side_effect=lambda *_, **__: events.append("start")):
            self.assertEqual(self.control.restart(), 0, self.err.getvalue())
        self.assertEqual(events, ["port", "port", "prepare", "start"])
        self.assertFalse(self.fs.handles)

    def test_first_on_checks_port_before_initialization_or_spawn(self):
        events = []
        with patch.object(self.control, "_probe_port", side_effect=lambda _l: events.append("port")), \
             patch.object(discovery, "prepare_windows_runtime_directories",
                          side_effect=lambda *_: events.append("prepare")), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_on_locked", side_effect=lambda *_, **__: events.append("start")):
            self.assertEqual(self.control.on(), 0, self.err.getvalue())
        self.assertEqual(events, ["port", "prepare", "start"])

    def _ready_runtime_directories(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)

    def test_raw_release_unknown_survives_preflight_error_mapping(self):
        self._ready_runtime_directories()
        with patch.object(self.backend, "open_root",
                          side_effect=PathEvidenceError("release_unknown")):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.inspect_runtime_identities(self.ROOT, self.paths, windows_light=True)
        self.assertEqual(caught.exception.code, "release_unknown")
        self.assertFalse(self.fs.handles)

    def test_existing_control_lock_reuses_held_preflight(self):
        self._ready_runtime_directories()
        lock = self.STATE + r"\control.lock"
        self.fs.objects[lock] = dict(self.fs.obj(70), data=b"control-canary")
        before = copy.deepcopy(self.fs.objects)
        original = discovery._windows_open_preflight
        with patch.object(discovery, "_windows_open_preflight", wraps=original) as preflight:
            with self.control._locked(self.paths) as held:
                self.assertEqual(held.role, "control.lock")
                self.assertIn(70, self.fs.locked)
            # One initial inspection plus one live preflight, no third full
            # traversal after acquiring the already-existing control lock.
            self.assertEqual(preflight.call_count, 2)
        self.assert_quiet(before)

    def test_existing_control_lock_rechecks_every_role_after_locking(self):
        for changed in ("active", "log", "control"):
            with self.subTest(changed=changed):
                self._ready_runtime_directories()
                lock = self.STATE + r"\control.lock"
                self.fs.objects[lock] = dict(self.fs.obj(70), data=b"control-canary")
                target = {"active": self.STATE + r"\active.json",
                          "log": self.paths["log"], "control": lock}[changed]
                original = platform_windows.acquire_guarded_path_lock
                def acquire(*args, **kwargs):
                    guard = original(*args, **kwargs)
                    if changed == "control":
                        self.fs.objects[target] = dict(self.fs.obj(71), data=b"replacement")
                    else:
                        self.fs.objects[target] = self.fs.obj(72)
                    return guard
                with patch.object(platform_windows, "acquire_guarded_path_lock", side_effect=acquire):
                    with self.assertRaises(RuntimeError):
                        with self.control._locked(self.paths):
                            self.fail("changed role must not authorize control")
                self.assertFalse(self.fs.handles)
                self.assertFalse(self.fs.locked)
                self.assertFalse(self.crt.mapping)
                self.fs.objects.pop(target, None)
                self.fs.objects.pop(lock, None)

    def test_start_probe_reuses_control_snapshot_without_full_preflight_or_creation(self):
        self._ready_runtime_directories()
        with self.control._locked(self.paths) as held:
            before = copy.deepcopy(self.fs.objects)
            with patch.object(discovery, "_windows_open_preflight",
                              side_effect=AssertionError("full preflight repeated")) as full:
                self.control._probe_start_locks(self.paths, held)
            full.assert_not_called()
            self.assertEqual(self.fs.objects, before)
            self.assertNotIn(self.STATE + r"\anchor.lock", self.fs.objects)
            self.assertNotIn(self.paths["lock"], self.fs.objects)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.crt.mapping)

    def test_start_probe_checks_existing_locks_without_modification(self):
        self._ready_runtime_directories()
        for index, path in enumerate((self.STATE + r"\anchor.lock", self.paths["lock"]), 70):
            self.fs.objects[path] = dict(self.fs.obj(index), data=b"fake-lock-canary")
        with self.control._locked(self.paths) as held:
            before = copy.deepcopy(self.fs.objects)
            with patch.object(discovery, "_windows_open_preflight",
                              side_effect=AssertionError("full preflight repeated")) as full:
                self.control._probe_start_locks(self.paths, held)
            full.assert_not_called()
            self.assertEqual(self.fs.objects, before)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_start_probe_rejects_changed_active_or_anchor_before_spawn(self):
        for changed in ("active", "anchor"):
            with self.subTest(changed=changed):
                self._ready_runtime_directories()
                anchor = self.STATE + r"\anchor.lock"
                self.fs.objects[anchor] = dict(self.fs.obj(70), data=b"fake-lock-canary")
                with self.control._locked(self.paths) as held:
                    if changed == "active":
                        self.fs.objects[self.STATE + r"\active.json"] = dict(
                            self.fs.obj(72), data=b"fixture")
                    else:
                        self.fs.objects[anchor]["index"] = 71
                    with patch.object(wc.subprocess, "Popen") as spawn:
                        with self.assertRaises(wc.WindowsControlError) as caught:
                            self.control._on_locked(self.paths, self.listen, held_control=held)
                    self.assertEqual(caught.exception.code, "start_state_changed")
                    spawn.assert_not_called()
                self.fs.objects.pop(self.STATE + r"\active.json", None)
                self.fs.objects.pop(anchor, None)
                self.fs.objects.pop(self.STATE + r"\control.lock", None)
                self.assertFalse(self.fs.handles)
                self.assertFalse(self.fs.locked)

    def test_start_probe_refuses_held_legacy_lock_without_spawning(self):
        self._ready_runtime_directories()
        self.fs.objects[self.paths["lock"]] = dict(self.fs.obj(70), data=b"fake-lock-canary")
        with platform_windows.acquire_guarded_path_lock(self.paths["lock"], create=False), \
             self.control._locked(self.paths) as held, \
             patch.object(wc.subprocess, "Popen") as spawn:
            with self.assertRaises(RuntimeError) as caught:
                self.control._on_locked(self.paths, self.listen, held_control=held)
        self.assertEqual(getattr(caught.exception, "code", None), "lock_conflict")
        spawn.assert_not_called()
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_start_poll_uses_absence_only_as_hint_never_as_readiness(self):
        self._ready_runtime_directories()
        with self.control._locked(self.paths) as held:
            child = Mock()
            child.poll.side_effect = [None, 0]
            with patch.dict(os.environ, {"LOCALAPPDATA": self.APPDATA, "SYSTEMROOT": r"C:\Windows"}, clear=True), \
                 patch.object(self.control, "_active", side_effect=AssertionError("record read forbidden")) as read, \
                 patch.object(self.control, "_locator_hint", return_value=False), \
                 patch.object(self.control, "_probe_start_locks") as probe, \
                 patch.object(self.control, "_probe_port"), \
                 patch.object(self.control, "_request") as request, \
                 patch.object(wc.time, "sleep"), \
                 patch.object(wc.subprocess, "Popen", return_value=child) as spawn:
                with self.assertRaises(wc.WindowsControlError) as caught:
                    self.control._on_locked(self.paths, self.listen, held_control=held)
            self.assertEqual(caught.exception.code, "startup_failed")
            probe.assert_called_once_with(self.paths, held)
            spawn.assert_called_once()
            read.assert_not_called()
            request.assert_not_called()

    def test_existing_legacy_pid_or_start_lock_remains_unknown(self):
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        for role, directory in (("pid", False), ("start_lock", True)):
            with self.subTest(role=role):
                self.fs.objects[self.paths[role]] = self.fs.obj(63, directory=directory)
                before = copy.deepcopy(self.fs.objects)
                with patch.object(self.control, "_probe_port"), \
                     patch.object(self.control, "_request") as request:
                    self.assertEqual(self.control.status(), 1)
                    self.assertIn("legacy_state_unknown", self.err.getvalue())
                request.assert_not_called()
                self.assert_quiet(before)
                self.fs.objects.pop(self.paths[role])
                self.err.seek(0); self.err.truncate()

    def test_existing_unlocked_control_anchor_and_legacy_are_not_created_or_truncated(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        targets = (self.STATE + r"\control.lock", self.STATE + r"\anchor.lock", self.paths["lock"])
        for index, path in enumerate(targets, 70):
            self.fs.objects[path] = dict(self.fs.obj(index), data=b"fake-lock-canary")
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_probe_port"), \
             patch.object(self.control, "_request") as request:
            self.assertEqual(self.control.status(), 0, self.err.getvalue())
        request.assert_not_called()
        self.assert_quiet(before)

    def test_stop_release_probe_borrows_held_control_lock_without_self_conflict(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        self.fs.objects[self.STATE + r"\active.json"] = dict(self.fs.obj(63), data=b"fixture")
        active = {"fixture": "already checked"}
        with patch.object(self.control, "_active", return_value=active), \
             patch.object(self.control, "_probe_port"):
            with self.control._locked(self.paths) as held:
                self.assertEqual(held.role, "control.lock")
                self.assertTrue(held._windows_guard.created)
                self.control._idle_readonly(self.paths, self.listen, expected_active=active,
                                            held_control=held)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.crt.mapping)
        self.assertNotIn(self.STATE + r"\anchor.lock", self.fs.objects)
        self.assertNotIn(self.paths["lock"], self.fs.objects)

    def test_missing_borrowed_control_path_cannot_be_accepted_as_released(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        self.fs.objects[self.STATE + r"\active.json"] = dict(self.fs.obj(63), data=b"fixture")
        active = {"fixture": "already checked"}
        with patch.object(self.control, "_active", return_value=active), \
             patch.object(self.control, "_probe_port") as port:
            with self.control._locked(self.paths) as held:
                self.fs.objects.pop(self.STATE + r"\control.lock")
                with self.assertRaises(wc.WindowsControlError) as caught:
                    self.control._idle_readonly(self.paths, self.listen, expected_active=active,
                                                held_control=held)
        self.assertEqual(caught.exception.code, "lock_identity_unknown")
        port.assert_not_called()
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)

    def test_running_status_never_creates_control_lock(self):
        active = dict(RECORD, project_root=self.ROOT, runtime_paths=self.paths,
                      config=self.control.config_path, entry=self.ROOT + r"\core\cli\model_proxy.py",
                      listen=self.listen)
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_locator_hint", return_value=True), \
             patch.object(self.control, "_active", return_value=active), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(self.control, "_request", return_value=({"supplies": [], "routes": [], "strategies": [], "cooldown": {}}, "fake-admin-credential")) as request, \
             patch.object(self.control, "_locked", side_effect=AssertionError("write lock forbidden")):
            self.assertEqual(self.control.status(), 0)
        request.assert_called_once_with(active, self.paths, "status")
        self.assert_quiet(before)

    def test_stale_record_off_is_idempotent_without_creating_control_lock(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        self.fs.objects[self.STATE + r"\active.json"] = dict(self.fs.obj(63), data=b"fixture")
        active = dict(RECORD, project_root=self.ROOT, runtime_paths=self.paths,
                      config=self.control.config_path, entry=self.ROOT + r"\core\cli\model_proxy.py")
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_locator_hint", return_value=True), \
             patch.object(os.path, "lexists", return_value=False), \
             patch.object(self.control, "_active", return_value=active), \
             patch.object(windows_process, "recorded_process_state", return_value="gone"), \
             patch.object(windows_process, "open_verified_process") as process, \
             patch.object(self.control, "_probe_port"), \
             patch.object(self.control, "_locked", side_effect=AssertionError("no creation permitted")):
            self.assertEqual(self.control.off(), 0, self.err.getvalue())
        process.assert_not_called()
        self.assertIn("Current configuration endpoint UNKNOWN (not checked)", self.out.getvalue())
        self.assert_quiet(before)

    def test_existing_locked_control_file_returns_unknown_without_stopping(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        path = self.STATE + r"\control.lock"
        self.fs.objects[path] = dict(self.fs.obj(70), data=b"fake-lock-canary")
        before = copy.deepcopy(self.fs.objects)
        with platform_windows.acquire_guarded_path_lock(path, create=False), \
             patch.object(self.control, "_probe_port"), \
             patch.object(wc.windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 1)
        process.assert_not_called()
        self.assert_quiet(before)

    def test_missing_state_does_not_hide_a_held_legacy_lock(self):
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        lock = self.paths["lock"]
        self.fs.objects[lock] = dict(self.fs.obj(70), data=b"fake-legacy-canary")
        before = copy.deepcopy(self.fs.objects)
        with platform_windows.acquire_guarded_path_lock(lock, create=False), \
             patch.object(self.control, "_probe_port"), \
             patch.object(self.control, "_request") as request, \
             patch.object(wc.windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.status(), 1)
            self.assertNotIn("stopped", self.out.getvalue())
        request.assert_not_called()
        process.assert_not_called()
        self.assert_quiet(before)

    def test_missing_state_and_unlocked_legacy_lock_can_be_checked_without_writes(self):
        self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.DEFAULT] = self.fs.obj(62, directory=True)
        self.fs.objects[self.paths["lock"]] = dict(self.fs.obj(70), data=b"fake-legacy-canary")
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_probe_port"), \
             patch.object(wc.windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 0, self.err.getvalue())
        process.assert_not_called()
        self.assert_quiet(before)

    def test_record_appearing_during_readonly_check_never_reports_stopped(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        before = copy.deepcopy(self.fs.objects)
        count = 0
        def probe(_listen):
            nonlocal count
            count += 1
            if count == 2:
                self.fs.objects[self.STATE + r"\active.json"] = dict(
                    self.fs.obj(77), data=b'{"fake":"racing-record"}')
        with patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(self.control, "_request") as request:
            self.assertEqual(self.control.status(), 1)
        self.assertNotIn("stopped", self.out.getvalue())
        request.assert_not_called()
        self.fs.objects.pop(self.STATE + r"\active.json", None)
        self.assert_quiet(before)

    def test_false_absence_hint_cannot_authorize_stopped_or_restart(self):
        self.fs.objects[self.STATE] = self.fs.obj(60, directory=True)
        self.fs.objects[self.STATE + r"\active.json"] = dict(
            self.fs.obj(77), data=b'{"fake":"untrusted-record"}')
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_probe_port"), \
             patch.object(self.control, "_request") as request, \
             patch.object(discovery, "prepare_windows_runtime_directories") as prepare, \
             patch.object(wc.windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.status(), 1)
            self.assertEqual(self.control.off(), 1)
            self.assertEqual(self.control.restart(), 1)
        self.assertNotIn("stopped", self.out.getvalue())
        request.assert_not_called()
        prepare.assert_not_called()
        process.assert_not_called()
        self.assert_quiet(before)

    def test_missing_project_root_is_unknown_not_stopped(self):
        self.fs.objects.pop(self.ROOT)
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_probe_port"), \
             patch.object(self.control, "_request") as request:
            self.assertEqual(self.control.status(), 1)
        self.assertNotIn("stopped", self.out.getvalue())
        request.assert_not_called()
        self.assert_quiet(before)

    def test_state_directory_appearing_during_probe_is_unknown(self):
        before = copy.deepcopy(self.fs.objects)
        count = 0
        def probe(_listen):
            nonlocal count
            count += 1
            if count == 2:
                self.fs.objects[self.STATE] = self.fs.obj(78, directory=True)
        with patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(self.control, "_request") as request:
            self.assertEqual(self.control.status(), 1)
        self.assertNotIn("stopped", self.out.getvalue())
        request.assert_not_called()
        self.fs.objects.pop(self.STATE, None)
        self.assert_quiet(before)

    def test_default_runtime_parent_appearing_during_probe_is_unknown(self):
        before = copy.deepcopy(self.fs.objects)
        count = 0
        def probe(_listen):
            nonlocal count
            count += 1
            if count == 2:
                self.fs.objects[self.APPDATA + r"\model_proxy"] = self.fs.obj(79, directory=True)
        with patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(self.control, "_request") as request:
            self.assertEqual(self.control.off(), 1)
        self.assertNotIn("stopped", self.out.getvalue())
        request.assert_not_called()
        self.fs.objects.pop(self.APPDATA + r"\model_proxy", None)
        self.assert_quiet(before)

    def test_off_with_missing_control_lock_verifies_target_before_creating_lock(self):
        control = wc.WindowsControl(RECORD["project_root"], config_path=RECORD["config"],
                                    out=io.StringIO(), err=io.StringIO())
        backend = StopBackend()
        backend.exe = RECORD["executable"]
        original = windows_process.open_verified_process
        events = []
        def open_process(pid, start, image, **kwargs):
            events.append("open_stop" if kwargs.get("terminate") else "open_observe")
            return original(pid, start, image, backend=backend, **kwargs)
        def verify(_active, revalidate):
            events.append("verify_entry")
            revalidate()
        @contextlib.contextmanager
        def locked(_paths):
            events.append("control_lock")
            yield
        with patch.object(control, "_locator_hint", return_value=True), \
             patch.object(os.path, "lexists", return_value=False), \
             patch.object(control, "_active", return_value=RECORD), \
             patch.object(control, "_configuration", side_effect=AssertionError("config read forbidden")), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(windows_process, "open_verified_process", side_effect=open_process), \
             patch.object(control, "_verify_entry", side_effect=verify), \
             patch.object(control, "_locked", side_effect=locked), \
             patch.object(control, "_idle_readonly") as idle:
            self.assertEqual(control.off(), 0, control.err.getvalue())
        self.assertEqual(events, ["open_observe", "verify_entry", "control_lock",
                                  "open_stop", "verify_entry"])
        self.assertEqual(backend.stops, [(100, control.stop_timeout)])
        self.assertEqual(backend.closed, [100, 100])
        idle.assert_called_once_with(RECORD["runtime_paths"], RECORD["listen"],
                                     expected_active=RECORD, held_control=None, current_listen=None)

    def test_off_missing_control_lock_and_bad_entry_never_creates_or_stops(self):
        control = wc.WindowsControl(RECORD["project_root"], config_path=RECORD["config"],
                                    out=io.StringIO(), err=io.StringIO())
        backend = StopBackend()
        backend.exe = RECORD["executable"]
        proof = windows_process.open_verified_process(
            RECORD["pid"], RECORD["process_start"], RECORD["executable"], backend=backend)
        with patch.object(control, "_locator_hint", return_value=True), \
             patch.object(os.path, "lexists", return_value=False), \
             patch.object(control, "_active", return_value=RECORD), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(windows_process, "open_verified_process", return_value=proof), \
             patch.object(control, "_verify_entry", side_effect=wc.WindowsControlError("launch_mismatch")), \
             patch.object(control, "_locked") as locked:
            self.assertEqual(control.off(), 1)
        locked.assert_not_called()
        self.assertEqual(backend.stops, [])
        self.assertEqual(backend.closed, [100])

    def test_off_waits_for_actual_lock_contention_without_creating_files(self):
        control = wc.WindowsControl(RECORD["project_root"], config_path=RECORD["config"],
                                    out=io.StringIO(), err=io.StringIO())
        with patch.object(control, "_active", return_value=RECORD), \
             patch.object(windows_process, "recorded_process_state", return_value="gone"), \
             patch.object(control, "_idle_readonly",
                          side_effect=[wc.WindowsControlError("lock_conflict"), None]) as probe, \
             patch.object(wc.time, "sleep") as sleep, \
             patch.object(windows_process, "open_verified_process") as process:
            control._off_locked(RECORD["runtime_paths"], None)
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once()
        process.assert_not_called()

    def test_off_unknown_release_evidence_does_not_wait_or_signal(self):
        control = wc.WindowsControl(RECORD["project_root"], config_path=RECORD["config"],
                                    out=io.StringIO(), err=io.StringIO())
        with patch.object(control, "_active", return_value=RECORD), \
             patch.object(windows_process, "recorded_process_state", return_value="gone"), \
             patch.object(control, "_idle_readonly",
                          side_effect=wc.WindowsControlError("access_unverified")), \
             patch.object(wc.time, "sleep") as sleep, \
             patch.object(windows_process, "open_verified_process") as process:
            with self.assertRaises(wc.WindowsControlError) as caught:
                control._off_locked(RECORD["runtime_paths"], None)
        self.assertEqual(caught.exception.code, "access_unverified")
        sleep.assert_not_called()
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
