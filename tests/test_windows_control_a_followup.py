"""Follow-up contracts with fake paths, snapshots and process observations."""
import contextlib
import itertools
import io
import json
import ntpath
import os
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import discovery, server, windows_process
from core.cli import dispatch, model_proxy as bootstrap
from core.cli import windows_control as wc
from core.runtime_paths import PATH_KEYS, LEGACY_DEFAULTS, RuntimePathsError, parse_runtime_paths, read_runtime_paths
from tests.test_windows_control_a_contract import ROOT, ENTRY, CONFIG, PATHS, RECORD


class ProvisionTests(unittest.TestCase):
    def test_only_fixed_and_exact_default_parents_are_created(self):
        local = r"C:\fixture\Local AppData"
        paths = dict(PATHS, lock=local + r"\model_proxy\runtime\model_proxy.lock")
        calls = []
        ensure = lambda path, *, trusted_boundary: calls.append(path) or SimpleNamespace(
            created=True, identity=(17, 101, 1, 16), protection=("private",))
        discovery.prepare_windows_runtime_directories(ROOT, paths, local, ensure_directory=ensure)
        self.assertEqual(calls, [ROOT + r"\.runtime", local + r"\model_proxy",
                                 local + r"\model_proxy\runtime"])
        calls.clear()
        discovery.prepare_windows_runtime_directories(ROOT, PATHS, local, ensure_directory=ensure)
        self.assertEqual(calls, [ROOT + r"\.runtime"])

    def test_unsafe_existing_directory_or_unknown_create_stops_without_repair(self):
        def reject(_path, *, trusted_boundary):
            error = RuntimeError("unsafe")
            error.code, error.created = "protection_unavailable", False
            raise error
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery.prepare_windows_runtime_directories(ROOT, PATHS, None, ensure_directory=reject)
        self.assertEqual(caught.exception.code, "windows_private_directory_unverified")
        def unknown(_path, *, trusted_boundary):
            error = RuntimeError("unknown")
            error.created = None
            raise error
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery.prepare_windows_runtime_directories(ROOT, PATHS, None, ensure_directory=unknown)
        self.assertEqual(caught.exception.code, "windows_creation_unknown")

    def test_missing_native_directory_primitive_fails_explicitly(self):
        from core import platform_windows
        with patch.object(platform_windows, "ensure_private_directory", None):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.prepare_windows_runtime_directories(ROOT, PATHS, None)
        self.assertEqual(caught.exception.code, "windows_private_creation_unavailable")

    def test_invalid_primitive_result_reports_possible_created_directory(self):
        for created, code in ((None, "windows_creation_unknown"),
                              (True, "windows_directory_created_unverified")):
            with self.subTest(created=created):
                with self.assertRaises(discovery.PreflightError) as caught:
                    discovery.prepare_windows_runtime_directories(
                        ROOT, PATHS, None, ensure_directory=lambda _, *, trusted_boundary: SimpleNamespace(created=created))
                self.assertEqual(caught.exception.code, code)

    def test_on_invalid_runtime_does_not_create_directories_or_spawn(self):
        controller = wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_configuration", side_effect=RuntimePathsError(
                 "unreadable_file", "fake runtime unavailable")), \
             patch.object(discovery, "prepare_windows_runtime_directories") as provision, \
             patch.object(wc.subprocess, "Popen") as spawn:
            self.assertEqual(controller.on(), 1)
        provision.assert_not_called()
        spawn.assert_not_called()

    def test_custom_parent_spelling_is_not_silently_normalized_and_created(self):
        local = r"C:\fixture\Local AppData"
        custom = local + r"\elsewhere\..\model_proxy\runtime\model_proxy.lock"
        calls = []
        discovery.prepare_windows_runtime_directories(
            ROOT, dict(PATHS, lock=custom), local,
            ensure_directory=lambda path, *, trusted_boundary: calls.append(path) or SimpleNamespace(
                created=True, identity=(17, 101, 1, 16), protection=("private",)))
        self.assertEqual(calls, [ROOT + r"\.runtime"])

    def test_log_created_only_when_missing_and_fresh_protection_is_checked(self):
        missing = SimpleNamespace(as_dict=lambda: {"log": SimpleNamespace(exists=False, canonical=PATHS["log"])})
        present = SimpleNamespace(as_dict=lambda: {"log": SimpleNamespace(
            exists=True, canonical=PATHS["log"], device=17, inode=101)})
        create = Mock(return_value=SimpleNamespace(created=True, identity=(17, 101, 1, 0),
                                                   protection=("private",)))
        inspect = Mock(return_value=present)
        self.assertIs(discovery.prepare_windows_log(ROOT, PATHS, missing,
                         create_empty_file=create, inspect=inspect), present)
        create.assert_called_once_with(PATHS["log"], trusted_boundary=ROOT)
        inspect.assert_called_once_with(ROOT, PATHS, windows_light=True)
        create.reset_mock()
        self.assertIs(discovery.prepare_windows_log(ROOT, PATHS, present,
                         create_empty_file=create, inspect=inspect), present)
        create.assert_not_called()

    def test_log_unknown_creation_cannot_be_treated_as_success(self):
        missing = SimpleNamespace(as_dict=lambda: {"log": SimpleNamespace(exists=False, canonical=PATHS["log"])})
        def fail(_path, *, trusted_boundary):
            error = RuntimeError("unknown")
            error.created = None
            raise error
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery.prepare_windows_log(ROOT, PATHS, missing, create_empty_file=fail, inspect=Mock())
        self.assertEqual(caught.exception.code, "windows_creation_unknown")

    def test_created_log_replaced_before_reinspection_is_not_accepted(self):
        missing = SimpleNamespace(as_dict=lambda: {"log": SimpleNamespace(exists=False, canonical=PATHS["log"])})
        replaced = SimpleNamespace(as_dict=lambda: {"log": SimpleNamespace(
            exists=True, canonical=PATHS["log"], device=17, inode=102)})
        create = Mock(return_value=SimpleNamespace(created=True, identity=(17, 101, 1, 0),
                                                   protection=("private",)))
        with self.assertRaises(discovery.PreflightError):
            discovery.prepare_windows_log(ROOT, PATHS, missing, create_empty_file=create,
                                          inspect=Mock(return_value=replaced))

    def test_b_creation_primitives_wire_through_fake_win32_preflight(self):
        from core import platform_windows as native
        from tests.test_windows_secure_creation import PrivateCreation
        fixture = PrivateCreation("runTest")
        fixture.setUp()
        root = fixture.ROOT
        state = root + r"\.runtime"
        fixture.fs.objects.pop(state)
        paths = {key: r"C:\fixture\runtime" + "\\" + key for key in PATH_KEYS}
        def ensure(path, *, trusted_boundary):
            return native.ensure_private_directory(path, trusted_boundary=trusted_boundary, **fixture.args())
        def create(path, *, trusted_boundary):
            return native.create_private_empty_file(path, trusted_boundary=trusted_boundary, **fixture.args())
        with patch.object(os, "name", "nt"), \
             patch.object(discovery, "_windows_path_backend", return_value=fixture.paths):
            discovery.prepare_windows_runtime_directories(root, paths, None, ensure_directory=ensure)
            snapshot = discovery.inspect_runtime_identities(root, paths, windows_light=True)
            self.assertFalse(snapshot.as_dict()["log"].exists)
            snapshot = discovery.prepare_windows_log(root, paths, snapshot, create_empty_file=create)
        self.assertTrue(snapshot.as_dict()["log"].exists)
        self.assertEqual(fixture.fs.objects[paths["log"]].get("data", b""), b"")
        self.assertFalse(fixture.fs.handles)


