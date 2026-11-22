"""Exercise Windows core orchestration with fake NTFS, fake listener and no network."""
from __future__ import annotations

import os
import threading
import unittest
from pathlib import PureWindowsPath
from unittest.mock import patch

import core.discovery as discovery
import core.platform_lock as locks
import core.platform_windows as win
import core.server as server
import core.windows_acl as acl
from core.runtime_paths import PATH_KEYS
from core.windows_paths import WindowsPathBackend
from tests.test_windows_adapter import VirtualACL, VirtualCRT
from tests.test_windows_discovery import PathWinBridge
from tests.test_windows_record_commit import RecordFS


class FakeConfigStore:
    """Expose ConfigStore's locked snapshot contract using synthetic Win32 I/O."""
    def __init__(self, config_path, on_reload=None):
        if on_reload is not None:
            raise AssertionError("unexpected reload callback")
        snapshot = server.read_stable_config(config_path)
        self._lock = threading.Lock()
        self._probe_marker = snapshot.marker
        self._config = snapshot.config


class FakeServer:
    def __init__(self, address, handler):
        self.server_address = address
        self.socket = object()
        self.closed = False
    def server_close(self): self.closed = True


class WindowsCoreMockStartupTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    STATE = r"C:\fixture\project\.runtime"
    RUNTIME = r"C:\fixture\runtime"

    def setUp(self):
        self.fs = RecordFS()
        self.crt = VirtualCRT(self.fs)
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.paths = {key: self.RUNTIME + "\\" + key for key in PATH_KEYS}
        self.config = self.ROOT + r"\config.json"
        self.path_config = self.ROOT + r"\runtime_paths.json"
        self.fs.objects[self.config] = dict(self.fs.obj(710), data=b"{}")
        self.fs.objects[self.path_config] = dict(self.fs.obj(711), data=b"{}")
        self.install = {"schema": discovery.INSTANCE_SCHEMA, "record": "install", "install_id": "a" * 32,
                        "project_root": self.ROOT, "root_identity": {"device": 17, "inode": 11},
                        "ever_started": False, "last_instance_nonce": None,
                        "initialization_source": "fake fixture"}
        self.listen = {"host": "127.0.0.1", "port": 19431,
                       "host_check_enabled": False, "allow_hosts": []}
        self.counter = iter((f"{n:032x}" for n in range(100)))
        fs, totals = self.fs, self.paths["totals"]

        class FakePath(PureWindowsPath):
            def lstat(self):
                # The fixture has no ledger: prove that only in fake NTFS.
                if str(self) != totals or str(self) in fs.objects:
                    raise AssertionError("unexpected real or present path stat")
                raise FileNotFoundError(str(self))

        patches = (
            patch.object(os, "name", "nt"),
            patch.dict(os.environ, {"MODEL_PROXY_START_NONCE": "b" * 32, "HOME": self.ROOT + r"\home"}, clear=True),
            patch.object(locks, "_windows_release_unknown", False),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(win, "_NativeFDAPI", return_value=self.crt),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
            patch("secrets.token_hex", side_effect=lambda n: next(self.counter)),
            patch.object(server, "Path", FakePath),
            patch.object(server, "_PACKAGE_DIR", PureWindowsPath(self.ROOT)),
            patch.object(server, "_RUNTIME_PATHS_FILE", PureWindowsPath(self.path_config)),
            patch.object(server, "_runtime_paths", self.paths),
            patch.object(server, "resolve_runtime_paths", return_value=self.paths),
            patch.object(server, "resolve_listen", return_value=self.listen),
            patch.object(server, "ConfigStore", FakeConfigStore),
            patch.object(server, "ProxyHTTPServer", FakeServer),
            patch.object(server, "process_start_identity", return_value={"kind": "windows_filetime", "ticks_100ns": 12345}),
            patch.object(server, "process_executable", return_value=r"C:\fixture\python.exe"),
            patch.object(server.os, "getpid", return_value=4321),
            patch.object(server.os, "kill", side_effect=AssertionError("signals forbidden")),
            patch.object(server.socket, "socket", side_effect=AssertionError("real sockets forbidden")),
            patch.object(server, "init_logging", side_effect=AssertionError("real log forbidden")),
            patch.object(server, "UsageTotalsStore", side_effect=AssertionError("real ledger forbidden")),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        self.anchor = locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.legacy = locks.acquire_lock(self.anchor.snapshot, "lock", create=True, anchor=self.anchor)
        self.addCleanup(self.close_locks)
        discovery.write_instance_record(self.STATE, "install.json", self.install,
                                        anchor=self.anchor, legacy=self.legacy)

    def close_locks(self):
        if not self.legacy.closed: self.legacy.close()
        if not self.anchor.closed: self.anchor.close()

    def test_startup_replace_error_keeps_observed_recovery_and_never_rolls_back(self):
        self.fs.fail_code = 1175
        self.fs.fail_only_replace = True
        # This test exercises record recovery after the listener gate; it does
        # not assert that real Windows listener exclusivity is verified.
        with patch.object(server, "_require_windows_listener_validation") as listener_gate, \
             patch.object(server, "_restore_prestart_records", side_effect=AssertionError("rollback forbidden")) as rollback, \
             patch.object(server, "_verify_migration_guard", wraps=server._verify_migration_guard) as verify, \
             patch.object(server, "_run_bound_server", side_effect=AssertionError("serve forbidden")):
            with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
                server._run_server(PureWindowsPath(self.config), self.listen, self.anchor, self.legacy)
        listener_gate.assert_called_once_with()
        self.assertEqual(caught.exception.winerror, 1175)
        self.assertEqual(caught.exception.recovery.target, "original")
        self.assertEqual(caught.exception.recovery.source, "replacement")
        self.assertTrue(locks._windows_release_unknown)
        self.assertEqual([x[0] for x in self.fs.commit_calls], ["new", "new", "new", "replace"])
        self.assertIn(self.STATE + r"\last_good_paths.json", self.fs.objects)
        self.assertIn(self.STATE + r"\active.json", self.fs.objects)
        rollback.assert_not_called()
        self.assertEqual(verify.call_count, 3)
        self.assertTrue(all(call.args[6]["active"] is None for call in verify.call_args_list))

    def test_preinitialized_core_reaches_ready_with_mocked_listener_and_no_real_io(self):
        seen = []
        def fake_bound(proxy, config_path, listen, store, state, record, ready, attempted,
                       anchor, legacy, migration_guard=None, expected_records=None, previous_records=None):
            current = discovery.read_instance_set(state)
            self.assertEqual(current["active"]["state"], "STARTING")
            ready_record = dict(record, state="READY")
            attempted["active.json"].append(ready_record)
            discovery.write_instance_record(state, "active.json", ready_record, anchor=anchor, legacy=legacy)
            result = discovery.read_instance_set(state)
            self.assertEqual(result["active"]["state"], "READY")
            ready["serving_started"] = True
            seen.append(result)
        # Simulate gate success only to verify the downstream record protocol.
        with patch.object(server, "_require_windows_listener_validation") as listener_gate, \
             patch.object(server, "_verify_migration_guard", wraps=server._verify_migration_guard) as verify, \
             patch.object(server, "_run_bound_server", side_effect=fake_bound):
            server._run_server(PureWindowsPath(self.config), self.listen, self.anchor, self.legacy)
        listener_gate.assert_called_once_with()
        self.assertEqual(verify.call_count, 4)
        guard = verify.call_args_list[0].args[0]
        self.assertEqual(guard[0], server.ConfigSnapshot({}, server.FileMarker(17, 710, 1000, 2000, 2)))
        self.assertEqual(guard[1], server.ConfigSnapshot({}, server.FileMarker(17, 711, 1000, 2000, 2)))
        self.assertIsNone(guard[3])  # The virtual ledger is absent.
        self.assertTrue(all(call.args[0] == guard for call in verify.call_args_list))
        self.assertTrue(all(call.args[6]["active"] is None for call in verify.call_args_list[:3]))
        self.assertEqual(verify.call_args_list[3].args[6]["active"]["state"], "STARTING")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["active"]["process_start"]["kind"], "windows_filetime")
        self.assertEqual([entry[0] for entry in self.fs.commit_calls],
                         ["new", "new", "new", "replace", "replace"])
        discovery.verify_held_instance_locks(self.anchor, self.legacy)

    def test_config_drift_after_fake_bind_is_rejected_before_record_writes(self):
        def bind_and_change_config(address, handler):
            config = self.fs.objects[self.config]
            config["data"] = b'{"generation":"changed"}'
            config["change"] = config.get("change", 2000) + 100
            return FakeServer(address, handler)

        # Config drift after a fake bind is a downstream behavior.
        with patch.object(server, "_require_windows_listener_validation") as listener_gate, \
             patch.object(server, "ProxyHTTPServer", side_effect=bind_and_change_config), \
             patch.object(server, "_verify_migration_guard", wraps=server._verify_migration_guard) as verify, \
             patch.object(server, "_run_bound_server", side_effect=AssertionError("serve forbidden")) as bound:
            with self.assertRaises(discovery.PreflightError) as caught:
                server._run_server(PureWindowsPath(self.config), self.listen, self.anchor, self.legacy)
        listener_gate.assert_called_once_with()
        self.assertEqual(caught.exception.code, "startup_config_changed")
        self.assertEqual(verify.call_count, 2)
        self.assertEqual([entry[0] for entry in self.fs.commit_calls], ["new"])
        bound.assert_not_called()
        discovery.verify_held_instance_locks(self.anchor, self.legacy)


