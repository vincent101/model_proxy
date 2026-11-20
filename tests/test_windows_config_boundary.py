"""Targeted fake Win32 config-boundary wiring tests; no real services/configs."""
from dataclasses import replace
import os
from pathlib import PureWindowsPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import config_snapshot as config, discovery, platform_windows as win, server
from core import windows_acl as acl
from core.runtime_paths import PATH_KEYS
from core.windows_paths import WindowsPathBackend
from tests.test_windows_acl import OTHER
from tests.test_windows_adapter import VirtualACL
from tests.test_windows_config_snapshot import ConfigFS
from tests.test_windows_discovery import PathWinBridge


class RootOwnerACL(VirtualACL):
    def read_observation(self, handle, kind, required):
        result = super().read_observation(handle, kind, required)
        if self.api.handles[handle][0] in ("C:\\", "D:\\"):
            return replace(result, owner=OTHER)
        return result


class WindowsConfigBoundaryTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    BUSINESS = ROOT + r"\config.json"
    RUNTIME = ROOT + r"\config\runtime_paths.json"
    EXTERNAL = r"C:\fixture\external\business.json"

    def setUp(self):
        self.fs = ConfigFS()
        self.fs.objects[self.BUSINESS] = dict(self.fs.obj(801), data=b'{"generation":"A"}')
        self.fs.objects[self.ROOT + r"\config"] = self.fs.obj(802, directory=True)
        self.fs.objects[self.RUNTIME] = dict(self.fs.obj(803), data=b'{}')
        self.fs.objects[r"C:\fixture\external"] = self.fs.obj(804, directory=True)
        self.fs.objects[self.EXTERNAL] = dict(self.fs.obj(805), data=b'{"generation":"external"}')
        self.acl = RootOwnerACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        patches = (
            patch.object(os, "name", "nt"),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
            patch.object(server, "_PACKAGE_DIR", PureWindowsPath(self.ROOT)),
            patch.object(server, "_RUNTIME_PATHS_FILE", PureWindowsPath(self.RUNTIME)),
            patch.object(server, "_business_path", side_effect=lambda p: p),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.assertFalse(self.fs.handles)

    def test_explicit_boundary_bypasses_only_root_acl_and_keeps_target_checks(self):
        with self.assertRaises(config.ConfigSnapshotError):
            config.read_stable_config(self.BUSINESS)
        with self.assertRaises(config.ConfigSnapshotError):
            config.current_file_marker(self.BUSINESS)
        snapshot = config.read_stable_config(self.BUSINESS, trusted_boundary=self.ROOT)
        self.assertEqual(config.current_file_marker(self.BUSINESS, trusted_boundary=self.ROOT), snapshot.marker)
        self.fs.objects[self.BUSINESS]["unsafe_group"] = True
        with self.assertRaises(config.ConfigSnapshotError):
            config.read_stable_config(self.BUSINESS, trusted_boundary=self.ROOT)
        with self.assertRaises(config.ConfigSnapshotError):
            config.current_file_marker(self.BUSINESS, trusted_boundary=self.ROOT)
        self.fs.objects[self.BUSINESS]["unsafe_group"] = False
        self.fs.objects[self.BUSINESS]["attributes"] = 0x400
        with self.assertRaises(config.ConfigSnapshotError):
            config.read_stable_config(self.BUSINESS, trusted_boundary=self.ROOT)
        self.fs.objects[self.BUSINESS]["attributes"] = 0
        self.fs.objects[self.ROOT]["unsafe_group"] = True
        with self.assertRaises(config.ConfigSnapshotError):
            config.read_stable_config(self.BUSINESS, trusted_boundary=self.ROOT)

    def test_store_init_auto_manual_reload_and_external_parent(self):
        with patch.object(win, "read_guarded_file", wraps=win.read_guarded_file) as guarded:
            store = server.ConfigStore(self.BUSINESS)
            self.assertEqual(store._config["generation"], "A")
            obj = self.fs.objects[self.BUSINESS]
            obj["data"], obj["change"] = b'{"generation":"B"}', 3000
            self.assertTrue(store.maybe_reload())
            self.assertEqual(store._config["generation"], "B")
            obj["data"], obj["change"] = b'{"generation":"C"}', 4000
            self.assertTrue(store.reload())
            self.assertEqual(store._config["generation"], "C")
            external = server.ConfigStore(self.EXTERNAL)
            self.assertEqual(external._config["generation"], "external")
            self.assertEqual(server._business_marker(self.EXTERNAL), external._probe_marker)
            for call in guarded.call_args_list:
                target = call.args[0]
                expected = self.ROOT if target == self.BUSINESS else r"C:\fixture\external"
                self.assertEqual(call.kwargs["trusted_boundary"], expected)
            self.assertTrue(any(not c.kwargs["with_data"] for c in guarded.call_args_list))
            obj["unsafe_group"] = True
            self.assertFalse(store.maybe_reload())
            self.assertFalse(store.reload())
            self.assertEqual(store._config["generation"], "C")

    def test_project_on_another_fake_volume_uses_its_own_root(self):
        root = r"D:\other\project"
        business = root + r"\config.json"
        runtime = root + r"\runtime_paths.json"
        self.fs.objects["D:\\"] = self.fs.obj(910, directory=True)
        self.fs.objects[r"D:\other"] = self.fs.obj(911, directory=True)
        self.fs.objects[root] = self.fs.obj(912, directory=True)
        self.fs.objects[business] = dict(self.fs.obj(913), data=b'{"generation":"D"}')
        self.fs.objects[runtime] = dict(self.fs.obj(914), data=b'{}')
        with patch.object(server, "_PACKAGE_DIR", PureWindowsPath(root)), \
             patch.object(server, "_RUNTIME_PATHS_FILE", PureWindowsPath(runtime)), \
             patch.object(win, "read_guarded_file", wraps=win.read_guarded_file) as guarded:
            with self.assertRaises(config.ConfigSnapshotError):
                config.read_stable_config(business)
            store = server.ConfigStore(business)
            self.assertEqual(store._config["generation"], "D")
            self.assertEqual(server._business_marker(business), store._probe_marker)
            self.assertEqual(server._runtime_paths_marker(), server._read_runtime_paths_config().marker)
            self.assertTrue(all(c.kwargs["trusted_boundary"] == root for c in guarded.call_args_list[1:]))

    def test_ambiguous_project_alias_rejected_not_treated_as_external(self):
        alias = r"C:\fixture\PROJECT\config.json"
        with self.assertRaises(config.ConfigSnapshotError):
            server._read_business_config(alias)
        with self.assertRaises(config.ConfigSnapshotError):
            server._business_marker(alias)
        alias_short = r"C:\fixture\PROJEC~1\config.json"
        with self.assertRaises(config.ConfigSnapshotError):
            server._read_business_config(alias_short)

    def test_preflighted_runtime_roles_and_startup_totals_keep_their_boundaries(self):
        paths = {key: r"C:\fixture\runtime" + "\\" + key for key in PATH_KEYS}
        self.fs.objects[paths["log"]] = dict(self.fs.obj(900), data=b"fake log")
        self.fs.objects[paths["totals"]] = dict(self.fs.obj(901), data=b"{}")
        snapshot = discovery.inspect_runtime_identities(self.ROOT, paths, windows_light=True)
        with patch.object(server, "Path", PureWindowsPath), \
             patch.object(win, "read_guarded_file", wraps=win.read_guarded_file) as guarded:
            observations = dict(server._check_windows_legacy_observations(snapshot))
            self.assertEqual(observations["log"].inode, 900)
            self.assertEqual(observations["totals"].inode, 901)
            ledger = server._capture_migration_ledger(snapshot)
            self.assertIsInstance(ledger, server.LedgerUnavailable)
            self.assertEqual(ledger.marker, observations["totals"])
            for call in guarded.call_args_list:
                self.assertEqual(call.kwargs["trusted_boundary"], r"C:\fixture\runtime")
            # A lookalike without a preflighted role keeps strict defaults.
            unproven = SimpleNamespace(as_dict=snapshot.as_dict)
            with self.assertRaises(discovery.PreflightError):
                server._capture_migration_ledger(unproven)
            self.assertIsNone(guarded.call_args.kwargs["trusted_boundary"])
            self.fs.objects[paths["totals"]]["unsafe_group"] = True
            with self.assertRaises(discovery.PreflightError):
                server._capture_migration_ledger(snapshot)

    def test_confirmation_and_migration_guard_use_same_config_boundaries(self):
        store = server.ConfigStore(self.EXTERNAL)
        items = {key: object() for key in (*PATH_KEYS, "state_dir", "anchor.lock")}
        items["state_dir"] = SimpleNamespace(canonical=self.ROOT + r"\.runtime")
        snapshot = SimpleNamespace(as_dict=lambda: items)
        legacy = SimpleNamespace(snapshot=snapshot)
        listen, runtime, ledger = object(), object(), object()
        with patch.object(server, "Path", PureWindowsPath), \
             patch.object(server, "_runtime_paths", runtime), \
             patch.object(server, "resolve_listen", return_value=listen), \
             patch.object(server, "resolve_runtime_paths", return_value=runtime), \
             patch.object(server, "_check_config_targets"), \
             patch.object(server, "_check_legacy_observations", return_value=()), \
             patch.object(server, "_capture_migration_ledger", return_value=ledger), \
             patch.object(server, "verify_held_instance_locks"), \
             patch.object(server, "inspect_runtime_identities", return_value=snapshot), \
             patch.object(discovery, "read_windows_active", return_value=None), \
             patch.object(win, "read_guarded_file", wraps=win.read_guarded_file) as guarded:
            server._capture_confirmation_snapshot(self.EXTERNAL, store, listen, legacy)
            guard = server._capture_migration_guard(self.EXTERNAL, store, listen, legacy)
            server._verify_migration_guard(guard, self.EXTERNAL, store, listen, None, legacy,
                                           {"windows_active": None})
            self.assertGreaterEqual(len(guarded.call_args_list), 10)
            for call in guarded.call_args_list:
                target = call.args[0]
                expected = self.ROOT if target == self.RUNTIME else r"C:\fixture\external"
                self.assertIn(target, (self.EXTERNAL, self.RUNTIME))
                self.assertEqual(call.kwargs["trusted_boundary"], expected)
            for path in (self.EXTERNAL, self.RUNTIME):
                calls = [c for c in guarded.call_args_list if c.args[0] == path]
                self.assertTrue(any(c.kwargs["with_data"] for c in calls))
                self.assertTrue(any(not c.kwargs["with_data"] for c in calls))


class PosixCompatibilityTests(unittest.TestCase):
    def test_optional_boundary_does_not_change_posix_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "fake.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"fixture":true}')
            with patch.object(config, "verify_local_filesystem_fd"):
                a = config.read_stable_config(path)
                b = config.read_stable_config(path, trusted_boundary="ignored-on-posix")
                self.assertEqual(a, b)
                self.assertEqual(config.current_file_marker(path),
                                 config.current_file_marker(path, trusted_boundary="ignored-on-posix"))


if __name__ == "__main__":
    unittest.main()