class OffRecoveryTests(unittest.TestCase):
    def controller(self):
        return wc.WindowsControl(ROOT, config_path=CONFIG, out=io.StringIO(), err=io.StringIO())

    def test_off_uses_record_without_parsing_damaged_config_or_runtime_json(self):
        controller = self.controller()
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_locator_hint", return_value=True), \
             patch.object(wc.os.path, "lexists", return_value=True), \
             patch.object(controller, "_active", return_value=RECORD), \
             patch.object(controller, "_configuration", side_effect=AssertionError("config read forbidden")), \
             patch.object(controller, "_runtime_configuration", side_effect=AssertionError("runtime read forbidden")), \
             patch.object(controller, "_locked", return_value=contextlib.nullcontext()) as lock, \
             patch.object(controller, "_off_locked") as off:
            self.assertEqual(controller.off(), 0)
        lock.assert_called_once_with(PATHS)
        off.assert_called_once_with(PATHS, None, held_control=None,
                                    current_listen=None, verify_current=True)

    def test_changed_locator_never_stops(self):
        controller = self.controller()
        changed = dict(RECORD, instance_nonce="b" * 32)
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_locator_hint", return_value=True), \
             patch.object(wc.os.path, "lexists", return_value=True), \
             patch.object(controller, "_active", side_effect=[RECORD, changed]), \
             patch.object(controller, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(controller, "_off_locked") as off:
            self.assertEqual(controller.off(), 1)
        off.assert_not_called()

    def test_restart_bad_config_stops_neither_old_nor_new_process(self):
        controller = self.controller()
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_configuration", side_effect=ValueError("bad json")), \
             patch.object(controller, "_off_locked") as off, \
             patch.object(controller, "_on_locked") as on:
            self.assertEqual(controller.restart(), 1)
        off.assert_not_called()
        on.assert_not_called()

    def test_restart_different_recorded_config_does_not_stop_old_instance(self):
        controller = self.controller()
        other = dict(RECORD, config=ROOT + r"\config\other.json")
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_locator_hint", return_value=True), \
             patch.object(controller, "_configuration", return_value=(PATHS, RECORD["listen"])), \
             patch.object(controller, "_active", return_value=other), \
             patch.object(controller, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(controller, "_off_locked") as off, \
             patch.object(controller, "_on_locked") as on:
            self.assertEqual(controller.restart(), 1)
        off.assert_not_called()
        on.assert_not_called()

    def test_off_stops_only_the_same_verified_handle_even_if_config_is_unreadable(self):
        from tests.test_windows_process_a_stop import StopBackend
        controller = self.controller()
        backend = StopBackend()
        backend.exe = RECORD["executable"]
        proof = windows_process.open_verified_process(
            RECORD["pid"], RECORD["process_start"], RECORD["executable"],
            backend=backend, terminate=True)
        def verify_entry(active, revalidate):
            self.assertEqual(active, RECORD)
            revalidate()
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_locator_hint", return_value=True), \
             patch.object(wc.os.path, "lexists", return_value=True), \
             patch.object(controller, "_active", return_value=RECORD), \
             patch.object(controller, "_configuration", side_effect=AssertionError("bad config read")), \
             patch.object(controller, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(wc.windows_process, "recorded_process_state", return_value="running"), \
             patch.object(wc.windows_process, "open_verified_process", return_value=proof), \
             patch.object(controller, "_verify_entry", side_effect=verify_entry), \
             patch.object(controller, "_idle_readonly") as idle:
            self.assertEqual(controller.off(), 0)
        self.assertEqual(backend.opened, [4321])
        self.assertEqual(backend.stops, [(100, controller.stop_timeout)])
        self.assertEqual(backend.closed, [100])
        idle.assert_called_once_with(PATHS, RECORD["listen"], expected_active=RECORD,
                                     held_control=None, current_listen=None)

    def test_off_does_not_require_current_config_override_to_match_record(self):
        controller = wc.WindowsControl(ROOT, config_path=ROOT + r"\config\other.json",
                                       out=io.StringIO(), err=io.StringIO())
        with patch.object(controller, "_active", return_value=RECORD), \
             patch.object(wc.windows_process, "recorded_process_state", return_value="reused"), \
             patch.object(wc.windows_process, "open_verified_process") as process, \
             patch.object(controller, "_idle_readonly"):
            controller._off_locked(PATHS, None)
        process.assert_not_called()

    def test_off_different_entry_never_terminates(self):
        from tests.test_windows_process_a_stop import StopBackend
        controller = self.controller()
        backend = StopBackend()
        backend.exe = RECORD["executable"]
        proof = windows_process.open_verified_process(
            RECORD["pid"], RECORD["process_start"], RECORD["executable"],
            backend=backend, terminate=True)
        with patch.object(wc.os, "name", "nt"), \
             patch.object(controller, "_locator_hint", return_value=True), \
             patch.object(wc.os.path, "lexists", return_value=True), \
             patch.object(controller, "_active", return_value=RECORD), \
             patch.object(controller, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(wc.windows_process, "recorded_process_state", return_value="running"), \
             patch.object(wc.windows_process, "open_verified_process", return_value=proof), \
             patch.object(controller, "_verify_entry", side_effect=wc.WindowsControlError("launch_mismatch")), \
             patch.object(controller, "_probe_locks") as probe:
            self.assertEqual(controller.off(), 1)
        self.assertEqual(backend.stops, [])
        probe.assert_not_called()


class DirectLaunchTests(unittest.TestCase):
    def test_no_argument_launch_reexecs_absolute_entry_with_explicit_config_and_port(self):
        image = r"C:\Python\python.exe"
        child = Mock()
        child.wait.return_value = 23
        run = Mock(return_value=child)
        resolve = Mock(return_value={"port": 12345})
        self.assertEqual(bootstrap._normalize_windows_direct_launch(
            {"MODEL_PROXY_CONFIG": CONFIG}, root=ROOT, entry=ENTRY, executable=image,
            original=[image, "-B", ENTRY], spawn=run, resolve=resolve), 23)
        expected = [image, "-B", ENTRY, "--config", CONFIG, "--port", "12345"]
        run.assert_called_once_with(expected, close_fds=True)
        child.wait.assert_called_once_with()
        resolve.assert_called_once_with(CONFIG, None)

    def test_unknown_interpreter_switch_fails_before_reexec(self):
        run = Mock()
        with self.assertRaises(ValueError):
            bootstrap._normalize_windows_direct_launch(
                {}, root=ROOT, entry=ENTRY, executable=r"C:\Python\python.exe",
                original=[r"C:\Python\python.exe", "-O", ENTRY], spawn=run, resolve=Mock())
        run.assert_not_called()

    def test_unobservable_direct_server_entry_refused_before_startup(self):
        root = PureWindowsPath(ROOT)
        with patch.object(server, "_PACKAGE_DIR", root), \
             patch.object(server.sys, "orig_argv", [r"C:\Python\python.exe", ENTRY]):
            with self.assertRaises(discovery.PreflightError) as caught:
                server._require_windows_service_launch(PureWindowsPath(CONFIG), {"port": 12345})
        self.assertEqual(caught.exception.code, "windows_launch_unverified")


class DispatchTests(unittest.TestCase):
    def test_windows_menus_receive_same_real_controller(self):
        control = Mock()
        for name in ("supply", "route", "strategy"):
            with self.subTest(name=name), patch.object(dispatch.sys, "platform", "win32"), \
                 patch("core.cli.windows_control.WindowsControl", return_value=control), \
                 patch.object(dispatch, "config_path", return_value=CONFIG), \
                 patch.object(dispatch, "menu", return_value=0) as menu:
                self.assertEqual(dispatch.main([name], root=ROOT), 0)
                self.assertIs(menu.call_args.args[2], control)

    def test_readonly_menu_still_works_if_controller_cannot_be_constructed(self):
        result = SimpleNamespace(returncode=0, stdout=b"fake\n__RELOAD__=no\n")
        with patch.object(dispatch.sys, "platform", "win32"), \
             patch("core.cli.windows_control.WindowsControl", side_effect=ValueError("fixture")), \
             patch.object(dispatch, "config_path", return_value=CONFIG), \
             patch.object(dispatch.os.path, "isfile", return_value=True), \
             patch.object(dispatch.subprocess, "run", return_value=result), \
             patch.object(dispatch, "_interactive_stdin", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.main(["supply"], root=ROOT), 0)

    def test_mac_menu_still_receives_mac_controller(self):
        control = Mock()
        authority = Mock()
        with patch.object(dispatch.sys, "platform", "darwin"), \
             patch("core.cli.authority.MacInstallationAuthority", return_value=authority), \
             patch("core.cli.control.MacControl", return_value=control) as construct, \
             patch.object(dispatch, "config_path", return_value="/tmp/fake-config.json"), \
             patch.object(dispatch, "menu", return_value=0) as menu:
            self.assertEqual(dispatch.main(["route"], root="/tmp/fake-project"), 0)
        construct.assert_called_once_with("/tmp/fake-project", authority=authority,
                                          config_path="/tmp/fake-config.json")
        self.assertIs(menu.call_args.args[2], control)

    def test_switch_saved_but_reload_failed_returns_failure(self):
        control = Mock()
        control.reload.return_value = 9
        def helper(_argv, *, env):
            with open(env["CONFIG_OPS_RELOAD_MARKER"], "w", encoding="utf-8") as marker:
                marker.write("yes")
            return SimpleNamespace(returncode=0)
        error = io.StringIO()
        with patch.object(dispatch.sys, "platform", "win32"), \
             patch("core.cli.windows_control.WindowsControl", return_value=control), \
             patch.object(dispatch, "config_path", return_value=CONFIG), \
             patch.object(dispatch, "write_preflight", return_value=True), \
             patch.object(dispatch.os.path, "isfile", return_value=True), \
             patch.object(dispatch.subprocess, "run", side_effect=helper), \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.main(["switch", "fake", "route"], root=ROOT), 9)
        control.reload.assert_called_once_with()
        self.assertIn("saved, but reload was not confirmed", error.getvalue())

    def test_readonly_helper_does_not_reload(self):
        control = Mock()
        with patch.object(dispatch.os.path, "isfile", return_value=True), \
             patch.object(dispatch.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout=b"fake\n__RELOAD__=no\n")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.run_helper("supply-list", [], CONFIG, control, root=ROOT), 0)
        control.reload.assert_not_called()

    def test_windows_marker_contract_drives_reload_and_strips_stale_fd_marker(self):
        control = Mock()
        control.reload.return_value = 0
        holder = SimpleNamespace(path=r"C:\Temp\model-proxy-cli-fixture\reload", cleanup_ok=False)
        def child(_argv, *, env):
            self.assertEqual(env["CONFIG_OPS_RELOAD_MARKER"], holder.path)
            self.assertNotIn("CONFIG_OPS_RELOAD_MARKER_FD", env)
            return SimpleNamespace(returncode=0)
        with patch.object(dispatch.os, "name", "nt"), \
             patch.object(dispatch.sys, "platform", "win32"), \
             patch.object(dispatch, "Path", PureWindowsPath), \
             patch.object(dispatch.os.path, "isfile", return_value=True), \
             patch.dict(dispatch.os.environ, {"CONFIG_OPS_RELOAD_MARKER_FD": "123"}), \
             patch.object(dispatch, "_windows_marker_scope", return_value=contextlib.nullcontext(holder)), \
             patch.object(dispatch, "_read_windows_marker", return_value="yes"), \
             patch.object(dispatch.subprocess, "run", side_effect=child), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.run_helper("switch", ["fake", "route"], CONFIG, control, root=ROOT), 0)
        self.assertTrue(holder.cleanup_ok)
        control.reload.assert_called_once_with()

    def test_private_windows_marker_initialization_uses_b_writer_contract(self):
        from core import platform_windows as native, windows_secure_files as secure
        directory = r"C:\Temp\model-proxy-cli-" + "a" * 32
        marker = directory + r"\reload"
        created = SimpleNamespace(created=True, identity=("fixture",), protection=("private",))
        empty = Mock(exists=False)
        ready = Mock(exists=True, contents=b"started")
        write = Mock(return_value=SimpleNamespace(state="VERIFIED"))
        with patch.object(dispatch.tempfile, "gettempdir", return_value=r"C:\Temp"), \
             patch.object(dispatch.secrets, "token_hex", return_value="a" * 32), \
             patch.object(native, "ensure_private_directory", return_value=created) as ensure, \
             patch.object(secure, "capture_target", side_effect=[
                 contextlib.nullcontext(empty), contextlib.nullcontext(ready)]) as capture, \
             patch.object(secure, "write_target", write), \
             patch.object(dispatch, "_cleanup_windows_marker") as cleanup:
            with dispatch._windows_marker_scope() as holder:
                self.assertEqual(holder.path, marker)
                holder.cleanup_ok = True
        ensure.assert_called_once_with(directory)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(write.call_args.args, (empty, b"started"))
        ready.revalidate.assert_called_once_with()
        cleanup.assert_called_once_with(directory, created)

    def test_windows_marker_and_b_writer_round_trip_with_fake_win32_backend(self):
        from core import platform_windows as native, windows_acl, windows_secure_files as secure
        from core.cli import _config_ops
        from core.windows_paths import WindowsPathBackend
        from tests.test_windows_adapter import VirtualACL
        from tests.test_windows_discovery import PathWinBridge
        from tests.test_windows_record_commit import RecordFS
        fs = RecordFS()
        fake_root = r"C:\fixture\project"
        acl_backend = VirtualACL(fs)
        paths_backend = WindowsPathBackend(win32=PathWinBridge(fs), acl_backend=acl_backend)
        counter = itertools.count(12000)
        def mkdir(path, *, user_sid):
            if path in fs.objects:
                raise OSError("already exists")
            fs.objects[path] = fs.obj(next(counter), directory=True)
        def create(path, *, user_sid, original_handle=None):
            handle = fs.open_file(path, True)
            fs.handles[handle][1]["index"] = next(counter)
            return handle
        fs.create_private_directory = mkdir
        fs.open_private_candidate = create
        token_counter = itertools.count(1)
        def token(length):
            return f"{next(token_counter):0{2 * length}x}"
        with patch.object(os, "name", "nt"), \
             patch.object(dispatch.tempfile, "gettempdir", return_value=fake_root), \
             patch.object(dispatch.secrets, "token_hex", side_effect=token), \
             patch.object(discovery, "_windows_path_backend", return_value=paths_backend), \
             patch.object(secure, "_windows_path_backend", return_value=paths_backend), \
             patch.object(native, "_api", side_effect=lambda backend=None: backend if backend is not None else fs), \
             patch.object(windows_acl, "_NativeBackend", return_value=acl_backend), \
             patch.object(dispatch, "_cleanup_windows_marker") as cleanup:
            with dispatch._windows_marker_scope() as holder:
                self.assertEqual(fs.objects[holder.path]["data"], b"started")
                baseline = secure.capture_target(holder.path, authorized_root=ntpath.dirname(holder.path), max_bytes=64)
                with patch.object(_config_ops, "_MARKER_REQUIRED", True), \
                     patch.object(_config_ops, "_MARKER_BASELINE", baseline), \
                     patch.object(_config_ops, "_MARKER_CONTENT", None), \
                     patch.object(_config_ops, "_WRITE_COMMITTED", True), \
                     patch.dict(os.environ, {"CONFIG_OPS_RELOAD_MARKER": holder.path}):
                    _config_ops._set_marker("yes")
                self.assertEqual(dispatch._read_windows_marker(holder.path), "yes")
                holder.cleanup_ok = True
        cleanup.assert_called_once()
        self.assertFalse(fs.handles)

    def test_unexpected_marker_artifact_is_not_deleted(self):
        from core import windows_acl, windows_paths, platform_windows
        directory = r"C:\Temp\model-proxy-cli-fixture"
        created = SimpleNamespace(identity=(17, 11, 1, 16), protection=("private",))
        lease = Mock()
        lease.evidence.exists = True
        lease._handles = [SimpleNamespace(raw=123)]
        proof = SimpleNamespace(observation=SimpleNamespace(identity=created.identity))
        entry = SimpleNamespace(name="unrelated", path=directory + r"\unrelated")
        with patch("core.discovery._windows_path_backend", return_value=Mock()), \
             patch.object(windows_paths, "inspect", return_value=lease), \
             patch.object(windows_acl, "read_stable_evidence", return_value=proof), \
             patch.object(platform_windows, "acl_protection", return_value=created.protection), \
             patch.object(dispatch.os, "scandir", return_value=contextlib.nullcontext(iter([entry]))), \
             patch.object(dispatch.os, "unlink") as unlink, \
             patch.object(dispatch.os, "rmdir") as rmdir:
            with self.assertRaises(RuntimeError):
                dispatch._cleanup_windows_marker(directory, created)
        unlink.assert_not_called()
        rmdir.assert_not_called()

    def test_saved_config_with_reload_exception_is_reported_as_saved(self):
        control = Mock()
        control.reload.side_effect = RuntimeError("fake failure")
        def child(_argv, *, env):
            with open(env["CONFIG_OPS_RELOAD_MARKER"], "w", encoding="utf-8") as marker:
                marker.write("yes")
            return SimpleNamespace(returncode=0)
        error = io.StringIO()
        with patch.object(dispatch.sys, "platform", "linux"), \
             patch.object(dispatch.os.path, "isfile", return_value=True), \
             patch.object(dispatch.subprocess, "run", side_effect=child), \
             contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.run_helper("switch", ["fake", "route"], CONFIG, control, root=ROOT), 1)
        self.assertIn("Configuration saved, but reload was not confirmed", error.getvalue())


