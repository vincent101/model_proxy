"""Windows discovery/lock integration with synthetic in-memory Win32 only."""
from __future__ import annotations

import os
import copy
import io
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock, patch

import core.discovery as discovery
import core.server as server
import core.platform_lock as locks
import core.platform_windows as win
import core.windows_acl as acl
from core.runtime_paths import DEFAULTS, LEGACY_DEFAULTS, PATH_KEYS, parse_runtime_paths
from core.windows_paths import WindowsPathBackend, inspect, may_contain
from tests.test_windows_adapter import VirtualWindows, VirtualCRT, VirtualACL
from tests.test_windows_acl import raw_ace


class VirtualFS(VirtualWindows):
    def __init__(self):
        super().__init__()
        self.objects = {
            "C:\\": self.obj(1, directory=True),
            r"C:\fixture": self.obj(2, directory=True),
            r"C:\fixture\project": self.obj(11, directory=True),
            r"C:\fixture\project\.runtime": self.obj(12, directory=True),
            # 生产把 runtime_paths.json 放在 <project>\config\ 下；虚拟卷补上该目录，
            # 否则所有走 config 角色预检的用例都会判「父目录未观测」。
            r"C:\fixture\project\config": self.obj(14, directory=True),
            r"C:\fixture\runtime": self.obj(13, directory=True),
        }
        self.next_index = 1000

    def _open(self, path, typ, create=False):
        handle = super()._open(path, typ, create)
        if create:
            self.handles[handle][1]["index"] = self.next_index
            self.next_index += 1
        return handle


class PathWinBridge:
    """Win32-shaped bridge; not a real OS and not proof of Win32 behavior."""
    def __init__(self, fs):
        self.fs = fs
        self.release_unknown = False
    def volume_info(self, drive): return self.fs.volume_info(drive + "\\")
    def open_existing(self, path, access, share, flags):
        if (access, share, flags) != (0x20081, 3, 0x2200000):
            raise AssertionError("unsafe fake open")
        return self.fs._open(path, "probe")
    def file_info(self, handle):
        info = self.fs.file_info(handle)
        return (int(info.volume_serial), (int(info.index_high) << 32) | int(info.index_low),
                int(info.links), int(info.attributes))
    def close(self, handle):
        try:
            self.fs.close(handle)
        except BaseException:
            self.release_unknown = True
            raise


class WindowsDiscoveryLockTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    RUNTIME = r"C:\fixture\runtime"
    STATE = r"C:\fixture\project\.runtime"

    def setUp(self):
        self.fs = VirtualFS()
        self.crt = VirtualCRT(self.fs)
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.paths = {key: self.RUNTIME + "\\" + key for key in PATH_KEYS}
        patches = (
            patch.object(os, "name", "nt"),
            patch.object(locks, "_windows_release_unknown", False),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(win, "_NativeFDAPI", return_value=self.crt),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def snapshot(self, *, light=False):
        return discovery.inspect_runtime_identities(self.ROOT, self.paths,
                                                   windows_light=light)

    def test_unified_default_preflight_before_and_after_private_creation(self):
        self.paths = {key: self.STATE + "\\" + name for key, name in {
            "log": "model_proxy.log", "totals": "model_proxy_totals.json",
            "lock": "model_proxy.lock", "pid": "model_proxy.pid",
            "ensure_log": "model_proxy_ensure.log",
            "start_lock": "model_proxy_start.lock",
        }.items()}
        del self.fs.objects[self.STATE]
        first = self.snapshot()
        self.assertFalse(first.as_dict()["state_dir"].exists)
        calls = []

        def create(path, *, trusted_boundary):
            self.assertEqual(trusted_boundary, self.ROOT)
            calls.append(path)
            self.fs.objects[path] = self.fs.obj(12, directory=True)
            return SimpleNamespace(created=True, identity=(17, 12, 1, 16),
                                   protection=("private",))

        with patch.object(discovery, "_windows_open_preflight",
                          wraps=discovery._windows_open_preflight) as preflight:
            discovery.prepare_windows_runtime_directories(
                self.ROOT, self.paths, None, ensure_directory=create)
        self.assertEqual(calls, [self.STATE])
        self.assertEqual(preflight.call_count, 2)
        self.assertTrue(self.snapshot().as_dict()["state_dir"].exists)

    def test_first_start_conflict_cannot_create_directory(self):
        del self.fs.objects[self.STATE]
        self.paths["lock"] = self.STATE + r"\same"
        self.paths["pid"] = self.STATE + r"\same"
        create = Mock()
        with self.assertRaises(discovery.PreflightError):
            discovery.prepare_windows_runtime_directories(
                self.ROOT, self.paths, None, ensure_directory=create)
        create.assert_not_called()

    def test_light_preflight_still_rejects_collision_with_all_fixed_records(self):
        for fixed in ("install.json", "last_good_paths.json"):
            with self.subTest(fixed=fixed):
                paths = dict(self.paths)
                paths["log"] = self.STATE + "\\" + fixed
                with self.assertRaises(discovery.PreflightError) as caught:
                    discovery.inspect_runtime_identities(self.ROOT, paths, windows_light=True)
                self.assertEqual(caught.exception.code, "same_identity")
                self.assertFalse(self.fs.handles)

    def test_project_case_alias_never_uses_weaker_external_parent_boundary(self):
        # An alternate project spelling can resolve to the same directory on
        # Windows. Treating it as external would skip the unsafe intermediate.
        alias = r"C:\fixture\PROJECT"
        intermediate = alias + r"\unsafe"
        self.fs.objects[alias] = self.fs.objects[self.ROOT]
        self.fs.objects[intermediate] = self.fs.obj(70, directory=True)
        target = intermediate + r"\log"
        self.paths["log"] = target
        with patch("core.windows_paths.inspect", wraps=inspect) as inspect_path:
            with self.assertRaises(discovery.PreflightError) as caught:
                self.snapshot()
        self.assertEqual(caught.exception.code, "windows_boundary_unknown")
        self.assertFalse(any(call.args[0] == target for call in inspect_path.call_args_list))
        self.assertFalse(self.fs.handles)

    def test_legacy_creation_uses_parser_provenance_not_an_equal_custom_path(self):
        local = r"C:\fixture\Local"
        self.fs.objects[local] = self.fs.obj(80, directory=True)
        raw = dict(DEFAULTS)
        raw["lock"] = LEGACY_DEFAULTS["lock"]
        legacy = parse_runtime_paths(raw, project_root=self.ROOT, platform="windows",
                                     local_appdata=local).map_values(str)
        raw["lock"] = legacy["lock"]
        custom = parse_runtime_paths(raw, project_root=self.ROOT, platform="windows",
                                     local_appdata=local).map_values(str)
        create = Mock()
        with self.assertRaises(discovery.PreflightError):
            discovery.prepare_windows_runtime_directories(
                self.ROOT, custom, local, ensure_directory=create)
        create.assert_not_called()
        calls = []
        def safe_create(path, *, trusted_boundary):
            self.assertEqual(trusted_boundary, self.ROOT if path == self.STATE else local)
            calls.append(path)
            if path not in self.fs.objects:
                self.fs.objects[path] = self.fs.obj(100 + len(calls), directory=True)
                created = True
            else:
                created = False
            info = self.fs.objects[path]
            return SimpleNamespace(created=created, identity=(17, info["index"], 1, 16),
                                   protection=("private",))
        discovery.prepare_windows_runtime_directories(
            self.ROOT, legacy, local, ensure_directory=safe_create)
        self.assertEqual(calls, [self.STATE, local + r"\model_proxy",
                                 local + r"\model_proxy\runtime"])
        self.assertFalse(self.fs.handles)

    def test_fresh_default_status_and_off_do_not_create_or_contact_process(self):
        from core.cli.windows_control import WindowsControl
        del self.fs.objects[self.STATE]
        paths = parse_runtime_paths(dict(DEFAULTS), project_root=self.ROOT,
                                    platform="windows").map_values(str)
        listen = {"host": "127.0.0.1", "port": 19431}
        out, err = io.StringIO(), io.StringIO()
        control = WindowsControl(self.ROOT, out=out, err=err)
        before = copy.deepcopy(self.fs.objects)
        with patch.object(control, "_configuration", return_value=(paths, listen)), \
             patch.object(control, "_locator_hint", return_value=False), \
             patch.object(control, "_probe_port") as port, \
             patch.object(control, "_request") as request, \
             patch("core.windows_process.open_verified_process") as process, \
             patch.object(discovery, "prepare_windows_runtime_directories") as prepare:
            self.assertEqual(control.status(), 0, err.getvalue())
            self.assertEqual(control.off(), 0, err.getvalue())
        self.assertEqual(port.call_count, 4)
        request.assert_not_called()
        process.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(self.fs.objects, before)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_native_bridge_requires_matching_volume_and_acl_capability(self):
        import core.windows_paths as paths
        first = Mock()
        second = Mock()
        volume = (3, "NTFS", 17, r"\Device\HarddiskVolume1")
        first.volume_info.return_value = volume
        second.volume_info.return_value = volume
        with patch.object(paths, "NativeWin32", return_value=first), \
             patch.object(win, "_NativeAPI", return_value=second):
            bridge = discovery._WindowsNativeBridge()
            self.assertEqual(bridge.volume_info("C:"), volume)
            second.volume_info.assert_called_once_with("C:\\")
            second.volume_info.return_value = (3, "NTFS", 18, volume[3])
            with self.assertRaises(OSError):
                bridge.volume_info("C:")
            second.volume_info.side_effect = OSError("ACL capability unavailable")
            with self.assertRaises(OSError):
                bridge.volume_info("C:")

    def test_immutable_snapshot_contains_no_live_handles(self):
        snapshot = self.snapshot()
        self.assertIsInstance(snapshot, discovery.PathPreflight)
        self.assertIsInstance(snapshot, discovery.WindowsPathPreflight)
        self.assertTrue(snapshot.as_dict()["state_dir"].exists)
        self.assertFalse(snapshot.as_dict()["anchor.lock"].exists)
        self.assertIsNone(snapshot.as_dict()["state_dir"].mode)
        self.assertFalse(self.fs.handles)
        with self.assertRaises(FrozenInstanceError):
            snapshot.windows_evidence = ()
        self.assertEqual(snapshot, self.snapshot())
        self.assertFalse(self.fs.handles)

    def test_anchor_legacy_order_single_fd_owner_and_parent_leases(self):
        snapshot = self.snapshot()
        anchor = locks.acquire_lock(snapshot, "anchor.lock", create=True,
                                    access_verifier=locks.verify_local_access)
        try:
            self.assertEqual(anchor.role, "anchor.lock")
            self.assertTrue(anchor.snapshot.as_dict()["anchor.lock"].exists)
            self.assertIn(anchor.fileno(), self.crt.mapping)
            with self.assertRaises(locks.LockError) as caught:
                anchor.close() if anchor._children else locks.acquire_lock(
                    snapshot, "lock", create=True, anchor=anchor, access_verifier=locks.verify_local_access)
            self.assertEqual(caught.exception.code, "lock_order")  # stale pre-anchor snapshot
            legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor,
                                        access_verifier=locks.verify_local_access)
            self.assertIs(legacy._parent, anchor)
            self.assertTrue(legacy.snapshot.as_dict()["lock"].exists)
            with self.assertRaises(locks.LockError) as caught:
                anchor.close()
            self.assertEqual(caught.exception.code, "lock_order")
            legacy_fd, anchor_fd = legacy.fileno(), anchor.fileno()
            legacy.close()
            self.assertFalse(anchor.closed)
            anchor.close()
            self.assertEqual(self.crt.close_calls.count(legacy_fd), 1)
            self.assertEqual(self.crt.close_calls.count(anchor_fd), 1)
            self.assertFalse(self.fs.handles)
            self.assertFalse(self.fs.locked)
        finally:
            if not anchor.closed:
                anchor.close()

    def test_server_lock_identities_use_held_windows_handles_not_posix_stat(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        try:
            with patch.object(server.os, "fstat", side_effect=AssertionError("POSIX stat forbidden")):
                identities = server._lock_identities(anchor, legacy)
            self.assertEqual(identities["anchor"]["device"], 17)
            self.assertEqual(identities["legacy"]["device"], 17)
            self.assertNotEqual(identities["anchor"]["inode"], identities["legacy"]["inode"])
        finally:
            legacy.close()
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_held_lock_verifier_reopens_all_roles_and_keeps_ownership(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        try:
            discovery.verify_held_instance_locks(anchor, legacy)
            self.assertIn(anchor.fileno(), self.crt.mapping)
            self.assertIn(legacy.fileno(), self.crt.mapping)
            self.assertEqual(len(self.fs.locked), 2)
        finally:
            legacy.close()
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_held_anchor_acl_drift_invalidates_both_but_keeps_lock_order(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        try:
            self.fs.objects[self.STATE + r"\anchor.lock"]["control"] ^= 0x1000
            with self.assertRaises(discovery.PreflightError):
                discovery.verify_held_instance_locks(anchor, legacy)
            self.assertFalse(anchor._windows_guard.closed)
            self.assertEqual(len(self.fs.locked), 2)
            with self.assertRaises(locks.LockError):
                anchor.fileno()
            with self.assertRaises(locks.LockError):
                legacy.fileno()
            with self.assertRaises(locks.LockError) as caught:
                anchor.close()
            self.assertEqual(caught.exception.code, "lock_order")
            legacy.close()
            self.assertEqual(len(self.fs.locked), 1)
            anchor.close()
            self.assertFalse(self.fs.locked)
        finally:
            if not legacy.closed:
                legacy.close()
            if not anchor.closed:
                anchor.close()
        self.assertFalse(self.fs.handles)

    def test_held_other_role_drift_invalidates_evidence_without_unlocking(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        try:
            self.fs.objects[self.paths["log"]] = self.fs.obj(701)
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.verify_held_instance_locks(anchor, legacy)
            self.assertEqual(caught.exception.code, "preflight_changed")
            self.assertEqual(len(self.fs.locked), 2)
            with self.assertRaises(locks.LockError):
                anchor.fileno()
            with self.assertRaises(locks.LockError):
                legacy.fileno()
        finally:
            legacy.close()
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_lock_conflict_does_not_close_existing_owner(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        try:
            with self.assertRaises(locks.LockError) as caught:
                locks.acquire_lock(anchor.snapshot, "anchor.lock")
            self.assertEqual(caught.exception.code, "lock_conflict")
            self.assertFalse(anchor.closed)
            self.assertIn(anchor.fileno(), self.crt.mapping)
        finally:
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_hidden_native_path_close_failure_reports_release_unknown(self):
        root = "C:\\"
        def fail_temporary(typ, path, count):
            if typ == "probe" and path == root and count == 2:
                self.fs.close_fail.add(self.fs.next_handle + 1)
        self.fs.on_open = fail_temporary
        with self.assertRaises(discovery.PreflightError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, "release_unknown")
        failed_handle = next(iter(self.fs.close_fail))
        self.assertEqual(self.fs.close_calls.count(failed_handle), 1)

    def test_hidden_close_failure_during_acquisition_freezes_process(self):
        snapshot = self.snapshot()
        root = "C:\\"
        target_count = self.fs.counts.get(("probe", root), 0) + 2
        def fail_temporary(typ, path, count):
            if typ == "probe" and path == root and count == target_count:
                self.fs.close_fail.add(self.fs.next_handle + 1)
        self.fs.on_open = fail_temporary
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertFalse(caught.exception.created)
        self.assertTrue(locks._windows_release_unknown)
        count = len(self.fs.calls)
        with self.assertRaises(locks.LockError):
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertEqual(len(self.fs.calls), count)
        failed_handle = next(iter(self.fs.close_fail))
        self.assertEqual(self.fs.close_calls.count(failed_handle), 1)

    def test_acl_internal_release_failure_is_not_reported_as_ordinary_denial(self):
        self.acl.read_observation = Mock(side_effect=acl.ACLVerificationError("resource_release_failed"))
        with self.assertRaises(discovery.PreflightError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, "release_unknown")

    def test_held_target_acl_resource_failure_reports_unknown_and_keeps_order(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        anchor_handle = self.crt.handle(anchor.fileno())
        def fail_resource(handle, kind):
            if handle == anchor_handle and kind == "file":
                raise acl.ACLVerificationError("resource_release_failed")
        self.acl.on_read = fail_resource
        try:
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.verify_held_instance_locks(anchor, legacy)
            self.assertEqual(caught.exception.code, "release_unknown")
            self.assertTrue(locks._windows_release_unknown)
            self.assertEqual(len(self.fs.locked), 2)
            with self.assertRaises(locks.LockError):
                anchor.close()
        finally:
            self.acl.on_read = None
            legacy.close()
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_held_guard_temporary_close_failure_invalidates_and_reports_unknown(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        anchor_path = self.STATE + r"\anchor.lock"
        target_count = self.fs.counts.get(("file", anchor_path), 0) + 1
        def fail_target_temp(typ, path, count):
            if typ == "file" and path == anchor_path and count == target_count:
                self.fs.close_fail.add(self.fs.next_handle + 1)
        self.fs.on_open = fail_target_temp
        try:
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.verify_held_instance_locks(anchor, legacy)
            self.assertEqual(caught.exception.code, "release_unknown")
            self.assertTrue(locks._windows_release_unknown)
            self.assertEqual(len(self.fs.locked), 2)
            with self.assertRaises(locks.LockError):
                anchor.fileno()
            with self.assertRaises(locks.LockError):
                legacy.fileno()
        finally:
            legacy.close()
            anchor.close()
        failed_handle = next(iter(self.fs.close_fail))
        self.assertEqual(self.fs.close_calls.count(failed_handle), 1)

    def test_anchor_release_unknown_blocks_same_process_reacquisition(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        fd = anchor.fileno()
        snapshot = anchor.snapshot
        self.crt.fail_close = True
        with self.assertRaises(locks.LockError) as caught:
            anchor.close()
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertTrue(anchor.closed)
        with self.assertRaises(locks.LockError) as caught:
            anchor.close()
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertEqual(self.crt.close_calls.count(fd), 1)
        count = len(self.fs.calls)
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock")
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertEqual(len(self.fs.calls), count)

    def test_unknown_legacy_release_pins_anchor_and_never_retries_fd(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        legacy_fd = legacy.fileno()
        self.crt.fail_close = True
        try:
            with self.assertRaises(locks.LockError) as caught:
                legacy.close()
            self.assertEqual(caught.exception.code, "lock_release_unknown")
            self.assertTrue(legacy.closed)
            with self.assertRaises(locks.LockError) as caught:
                legacy.close()
            self.assertEqual(caught.exception.code, "lock_release_unknown")
            self.assertEqual(self.crt.close_calls.count(legacy_fd), 1)
            with self.assertRaises(locks.LockError) as caught:
                anchor.close()
            self.assertEqual(caught.exception.code, "lock_order")
        finally:
            # Explicit fake-only cleanup; this does not retry the unknown fd.
            self.crt.fail_close = False
            with anchor._state_lock:
                anchor._children = 0
            if not anchor.closed:
                anchor.close()

    def test_reentrant_anchor_close_cannot_bypass_pending_legacy(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        try:
            def attempt_close(*args, **kwargs):
                anchor.close()
            with patch.object(locks, "_acquire_windows_locked", side_effect=attempt_close):
                with self.assertRaises(locks.LockError) as caught:
                    locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
            self.assertEqual(caught.exception.code, "lock_order")
            self.assertEqual(anchor._pending_children, 0)
            self.assertFalse(anchor.closed)
        finally:
            anchor.close()

    def test_acquisition_cleanup_failure_reports_unknown_without_fd_retry(self):
        snapshot = self.snapshot()
        original = self.fs.lock
        def drift_and_fail_close(handle, *, unlock):
            original(handle, unlock=unlock)
            if not unlock:
                self.fs.objects[self.paths["log"]] = self.fs.obj(700)
                self.crt.fail_close = True
        self.fs.lock = drift_and_fail_close
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertEqual(caught.exception.code, "lock_release_unknown")
        self.assertTrue(caught.exception.created)
        self.assertEqual(len(self.crt.close_calls), 1)

    def test_callback_with_forged_equality_is_still_rejected(self):
        class FakeVerifier:
            def __call__(self, path, kind): return True
            def __eq__(self, other): return True
        snapshot = self.snapshot()
        count = len(self.fs.calls)
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=FakeVerifier())
        self.assertEqual(caught.exception.code, "windows_security_unverified")
        self.assertEqual(len(self.fs.calls), count)

    def test_snapshot_repr_does_not_expose_acl_protection_bytes(self):
        snapshot = self.snapshot()
        for _, evidence in snapshot.windows_evidence:
            for component in evidence.components:
                protection = component.observation.protection
                self.assertNotIn(repr(protection), repr(snapshot))

    def test_noncanonical_windows_path_fails_before_open(self):
        paths = dict(self.paths)
        paths["log"] = "C:/fixture/runtime/log"
        before = len(self.fs.calls)
        with self.assertRaises(discovery.PreflightError):
            discovery.inspect_runtime_identities(self.ROOT, paths)
        self.assertEqual(len(self.fs.calls), before)

    def test_boolean_callback_cannot_authorize_windows(self):
        snapshot = self.snapshot()
        count = len(self.fs.calls)
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=lambda p, k: True)
        self.assertEqual(caught.exception.code, "windows_security_unverified")
        self.assertEqual(len(self.fs.calls), count)
        self.assertFalse(self.fs.handles)

    def test_same_runtime_path_and_fixed_state_overlap_reject_before_creation(self):
        for mapping in (dict(self.paths, totals=self.paths["log"]),
                        dict(self.paths, log=self.STATE + r"\anchor.lock")):
            with self.subTest(mapping=mapping), self.assertRaises(discovery.PreflightError):
                discovery.inspect_runtime_identities(self.ROOT, mapping)
            self.assertFalse(self.fs.handles)
            self.assertNotIn(self.STATE + r"\anchor.lock", self.fs.objects)

    def test_hardlinked_roles_are_rejected_by_physical_identity(self):
        shared = self.fs.obj(800)
        self.fs.objects[self.paths["log"]] = shared
        self.fs.objects[self.paths["totals"]] = shared
        with self.assertRaises(discovery.PreflightError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, "same_identity")
        self.assertFalse(self.fs.handles)

    def test_existing_legacy_start_directory_with_safe_shared_read_is_supported(self):
        target = self.paths["start_lock"]
        self.fs.objects[target] = self.fs.obj(704, directory=True)
        original = self.acl.read_observation
        def add_safe_read(handle, kind, required):
            result = original(handle, kind, required)
            if self.fs.handles[handle][0] == target:
                entry = acl._parse_ace(raw_ace(acl._BUILTIN_USERS, mask=acl.FILE_GENERIC_READ))
                from dataclasses import replace
                result = replace(result, aces=result.aces + (entry,))
            return result
        self.acl.read_observation = add_safe_read
        snapshot = self.snapshot()
        self.assertTrue(snapshot.as_dict()["start_lock"].exists)
        self.assertEqual(snapshot.windows_dict()["start_lock"].expected_kind, "directory")
        self.assertFalse(self.fs.handles)

    def test_missing_state_synthesizes_missing_fixed_identities(self):
        """状态目录缺席时，固定记录仍以「明确缺失」身份出现在快照里。

        契约：light 与完整预检都必须能回答「每个固定角色现在是什么样」。
        缺席时不能因为父目录不存在就整体失败（那会把「尚未初始化」误报成
        「证据不可用」），而要合成 exists=False 的身份。
        """
        self.fs.objects.pop(self.STATE)
        for light in (True, False):
            with self.subTest(light=light):
                snapshot = self.snapshot(light=light)
                identities = snapshot.as_dict()
                self.assertFalse(identities["state_dir"].exists)
                for name in ("anchor.lock", "control.lock", "active.json"):
                    self.assertIn(name, identities)
                    self.assertFalse(identities[name].exists, name)
                self.assertFalse(self.fs.handles, "缺席路径不得泄漏句柄")

    def test_initial_missing_state_is_recorded_but_locking_fails_before_io(self):
        self.fs.objects.pop(self.STATE)
        snapshot = self.snapshot()
        self.assertFalse(snapshot.as_dict()["state_dir"].exists)
        self.assertFalse(snapshot.as_dict()["anchor.lock"].exists)
        count = len(self.fs.calls)
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertEqual(caught.exception.code, "state_missing")
        self.assertEqual(len(self.fs.calls), count)
        self.assertFalse(self.fs.handles)

    def test_missing_anchor_without_create_never_opens_a_target(self):
        snapshot = self.snapshot()
        count = len(self.fs.calls)
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=False)
        self.assertEqual(caught.exception.code, "lock_missing")
        self.assertEqual(len(self.fs.calls), count)
        self.assertFalse(self.fs.handles)

    def test_anchor_acl_drift_blocks_legacy_before_creation(self):
        anchor = locks.acquire_lock(self.snapshot(), "anchor.lock", create=True)
        try:
            self.fs.objects[self.STATE + r"\anchor.lock"]["control"] ^= 0x1000
            with self.assertRaises(locks.LockError) as caught:
                locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
            self.assertEqual(caught.exception.code, "preflight_changed")
            self.assertNotIn(self.paths["lock"], self.fs.objects)
            self.assertFalse(anchor.closed)
        finally:
            anchor.close()
        self.assertFalse(self.fs.handles)

    def test_target_acl_change_after_lock_releases_fd_and_reports_creation(self):
        snapshot = self.snapshot()
        original = self.fs.lock
        def mutate_target(handle, *, unlock):
            original(handle, unlock=unlock)
            if not unlock:
                self.fs.handles[handle][1]["control"] ^= 0x1000
        self.fs.lock = mutate_target
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertTrue(caught.exception.created)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertEqual(len(self.crt.close_calls), 1)

    def test_missing_custom_parent_rejects_before_creating_any_lock(self):
        self.paths["log"] = self.RUNTIME + r"\missing\log"
        with self.assertRaises(discovery.PreflightError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, "parent_missing")
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)

    def test_changed_role_after_preflight_prevents_lock_creation(self):
        snapshot = self.snapshot()
        self.fs.objects[self.paths["log"]] = self.fs.obj(700)
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertEqual(caught.exception.code, "preflight_changed")
        self.assertNotIn(self.STATE + r"\anchor.lock", self.fs.objects)
        self.assertFalse(self.fs.handles)

    def test_other_role_acl_drift_after_lock_is_detected_and_released(self):
        self.fs.objects[self.paths["log"]] = self.fs.obj(701)
        snapshot = self.snapshot()
        original = self.fs.lock
        def change_other_acl(handle, *, unlock):
            original(handle, unlock=unlock)
            if not unlock:
                self.fs.objects[self.paths["log"]]["control"] ^= 0x1000
        self.fs.lock = change_other_acl
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertTrue(caught.exception.created)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertEqual(len(self.crt.close_calls), 1)

    def test_new_cross_role_alias_after_lock_is_rejected(self):
        snapshot = self.snapshot()
        original = self.fs.lock
        def alias_after_lock(handle, *, unlock):
            original(handle, unlock=unlock)
            if not unlock:
                self.fs.objects[self.paths["log"]] = self.fs.handles[handle][1]
        self.fs.lock = alias_after_lock
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertTrue(caught.exception.created)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertEqual(len(self.crt.close_calls), 1)

    def test_role_drift_after_lock_releases_lock_and_reports_created(self):
        snapshot = self.snapshot()
        original = self.fs.lock
        def drift(handle, *, unlock):
            original(handle, unlock=unlock)
            if not unlock:
                self.fs.objects[self.paths["log"]] = self.fs.obj(700)
        self.fs.lock = drift
        with self.assertRaises(locks.LockError) as caught:
            locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.assertTrue(caught.exception.created)
        self.assertIn(self.STATE + r"\anchor.lock", self.fs.objects)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertEqual(len(self.crt.close_calls), 1)

    def test_alias_comparison_matches_live_path_implementation(self):
        first = inspect(self.paths["log"], backend=self.backend)
        second = inspect(self.paths["totals"], backend=self.backend)
        try:
            self.assertEqual(discovery._windows_evidence_contains(first.evidence, second.evidence),
                             may_contain(first, second))
            self.assertEqual(discovery._windows_evidence_contains(second.evidence, first.evidence),
                             may_contain(second, first))
        finally:
            first.close()
            second.close()
        self.assertFalse(self.fs.handles)


if __name__ == "__main__":
    unittest.main()