class FakeBoundSocket:
    family = 2  # AF_INET
    def getsockopt(self, level, kind):
        import socket
        return socket.SOCK_STREAM
    def getsockname(self): return ("127.0.0.1", 19431)
    def listen(self, backlog): self.backlog = backlog


class WindowsFirstInitializationTests(unittest.TestCase):
    ROOT = r"C:\fixture\project"
    STATE = r"C:\fixture\project\.runtime"
    RUNTIME = r"C:\fixture\runtime"
    def setUp(self):
        self.fs = RecordFS()
        self.crt = VirtualCRT(self.fs)
        self.acl = VirtualACL(self.fs)
        self.backend = WindowsPathBackend(win32=PathWinBridge(self.fs), acl_backend=self.acl)
        self.paths = {key: self.RUNTIME + "\\" + key for key in PATH_KEYS}
        self.counter = iter((f"{n:032x}" for n in range(100)))
        patches = (
            patch.object(os, "name", "nt"),
            patch.object(locks, "_windows_release_unknown", False),
            patch.object(discovery, "_windows_path_backend", return_value=self.backend),
            patch.object(win, "_api", side_effect=lambda backend=None: backend if backend is not None else self.fs),
            patch.object(win, "_NativeFDAPI", return_value=self.crt),
            patch.object(acl, "_NativeBackend", return_value=self.acl),
            patch("secrets.token_hex", side_effect=lambda n: next(self.counter)),
            patch.object(discovery, "Path", PureWindowsPath),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        snapshot = discovery.inspect_runtime_identities(self.ROOT, self.paths)
        self.anchor = locks.acquire_lock(snapshot, "anchor.lock", create=True)
        self.legacy = locks.acquire_lock(self.anchor.snapshot, "lock", create=True, anchor=self.anchor)
        self.addCleanup(self.close_locks)
    def close_locks(self):
        if not self.legacy.closed: self.legacy.close()
        if not self.anchor.closed: self.anchor.close()

    def test_confirmed_initialization_creates_and_verifies_record_with_fake_socket(self):
        import socket
        listener = FakeBoundSocket()
        address = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 19431))]
        with patch.object(socket, "socket", FakeBoundSocket), \
             patch.object(socket, "getaddrinfo", return_value=address), \
             patch.object(os, "kill", side_effect=AssertionError("signal forbidden")):
            record = discovery.initialize_installation(self.anchor, self.legacy, listener,
                      listen={"host": "127.0.0.1", "port": 19431}, source="fake fixture", confirmed=True)
        self.assertEqual(record["record"], "install")
        self.assertEqual(discovery.read_instance_set(self.STATE)["install"], record)
        self.assertEqual([x[0] for x in self.fs.commit_calls], ["new"])

    def test_postwrite_initialization_error_never_deletes_record(self):
        import socket
        listener = FakeBoundSocket()
        address = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 19431))]
        with patch.object(socket, "socket", FakeBoundSocket), \
             patch.object(socket, "getaddrinfo", return_value=address), \
             patch.object(discovery, "read_instance_set", side_effect=[None, discovery.PreflightError("fake", "fake")]), \
             patch.object(discovery, "delete_instance_record_if_matches", side_effect=AssertionError("delete forbidden")) as cleanup:
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery.initialize_installation(self.anchor, self.legacy, listener,
                         listen={"host": "127.0.0.1", "port": 19431}, source="fake fixture", confirmed=True)
        self.assertEqual(caught.exception.code, "initialization_cleanup_failed")
        self.assertIn(self.STATE + r"\install.json", self.fs.objects)
        cleanup.assert_not_called()

    def test_native_commit_failure_preserves_recovery_without_cleanup(self):
        import socket
        listener = FakeBoundSocket()
        address = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 19431))]
        self.fs.fail_code = 1176
        with patch.object(socket, "socket", FakeBoundSocket), \
             patch.object(socket, "getaddrinfo", return_value=address), \
             patch.object(discovery, "delete_instance_record_if_matches", side_effect=AssertionError("cleanup forbidden")) as cleanup:
            with self.assertRaises(discovery.WindowsRecordCommitError) as caught:
                discovery.initialize_installation(self.anchor, self.legacy, listener,
                         listen={"host": "127.0.0.1", "port": 19431}, source="fake fixture", confirmed=True)
        self.assertEqual(caught.exception.winerror, 1176)
        self.assertEqual(caught.exception.recovery.target, "missing")
        self.assertEqual(caught.exception.recovery.source, "replacement")
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