class MacRuntimeContractTests(unittest.TestCase):
    def test_missing_or_broken_runtime_file_rejected_one_missing_field_defaults(self):
        with tempfile.TemporaryDirectory(prefix="mp-mac-runtime-fake-") as directory:
            root = Path(directory)
            source = root / "runtime_paths.json"
            with self.assertRaises(RuntimePathsError):
                read_runtime_paths(source, project_root=root, platform="posix")
            source.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimePathsError):
                read_runtime_paths(source, project_root=root, platform="posix")
            data = {key: "runtime/" + key for key in PATH_KEYS}
            del data["lock"]
            parsed = parse_runtime_paths(data, project_root=root, platform="posix")
            self.assertEqual(str(parsed["lock"]), LEGACY_DEFAULTS["lock"])

    def test_mac_runtime_error_stops_before_listener_or_logging(self):
        with patch.object(server.sys, "platform", "darwin"), \
             patch.object(server.os, "name", "posix"), \
             patch.object(server, "_mac_private_config_snapshot", return_value=None), \
             patch.object(server, "resolve_listen", return_value={"port": 1}), \
             patch.object(server, "resolve_runtime_paths", side_effect=RuntimePathsError(
                 "unreadable_file", "fake runtime unavailable")), \
             patch.object(server, "ProxyHTTPServer") as listener, \
             patch.object(server, "init_logging") as logging, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        listener.assert_not_called()
        logging.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "native macOS path behavior")
    def test_mac_symlink_dotdot_target_and_retarget_rejection(self):
        from core.mac_config_path import MacConfigError, capture_config, verify_config
        with tempfile.TemporaryDirectory(prefix="mp-a-mac-link-", dir="/private/tmp") as directory:
            root = Path(directory)
            (root / "config" / "inner").mkdir(parents=True)
            (root / "other" / "decoy-inner").mkdir(parents=True)
            target = root / "config" / "business.json"
            decoy = root / "other" / "business.json"
            target.write_text('{"fixture":"actual"}', encoding="utf-8")
            decoy.write_text('{"fixture":"decoy"}', encoding="utf-8")
            target.chmod(0o600)
            decoy.chmod(0o600)
            pivot = root / "other" / "pivot"
            pivot.symlink_to(root / "config" / "inner", target_is_directory=True)
            spelling = str(root / "other" / "pivot" / ".." / "business.json")
            snapshot = capture_config(spelling)
            self.assertEqual(snapshot.data, b'{"fixture":"actual"}')
            pivot.unlink()
            pivot.symlink_to(root / "other" / "decoy-inner", target_is_directory=True)
            with self.assertRaises(MacConfigError):
                verify_config(snapshot)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"fixture":"actual"}')
            self.assertEqual(decoy.read_text(encoding="utf-8"), '{"fixture":"decoy"}')


if __name__ == "__main__":
    unittest.main()
