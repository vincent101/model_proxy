"""Fake Windows startup wiring; no native Windows API or real listener."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager, redirect_stderr
from pathlib import PureWindowsPath
from unittest.mock import Mock, patch

import core.discovery as discovery
import core.platform_lock as locks
import core.server as server
from core.runtime_paths import PATH_KEYS
import tests.test_windows_service_startup as fixtures


class WindowsStartupWiringTests(fixtures.WindowsCoreMockStartupTests):
    @contextmanager
    def _light_startup_fixture(self):
        """Inject only preflight/stop points; lock/ACL behavior stays fake-native."""
        with patch.object(server, "_require_windows_service_launch"), \
             patch.object(discovery, "prepare_windows_runtime_directories"), \
             patch.object(discovery, "prepare_windows_log",
                          side_effect=lambda root, paths, snapshot: snapshot), \
             patch.object(server, "_check_legacy_observations",
                          return_value=tuple((key, None) for key in PATH_KEYS)), \
             patch.object(server, "_require_windows_listener_validation",
                          side_effect=discovery.PreflightError("test_stop_before_bind", "fake stop")):
            yield

    def test_direct_entry_checks_both_locks_and_stops_before_bind(self):
        self.close_locks()
        calls = []
        acquire = server.acquire_lock

        def tracking_acquire(snapshot, role, **kwargs):
            held = acquire(snapshot, role, **kwargs)
            calls.append((role, held))
            return held

        stderr = io.StringIO()
        initial_commits = list(self.fs.commit_calls)
        with self._light_startup_fixture(), patch.object(sys, "platform", "win32"), \
             patch.object(server, "_DEFAULT_CONFIG_PATH", PureWindowsPath(self.config)), \
             patch.object(server, "acquire_lock", side_effect=tracking_acquire), \
             patch.object(server, "verify_record_directory", wraps=server.verify_record_directory) as directory_check, \
             patch.object(server, "_verify_migration_guard", wraps=server._verify_migration_guard) as verify, \
             patch.object(server, "ProxyHTTPServer", side_effect=AssertionError("bind forbidden")) as bind, \
             patch.object(server, "write_instance_record", side_effect=AssertionError("record write forbidden")), \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("test_stop_before_bind", stderr.getvalue())
        self.assertEqual([role for role, _ in calls], ["anchor.lock", "lock"])
        self.assertTrue(all(held.closed for _, held in calls))
        directory_check.assert_called_once()
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.fs.commit_calls, initial_commits)
        bind.assert_not_called()

    def test_windows_listener_capability_is_not_a_permanent_gate(self):
        with patch.object(server.socket, "SO_EXCLUSIVEADDRUSE", 123, create=True):
            server._require_windows_listener_validation()

    def test_environment_assertions_do_not_bypass_listener_gate(self):
        # These are invented adversarial names, not supported configuration.
        with patch.dict(os.environ, {"CI": "1", "MODEL_PROXY_TESTING": "1",
                                    "MODEL_PROXY_WINDOWS_LISTENER_VALIDATED": "1"}), \
             patch.object(server.socket, "SO_EXCLUSIVEADDRUSE", None, create=True):
            with self.assertRaises(discovery.PreflightError) as caught:
                server._require_windows_listener_validation()
        self.assertEqual(caught.exception.code, "windows_listener_exclusivity_unavailable")

    def test_unknown_state_creation_fails_before_lock_or_listener(self):
        self.close_locks()
        for path in list(self.fs.objects):
            if path == self.STATE or path.startswith(self.STATE + "\\"):
                del self.fs.objects[path]
        stderr = io.StringIO()
        before = list(self.fs.commit_calls)
        with self._light_startup_fixture(), patch.object(sys, "platform", "win32"), \
             patch.object(server, "_DEFAULT_CONFIG_PATH", PureWindowsPath(self.config)), \
             patch.object(discovery, "prepare_windows_runtime_directories",
                          side_effect=discovery.PreflightError("windows_creation_unknown", "fake unknown")) as prepare, \
             patch.object(server, "acquire_lock", side_effect=AssertionError("lock forbidden")) as acquire, \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("windows_creation_unknown", stderr.getvalue())
        prepare.assert_called_once()
        self.assertNotIn(self.STATE, self.fs.objects)
        self.assertEqual(self.fs.commit_calls, before)
        acquire.assert_not_called()

    def test_unsafe_state_acl_rejected_before_locks(self):
        self.close_locks()
        # Same virtual directory first passes; adding a dangerous ACE alone
        # must produce the specific directory-protection failure.
        self.assertEqual(discovery.verify_record_directory(PureWindowsPath(self.STATE)), (17, 11))
        self.fs.objects[self.STATE]["unsafe_group"] = True
        with self.assertRaises(discovery.PreflightError) as acl_failure:
            discovery.verify_record_directory(PureWindowsPath(self.STATE))
        self.assertEqual(acl_failure.exception.code, "protection_unavailable")
        stderr = io.StringIO()
        before = list(self.fs.commit_calls)
        with self._light_startup_fixture(), patch.object(sys, "platform", "win32"), \
             patch.object(server, "_DEFAULT_CONFIG_PATH", PureWindowsPath(self.config)), \
             patch.object(server, "acquire_lock", side_effect=AssertionError("lock forbidden")) as acquire, \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("startup failed (config_path_unknown)", stderr.getvalue())
        self.assertNotIn("unexpected_error", stderr.getvalue())
        self.assertEqual(self.fs.commit_calls, before)
        acquire.assert_not_called()

    def _remove_fake_lock_files(self):
        self.close_locks()
        anchor_path = self.STATE + r"\anchor.lock"
        legacy_path = self.paths["lock"]
        self.fs.objects.pop(anchor_path)
        self.fs.objects.pop(legacy_path)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.crt.mapping)
        return anchor_path, legacy_path

    def test_missing_locks_created_and_released_but_retained_at_listener_gate(self):
        anchor_path, legacy_path = self._remove_fake_lock_files()
        events_start = len(self.fs.calls)
        commits_before, writes_before = list(self.fs.commit_calls), list(self.fs.write_calls)
        acquired = []
        real_acquire = server.acquire_lock

        def tracking_acquire(snapshot, role, **kwargs):
            held = real_acquire(snapshot, role, **kwargs)
            acquired.append((role, held, held._windows_guard.created))
            return held

        stderr = io.StringIO()
        with self._light_startup_fixture(), patch.object(sys, "platform", "win32"), \
             patch.object(server, "_DEFAULT_CONFIG_PATH", PureWindowsPath(self.config)), \
             patch.object(server, "acquire_lock", side_effect=tracking_acquire), \
             patch.object(server, "ProxyHTTPServer", side_effect=AssertionError("bind forbidden")) as bind, \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("test_stop_before_bind", stderr.getvalue())
        self.assertEqual([(role, created) for role, _, created in acquired],
                         [("anchor.lock", True), ("lock", True)])
        self.assertTrue(all(held.closed for _, held, _ in acquired))
        self.assertIn(anchor_path, self.fs.objects)
        self.assertIn(legacy_path, self.fs.objects)
        events = self.fs.calls[events_start:]
        self.assertEqual([event[2] for event in events
                          if event[0:2] == ("open", "file") and event[3]],
                         [anchor_path, legacy_path])
        anchor_id = self.fs.objects[anchor_path]["index"]
        legacy_id = self.fs.objects[legacy_path]["index"]
        self.assertEqual([(event[0], event[2]) for event in events if event[0] in ("lock", "unlock")],
                         [("lock", anchor_id), ("lock", legacy_id),
                          ("unlock", legacy_id), ("unlock", anchor_id)])
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.crt.mapping)
        self.assertFalse(locks._windows_release_unknown)
        self.assertEqual(self.fs.commit_calls, commits_before)
        self.assertEqual(self.fs.write_calls, writes_before)
        bind.assert_not_called()

    def _assert_new_lock_acl_drift_releases_every_acquired_lock(self, failing_role):
        anchor_path, legacy_path = self._remove_fake_lock_files()
        target = anchor_path if failing_role == "anchor.lock" else legacy_path
        events_start = len(self.fs.calls)
        commits_before, writes_before = list(self.fs.commit_calls), list(self.fs.write_calls)
        failed, acquired = [], []
        real_lock, real_acquire = self.fs.lock, server.acquire_lock

        def change_acl_after_lock(handle, *, unlock):
            real_lock(handle, unlock=unlock)
            if not unlock and self.fs.handles[handle][0] == target:
                self.fs.handles[handle][1]["control"] ^= 0x1000

        def tracking_acquire(snapshot, role, **kwargs):
            try:
                held = real_acquire(snapshot, role, **kwargs)
            except locks.LockError as exc:
                failed.append((role, exc.code, exc.created))
                raise
            acquired.append((role, held))
            return held

        stderr = io.StringIO()
        with self._light_startup_fixture(), patch.object(sys, "platform", "win32"), \
             patch.object(server, "_DEFAULT_CONFIG_PATH", PureWindowsPath(self.config)), \
             patch.object(self.fs, "lock", side_effect=change_acl_after_lock), \
             patch.object(server, "acquire_lock", side_effect=tracking_acquire), \
             patch.object(server, "ProxyHTTPServer", side_effect=AssertionError("bind forbidden")) as bind, \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(failed, [(failing_role, "security_changed", True)])
        self.assertIn("startup failed (security_changed)", stderr.getvalue())
        self.assertTrue(all(held.closed for _, held in acquired))
        self.assertIn(anchor_path, self.fs.objects)
        if failing_role == "anchor.lock":
            self.assertNotIn(legacy_path, self.fs.objects)
        else:
            self.assertIn(legacy_path, self.fs.objects)
        events = self.fs.calls[events_start:]
        anchor_id = self.fs.objects[anchor_path]["index"]
        expected = [("lock", anchor_id)]
        if failing_role == "lock":
            legacy_id = self.fs.objects[legacy_path]["index"]
            expected += [("lock", legacy_id), ("unlock", legacy_id)]
        expected += [("unlock", anchor_id)]
        self.assertEqual([(event[0], event[2]) for event in events if event[0] in ("lock", "unlock")],
                         expected)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.crt.mapping)
        self.assertFalse(locks._windows_release_unknown)
        self.assertEqual(self.fs.commit_calls, commits_before)
        self.assertEqual(self.fs.write_calls, writes_before)
        bind.assert_not_called()

    def test_new_anchor_acl_drift_retains_file_and_releases_lock(self):
        self._assert_new_lock_acl_drift_releases_every_acquired_lock("anchor.lock")

    def test_new_legacy_acl_drift_retains_both_files_and_releases_locks(self):
        self._assert_new_lock_acl_drift_releases_every_acquired_lock("lock")

    def test_new_lock_unknown_release_pins_anchor_without_retrying_fd(self):
        anchor_path, legacy_path = self._remove_fake_lock_files()
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        anchor = locks.acquire_lock(snapshot, "anchor.lock", create=True)
        legacy = locks.acquire_lock(anchor.snapshot, "lock", create=True, anchor=anchor)
        legacy_fd = legacy.fileno()
        self.assertTrue(anchor._windows_guard.created)
        self.assertTrue(legacy._windows_guard.created)
        self.crt.fail_close = True
        try:
            with self.assertRaises(locks.LockError) as release:
                legacy.close()
            self.assertEqual(release.exception.code, "lock_release_unknown")
            self.assertTrue(legacy.closed)
            self.assertTrue(locks._windows_release_unknown)
            with self.assertRaises(locks.LockError) as retry:
                legacy.close()
            self.assertEqual(retry.exception.code, "lock_release_unknown")
            self.assertEqual(self.crt.close_calls.count(legacy_fd), 1)
            with self.assertRaises(locks.LockError) as pinned:
                anchor.close()
            self.assertEqual(pinned.exception.code, "lock_order")
            self.assertFalse(anchor.closed)
            self.assertIn(anchor_path, self.fs.objects)
            self.assertIn(legacy_path, self.fs.objects)
        finally:
            # Fake-only teardown: do not retry the uncertain legacy fd.
            self.crt.fail_close = False
            with anchor._state_lock:
                anchor._children = 0
            if not anchor.closed:
                anchor.close()

    def test_legacy_lock_conflict_releases_anchor_without_binding(self):
        self.close_locks()
        acquired = []
        real_acquire = server.acquire_lock

        def conflict(snapshot, role, **kwargs):
            if role == "lock":
                raise locks.LockError("lock_conflict")
            held = real_acquire(snapshot, role, **kwargs)
            acquired.append(held)
            return held

        stderr = io.StringIO()
        before = list(self.fs.commit_calls)
        with self._light_startup_fixture(), patch.object(sys, "platform", "win32"), \
             patch.object(server, "_DEFAULT_CONFIG_PATH", PureWindowsPath(self.config)), \
             patch.object(server, "acquire_lock", side_effect=conflict), \
             patch.object(server, "ProxyHTTPServer", side_effect=AssertionError("bind forbidden")) as bind, \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("lock_conflict", stderr.getvalue())
        self.assertEqual(len(acquired), 1)
        self.assertTrue(acquired[0].closed)
        self.assertEqual(self.fs.commit_calls, before)
        bind.assert_not_called()

    def test_corrupt_ledger_degrades_to_unavailable_without_reset_or_writes(self):
        """损坏的账本降级为 LedgerUnavailable，不抛错、不重置、不写入。

        契约变更：只有「稳定的 JSON/schema 失败」可降级；身份、保护、I/O、
        关闭与竞态失败仍然是启动错误。本用例覆盖前者，因此断言降级而不是
        抛 PreflightError。
        """
        self.fs.objects[self.paths["totals"]] = dict(self.fs.obj(720), data=b'{"broken":')
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        before = self.fs.objects[self.paths["totals"]]["data"]
        writes = list(self.fs.write_calls)
        captured = server._capture_migration_ledger(snapshot)
        self.assertIsInstance(captured, server.LedgerUnavailable)
        self.assertEqual(self.fs.objects[self.paths["totals"]]["data"], before)
        self.assertEqual(self.fs.write_calls, writes)

    def test_present_ledger_is_captured_with_guarded_identity_without_writing(self):
        ledger = {"version": 3, "total": server._zero_bucket(), "days": {}, "months_archive": {}}
        data = json.dumps(ledger).encode()
        self.fs.objects[self.paths["totals"]] = dict(self.fs.obj(720), data=data)
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        before = list(self.fs.write_calls)
        captured = server._capture_migration_ledger(snapshot)
        self.assertEqual(captured.config, ledger)
        self.assertEqual((captured.marker.device, captured.marker.inode), (17, 720))
        self.assertEqual(self.fs.objects[self.paths["totals"]]["data"], data)
        self.assertEqual(self.fs.write_calls, before)

    def test_ledger_changed_during_read_is_rejected_without_reset(self):
        ledger = {"version": 3, "total": server._zero_bucket(), "days": {}, "months_archive": {}}
        self.fs.objects[self.paths["totals"]] = dict(self.fs.obj(720), data=json.dumps(ledger).encode())
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)

        def change(handle, obj):
            if self.fs.handles[handle][0] == self.paths["totals"]:
                obj["data"] += b" "
                obj["change"] = obj.get("change", 2000) + 100

        self.fs.on_read = change
        before = list(self.fs.write_calls)
        with self.assertRaises(discovery.PreflightError):
            server._capture_migration_ledger(snapshot)
        self.assertEqual(self.fs.write_calls, before)

    def test_unknown_previous_process_is_never_signalled(self):
        record = {"active": {"runtime_paths": self.paths, "locks": {},
                             "pid": 999, "process_start": {"kind": "windows_filetime", "ticks_100ns": 1}}}
        with self.assertRaises(discovery.PreflightError) as caught:
            server._ensure_stale_instance(record, self.paths, self.listen, {})
        self.assertEqual(caught.exception.code, "process_unverified")
        server.os.kill.assert_not_called()

    def _exercise_published_ready(self, change=None):
        """Run the real bound orchestration on virtual files and a fake server."""
        instances = []
        present_ledger = change in ("ledger", "ledger_snapshot")
        if present_ledger:
            # Reacquire the virtual locks with the existing ledger in their
            # identity snapshot; no real filesystem or service is involved.
            self.close_locks()
            ledger = {"version": 3, "total": server._zero_bucket(), "days": {}, "months_archive": {}}
            self.fs.objects[self.paths["totals"]] = dict(self.fs.obj(720), data=json.dumps(ledger).encode())
            snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
            self.anchor = locks.acquire_lock(snapshot, "anchor.lock", create=True)
            self.legacy = locks.acquire_lock(self.anchor.snapshot, "lock", create=True, anchor=self.anchor)

        injected = {"ready": False, "records": None, "ledger": None}
        original_parent = self.legacy._parent

        class FakeServingServer(fixtures.FakeServer):
            def __init__(self, address, handler):
                super().__init__(address, handler)
                self.served = False
                self.stopped = False
                instances.append(self)

            def serve_forever(self):
                self.served = True

            def shutdown(self):
                self.stopped = True

        real_write = server.write_instance_record

        def write_and_change(state, name, value, **kwargs):
            real_write(state, name, value, **kwargs)
            if name == "active.json" and value["state"] == "READY":
                injected["ready"] = True
                if change == "config":
                    obj = self.fs.objects[self.config]
                    obj["data"] = b'{"changed":true}'
                    obj["change"] = obj.get("change", 2000) + 100
                elif change == "path_config":
                    obj = self.fs.objects[self.path_config]
                    obj["data"] = b'{"changed":"fake path"}'
                    obj["change"] = obj.get("change", 2000) + 100
                elif change == "identity":
                    self.fs.objects[self.paths["pid"]] = dict(self.fs.obj(730), data=b"external fake")
                elif change == "lock":
                    # Simulate loss of the held-pair relationship while retaining
                    # virtual handles for deterministic teardown.
                    self.legacy._parent = None
                elif change == "ledger":
                    obj = self.fs.objects[self.paths["totals"]]
                    obj["data"] += b" "
                    obj["change"] = obj.get("change", 2000) + 100
                elif change == "ledger_snapshot":
                    def mutate_during_read(handle, obj):
                        if self.fs.handles[handle][0] == self.paths["totals"]:
                            obj["data"] += b" "
                            obj["change"] = obj.get("change", 2000) + 100
                            injected["ledger"] = obj["data"]
                            self.fs.on_read = None
                    self.fs.on_read = mutate_during_read
                elif change == "record":
                    obj = self.fs.objects[self.STATE + r"\active.json"]
                    data = json.loads(obj["data"])
                    data["state"] = "STARTING"
                    obj["data"] = json.dumps(data).encode()
                    obj["change"] += 100
                injected["records"] = {name: self.fs.objects[self.STATE + "\\" + name]["data"]
                                       for name in ("install.json", "active.json", "last_good_paths.json")}
                if present_ledger and change != "ledger_snapshot":
                    injected["ledger"] = self.fs.objects[self.paths["totals"]]["data"]

        def resolve_paths():
            if injected["ready"] and change == "runtime_paths":
                return dict(self.paths, pid=self.RUNTIME + r"\other-pid")
            return self.paths

        version = Mock()
        version.read_text.return_value = "fake"
        totals = Mock()
        totals.from_verified_snapshot.return_value = object()
        with patch.object(server, "_require_windows_listener_validation") as gate, \
             patch.object(server, "ProxyHTTPServer", FakeServingServer), \
             patch.object(server, "_VERSION_FILE", version), \
             patch.object(server, "_VERSION", "unknown"), \
             patch.object(server, "usage_totals", None), \
             patch.object(server, "init_logging"), \
             patch.object(server, "UsageTotalsStore", totals), \
             patch.object(server, "SessionOverridesSidecar"), \
             patch.object(server, "resolve_runtime_paths", side_effect=resolve_paths), \
             patch.object(server, "write_instance_record", side_effect=write_and_change), \
             patch.object(server, "_restore_prestart_records", side_effect=AssertionError("rollback forbidden")) as rollback, \
             patch.object(server, "delete_instance_record_if_matches", side_effect=AssertionError("delete forbidden")) as delete, \
             patch.object(server, "_verify_migration_guard", wraps=server._verify_migration_guard) as verify:
            try:
                if change:
                    with self.assertRaises(server.WindowsStartupRecoveryError) as caught:
                        server._run_server(PureWindowsPath(self.config), self.listen,
                                           self.anchor, self.legacy)
                    causes = {"config": "startup_config_changed", "path_config": "startup_config_changed",
                              "runtime_paths": "runtime_drift", "identity": "preflight_changed",
                              "lock": "locks_unverified", "ledger": "ledger_changed",
                              "ledger_snapshot": "ledger_unverified", "record": "records_changed"}
                    self.assertEqual(caught.exception.code, "record_cleanup_failed")
                    self.assertEqual(caught.exception.cause_code, causes[change])
                    self.assertEqual(server._windows_startup_recovery_detail(caught.exception),
                                     f": cause={causes[change]} records=unverified rollback=not_attempted")
                else:
                    server._run_server(PureWindowsPath(self.config), self.listen,
                                       self.anchor, self.legacy)
            finally:
                self.legacy._parent = original_parent
                self.fs.on_read = None
            gate.assert_called_once_with()  # The only bypass is this test's patch.
            rollback.assert_not_called()
            delete.assert_not_called()
        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].closed)
        self.assertEqual(instances[0].served, change is None)
        self.assertEqual(instances[0].stopped, change is None)
        self.assertIn(self.STATE + r"\active.json", self.fs.objects)
        self.assertEqual(injected["records"],
                         {name: self.fs.objects[self.STATE + "\\" + name]["data"]
                          for name in ("install.json", "active.json", "last_good_paths.json")})
        if present_ledger:
            self.assertIsNotNone(injected["ledger"])
            self.assertEqual(self.fs.objects[self.paths["totals"]]["data"], injected["ledger"])
        self.assertEqual([item[0] for item in self.fs.commit_calls],
                         ["new", "new", "new", "replace", "replace"])
        if change != "record":
            self.assertEqual(verify.call_args_list[-1].args[6]["active"]["state"], "READY")

    def test_ready_publication_rechecks_full_guard_before_fake_serve(self):
        self._exercise_published_ready()

    def test_ready_publication_rejects_changed_config_without_serving_or_rollback(self):
        self._exercise_published_ready("config")

    def test_ready_publication_rejects_other_consistent_record_set_without_serving(self):
        self._exercise_published_ready("record")

    def test_ready_rejects_changed_runtime_paths_file(self):
        self._exercise_published_ready("path_config")

    def test_ready_rejects_changed_resolved_runtime_path(self):
        self._exercise_published_ready("runtime_paths")

    def test_ready_rejects_changed_role_identity(self):
        self._exercise_published_ready("identity")

    def test_ready_rejects_unavailable_held_lock_pair(self):
        self._exercise_published_ready("lock")

    def test_ready_rejects_changed_ledger_without_reset(self):
        self._exercise_published_ready("ledger")

    def test_ready_rejects_unstable_ledger_snapshot_without_reset(self):
        self._exercise_published_ready("ledger_snapshot")

    def test_recovery_diagnostic_is_fixed_and_does_not_leak_original_details(self):
        for original, expected in (
            (discovery.PreflightError("ledger_changed", r"C:\secret\config TOKEN-SECRET"), "ledger_changed"),
            (discovery.PreflightError(r"C:\secret\TOKEN-SECRET", "private"), "unknown"),
            (RuntimeError(r"C:\secret\TOKEN-SECRET"), "unknown"),
        ):
            with self.subTest(expected=expected):
                error = server.WindowsStartupRecoveryError(original)
                stderr = io.StringIO()
                with patch.object(server, "resolve_listen", side_effect=error), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        server.main()
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(stderr.getvalue(),
                                 "model_proxy: startup failed (record_cleanup_failed)"
                                 f": cause={expected} records=unverified rollback=not_attempted\n")
                self.assertNotIn("secret", stderr.getvalue().lower())
                self.assertNotIn("TOKEN", stderr.getvalue())
        error.cause_code = r"C:\secret\TOKEN-SECRET"
        self.assertEqual(server._windows_startup_recovery_detail(error),
                         ": cause=unknown records=unverified rollback=not_attempted")

    def test_bound_setup_constructs_one_thinking_strip_store(self):
        proxy = Mock()
        record = {"runtime_paths": self.paths, "listen": self.listen, "locks": {}}
        ready, attempted = {"serving_started": False}, {"active.json": []}
        fake_totals = Mock()
        fake_totals.from_verified_snapshot.return_value = object()
        version = Mock()
        version.read_text.return_value = "fake"
        original = server.ThinkingStripStore
        with patch.object(server, "_VERSION_FILE", version), \
             patch.object(server, "_VERSION", "unknown"), \
             patch.object(server, "usage_totals", None), \
             patch.object(server, "_verify_migration_guard"), \
             patch.object(server, "init_logging"), \
             patch.object(server, "UsageTotalsStore", fake_totals), \
             patch.object(server, "SessionOverridesSidecar"), \
             patch.object(server, "ThinkingStripStore", wraps=original) as factory, \
             patch.object(server, "write_instance_record"), \
             patch.object(server, "read_instance_set", return_value={"active": dict(record, state="READY")}):
            server._run_bound_server(proxy, PureWindowsPath(self.config), self.listen, object(),
                                     PureWindowsPath(self.STATE), record, ready, attempted,
                                     self.anchor, self.legacy, (None, None, None, None), {})
        factory.assert_called_once_with()
        self.assertIsInstance(proxy.thinking_strip_store, original)
        self.assertTrue(ready["serving_started"])
        proxy.serve_forever.assert_called_once_with()


