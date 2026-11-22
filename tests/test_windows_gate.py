"""Startup ordering with a temporary project and one isolated loopback listener."""
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import core.server as server
from core.discovery import inspect_runtime_identities
from core.platform_lock import LockError, acquire_lock, verify_local_access
from core.runtime_paths import PATH_KEYS


class StartupIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS 的 /var → /private/var 是符号链接；verify_record_directory 要求
        # 固定目录为规范路径（生产不变量），临时根必须先 resolve。
        self.root = Path(self.tmp.name).resolve() / "project"
        self.root.mkdir(mode=0o700)
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        self.paths = {key: runtime / key for key in PATH_KEYS}
        self.path_file = self.root / "config" / "runtime_paths.json"
        self.path_file.parent.mkdir()
        self.path_file.write_text(json.dumps({k: str(v) for k, v in self.paths.items()}))
        self.config_file = self.path_file.parent / "fake-config.json"
        self.config_file.write_text(json.dumps({"supplies": [], "routes": [], "strategies": []}))
        config_file = patch.object(server, "_DEFAULT_CONFIG_PATH", self.config_file)
        config_file.start()
        self.addCleanup(config_file.stop)
        paths_file = patch.object(server, "_RUNTIME_PATHS_FILE", self.path_file)
        paths_file.start()
        self.addCleanup(paths_file.stop)
        self.mock_root = patch.object(server, "_PACKAGE_DIR", self.root)
        self.mock_root.start()
        self.addCleanup(self.mock_root.stop)
        self.resolve = patch.object(server, "resolve_runtime_paths",
                                    side_effect=lambda: self.original_resolve(self.path_file))
        self.original_resolve = server.resolve_runtime_paths
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        listen = patch.object(server, "resolve_listen", return_value={"host": "unused", "port": 0, "host_check_enabled": False, "allow_hosts": []})
        listen.start()
        self.addCleanup(listen.stop)

    def test_invalid_path_file_stops_before_logging_or_ledger(self):
        self.path_file.write_text("{")
        with patch.object(server, "init_logging") as log, \
             patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        log.assert_not_called()
        totals.assert_not_called()
        self.assertFalse((self.root / ".runtime").exists())

    def test_conflict_stops_before_state_logging_or_ledger(self):
        self.paths["totals"] = self.paths["log"]
        self.path_file.write_text(json.dumps({k: str(v) for k, v in self.paths.items()}))
        self.paths["log"].write_text("sentinel")
        with patch.object(server, "init_logging") as log, \
             patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertEqual(self.paths["log"].read_text(), "sentinel")
        self.assertFalse((self.root / ".runtime").exists())
        log.assert_not_called()
        totals.assert_not_called()

    def _assert_existing_legacy_artifact_stops_before_writes(self, role, *, directory=False):
        target = self.paths[role]
        if directory:
            target.mkdir()
            (target / "marker").write_text("legacy sentinel")
        else:
            target.write_text("legacy sentinel")
        stderr = io.StringIO()
        with patch.object(Path, "mkdir") as mkdir, \
             patch.object(server, "ProxyHTTPServer") as bind, \
             patch.object(server, "_confirm_initialization") as confirm, \
             patch.object(server, "init_logging") as logging, \
             patch.object(server, "UsageTotalsStore") as totals, \
             patch.object(server, "write_instance_record") as record, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("legacy_state_unknown", stderr.getvalue())
        mkdir.assert_not_called()
        bind.assert_not_called()
        confirm.assert_not_called()
        logging.assert_not_called()
        totals.assert_not_called()
        record.assert_not_called()
        self.assertFalse((self.root / ".runtime").exists())
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())
        self.assertEqual((target / "marker" if directory else target).read_text(), "legacy sentinel")

    def test_existing_old_pid_stops_direct_entry_before_any_write(self):
        self._assert_existing_legacy_artifact_stops_before_writes("pid")

    def test_existing_mkdir_start_lock_stops_direct_entry_before_any_write(self):
        self._assert_existing_legacy_artifact_stops_before_writes("start_lock", directory=True)

    def test_existing_file_start_lock_stops_direct_entry_before_any_write(self):
        self._assert_existing_legacy_artifact_stops_before_writes("start_lock")

    def test_unstarted_existing_install_and_old_pid_fail_before_any_new_write(self):
        from core.discovery import initialize_installation, read_instance_set
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True, access_verifier=verify_local_access, anchor=anchor) as legacy:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("127.0.0.1", 0))
                    sock.listen()
                    initialize_installation(anchor, legacy, sock,
                        listen={"host": "127.0.0.1", "port": sock.getsockname()[1]},
                        source="fake existing install", confirmed=True)
        self.assertFalse(read_instance_set(state)["install"]["ever_started"])
        self.paths["pid"].write_text("legacy sentinel")
        before = {item.name: item.read_bytes() for item in state.iterdir()}
        stderr = io.StringIO()
        with patch.object(Path, "mkdir") as mkdir, \
             patch.object(server, "ProxyHTTPServer") as bind, \
             patch.object(server, "init_logging") as logging, \
             patch.object(server, "write_instance_record") as record, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("legacy_state_unknown", stderr.getvalue())
        mkdir.assert_not_called()
        bind.assert_not_called()
        logging.assert_not_called()
        record.assert_not_called()
        self.assertEqual(before, {item.name: item.read_bytes() for item in state.iterdir()})
        self.assertEqual(self.paths["pid"].read_text(), "legacy sentinel")
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())

    def test_unsafe_legacy_runtime_file_fails_before_any_new_write(self):
        ledger = self.paths["totals"]
        ledger.write_text("legacy ledger sentinel")
        ledger.chmod(0o666)
        stderr = io.StringIO()
        with patch.object(Path, "mkdir") as mkdir, \
             patch.object(server, "init_logging") as logging, \
             patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("legacy_state_unknown", stderr.getvalue())
        mkdir.assert_not_called()
        logging.assert_not_called()
        bind.assert_not_called()
        self.assertEqual(ledger.read_text(), "legacy ledger sentinel")
        self.assertFalse((self.root / ".runtime").exists())

    def test_hardlinked_legacy_runtime_file_fails_before_any_new_write(self):
        ledger = self.paths["totals"]
        ledger.write_text("legacy ledger sentinel")
        alias = self.root / "runtime" / "other-ledger-name"
        os.link(ledger, alias)
        stderr = io.StringIO()
        with patch.object(Path, "mkdir") as mkdir, \
             patch.object(server, "init_logging") as logging, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("legacy_state_unknown", stderr.getvalue())
        mkdir.assert_not_called()
        logging.assert_not_called()
        self.assertEqual(ledger.read_text(), "legacy ledger sentinel")
        self.assertEqual(alias.read_text(), "legacy ledger sentinel")
        self.assertFalse((self.root / ".runtime").exists())

    def test_unknown_legacy_identity_stops_before_any_write(self):
        original_lstat = os.lstat
        def deny_pid(path, *args, **kwargs):
            if os.fspath(path) == str(self.paths["pid"]):
                raise PermissionError("fake denied")
            return original_lstat(path, *args, **kwargs)
        with patch("os.lstat", side_effect=deny_pid), \
             patch.object(Path, "mkdir") as mkdir, \
             patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        mkdir.assert_not_called()
        bind.assert_not_called()
        self.assertFalse((self.root / ".runtime").exists())

    def test_legacy_pid_created_after_readonly_preflight_is_rechecked_under_locks(self):
        real_verify = server.verify_record_directory
        real_check = server._check_legacy_observations
        injected = False
        def insert_after_state_creation(state):
            nonlocal injected
            if not injected:
                injected = True
                self.paths["pid"].write_text("legacy appeared")
            return real_verify(state)
        stderr = io.StringIO()
        with patch.object(server, "verify_record_directory", side_effect=insert_after_state_creation), \
             patch.object(server, "_check_legacy_observations", wraps=real_check) as checked, \
             patch.object(server, "_run_server") as run, \
             patch.object(server, "init_logging") as logging, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertTrue(injected)
        self.assertGreaterEqual(checked.call_count, 2)
        locked_snapshot = checked.call_args_list[-1].args[0].as_dict()
        self.assertTrue(locked_snapshot["anchor.lock"].exists)
        self.assertTrue(locked_snapshot["lock"].exists)
        self.assertIn("legacy_state_unknown", stderr.getvalue())
        self.assertEqual(self.paths["pid"].read_text(), "legacy appeared")
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())
        run.assert_not_called()
        logging.assert_not_called()

    def test_normal_preflight_holds_anchor_and_legacy_before_run(self):
        def check_locks(config_path, listen_cfg, anchor, legacy, *, initial_private=None):
            self.assertEqual(Path(config_path), self.config_file)
            self.assertEqual(listen_cfg, {"host": "unused", "port": 0,
                                          "host_check_enabled": False, "allow_hosts": []})
            self.assertEqual((anchor.role, legacy.role), ("anchor.lock", "lock"))
            self.assertIs(legacy._parent, anchor)
            self.assertFalse(anchor.closed)
            self.assertFalse(legacy.closed)
            if sys.platform == "darwin" and os.name == "posix":
                self.assertIsNotNone(initial_private)
                server._verify_mac_private_config(initial_private)
            server.verify_held_instance_locks(anchor, legacy)
            snapshot = inspect_runtime_identities(self.root, self.paths)
            with self.assertRaises(LockError) as caught:
                acquire_lock(snapshot, "anchor.lock", access_verifier=verify_local_access)
            self.assertEqual(caught.exception.code, "lock_conflict")
            self.assertFalse(self.paths["log"].exists())
            self.assertFalse(self.paths["totals"].exists())
        with patch.object(server, "_run_server", side_effect=check_locks) as run:
            server.main()
        run.assert_called_once()
        with acquire_lock(inspect_runtime_identities(self.root, self.paths), "anchor.lock",
                          access_verifier=verify_local_access):
            pass

    def test_sidecar_runtime_role_collision_is_rejected_before_writes(self):
        sidecar = self.config_file.parent / "session_overrides.json"
        sidecar.write_text("sidecar sentinel")
        original_inode = sidecar.stat().st_ino
        for role in ("log", "totals"):
            for mode in ("same_path", "hardlink"):
                with self.subTest(role=role, mode=mode):
                    alias = self.root / "runtime" / f"{role}-sidecar-hardlink"
                    if mode == "hardlink":
                        os.link(sidecar, alias)
                        self.paths[role] = alias
                    else:
                        self.paths[role] = sidecar
                    self.path_file.write_text(json.dumps({k: str(v) for k, v in self.paths.items()}))
                    with patch.object(server, "init_logging") as log, \
                         patch.object(server, "UsageTotalsStore") as totals, \
                         patch.object(server, "SessionOverridesSidecar") as sidecar_store, redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            server.main()
                    self.assertEqual(sidecar.read_text(), "sidecar sentinel")
                    self.assertEqual(sidecar.stat().st_ino, original_inode)
                    self.assertFalse((self.root / ".runtime").exists())
                    log.assert_not_called()
                    totals.assert_not_called()
                    sidecar_store.assert_not_called()
                    if mode == "hardlink":
                        alias.unlink()
                    self.paths[role] = self.root / "runtime" / role

    def test_absent_sidecar_path_collision_is_rejected(self):
        sidecar = self.config_file.parent / "session_overrides.json"
        self.paths["totals"] = sidecar
        self.path_file.write_text(json.dumps({k: str(v) for k, v in self.paths.items()}))
        with patch.object(server, "init_logging") as log, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertFalse(sidecar.exists())
        self.assertFalse((self.root / ".runtime").exists())
        log.assert_not_called()

    def test_log_and_totals_final_symlinks_rejected_before_writes(self):
        for role in ("log", "totals"):
            with self.subTest(role=role):
                alias = self.paths[role]
                target = self.root / "runtime" / f"{role}-target"
                target.write_text(f"{role} sentinel")
                alias.symlink_to(target)
                self.path_file.write_text(json.dumps({k: str(v) for k, v in self.paths.items()}))
                with patch.object(server, "init_logging") as log, \
                     patch.object(server, "UsageTotalsStore") as totals, \
                     patch.object(server, "_run_server") as run, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        server.main()
                self.assertTrue(alias.is_symlink())
                self.assertEqual(target.read_text(), f"{role} sentinel")
                self.assertFalse((self.root / ".runtime").exists())
                log.assert_not_called()
                totals.assert_not_called()
                run.assert_not_called()
                alias.unlink()
                target.unlink()

    def test_config_alias_rejected_before_state_or_logging(self):
        self.paths["log"] = self.path_file
        original = json.dumps({k: str(v) for k, v in self.paths.items()})
        self.path_file.write_text(original)
        with patch.object(server, "init_logging") as log, \
             patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertEqual(self.path_file.read_text(), original)
        self.assertFalse((self.root / ".runtime").exists())
        log.assert_not_called()
        totals.assert_not_called()

    def test_config_inside_fixed_state_is_rejected_before_state_creation(self):
        fake_config = self.root / ".runtime" / "install.json"
        with patch.object(server, "_DEFAULT_CONFIG_PATH", fake_config), \
             patch.object(server, "init_logging") as log, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertFalse((self.root / ".runtime").exists())
        log.assert_not_called()

    def test_bind_failure_does_not_initialize_logging_or_ledger(self):
        with patch.object(server, "ProxyHTTPServer", side_effect=OSError("fixture bind error")), \
             patch.object(server, "init_logging") as log, \
             patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        log.assert_not_called()
        totals.assert_not_called()

    def test_changed_runtime_path_file_fails_under_anchor(self):
        changed = dict(self.paths)
        changed["lock"] = self.root / "different-lock"
        with patch.object(server, "resolve_runtime_paths", side_effect=[self.paths, changed]), \
             patch.object(server, "_run_server") as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        run.assert_not_called()
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())

    def test_first_install_confirmed_starting_then_ready(self):
        from unittest.mock import MagicMock
        from core.discovery import read_instance_set
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            listen = {"host": "127.0.0.1", "port": port,
                      "host_check_enabled": False, "allow_hosts": []}
            self_root = self.root
            class FakeServer:
                socket = sock
                server_address = ("127.0.0.1", port)
                def __init__(self, *args): pass
                def serve_forever(self):
                    records = read_instance_set(self_root / ".runtime")
                    assert records["active"]["state"] == "READY"
                def shutdown(self): pass
                def server_close(self): pass
            nonce = "a" * 32
            def check_starting(path):
                records = read_instance_set(self.root / ".runtime")
                active, install = records["active"], records["install"]
                self.assertEqual(active["state"], "STARTING")
                self.assertEqual(active["instance_nonce"], nonce)
                self.assertEqual(install["last_instance_nonce"], nonce)
                self.assertEqual(active["listen"], {"host": "127.0.0.1", "port": port})
                self.assertEqual(active["runtime_paths"], {k: str(v) for k, v in self.paths.items()})
                self.assertEqual(active["install_id"], install["install_id"])
                self.assertGreater(active["locks"]["anchor"]["inode"], 0)
            with patch.dict(server.os.environ, {"MODEL_PROXY_START_NONCE": nonce}), \
                 patch.object(server, "resolve_listen", return_value=listen), \
                 patch.object(server, "ProxyHTTPServer", FakeServer), \
                 patch.object(server, "_confirm_initialization", return_value="fake isolated installation"), \
                 patch.object(server, "init_logging", side_effect=check_starting), \
                 patch.object(server, "UsageTotalsStore"), \
                 patch.object(server, "SessionOverridesSidecar"), \
                 patch.object(server, "log", MagicMock()):
                server.main()
        records = read_instance_set(self.root / ".runtime")
        self.assertEqual(records["active"]["state"], "READY")
        self.assertEqual(records["last_good_paths"]["instance_nonce"], nonce)
        self.assertTrue(records["install"]["ever_started"])

    def test_failed_start_after_record_commit_restores_unstarted_installation(self):
        from core.discovery import read_instance_set
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            listen = {"host": "127.0.0.1", "port": port,
                      "host_check_enabled": False, "allow_hosts": []}
            class FakeServer:
                socket = sock
                server_address = ("127.0.0.1", port)
                def __init__(self, *args): pass
                def server_close(self): pass
            stderr = io.StringIO()
            with patch.object(server, "resolve_listen", return_value=listen), \
                 patch.object(server, "ProxyHTTPServer", FakeServer), \
                 patch.object(server, "_confirm_initialization", return_value="fake fixture"), \
                 patch.object(server, "_run_bound_server", side_effect=RuntimeError("fake failure")) as failpoint, \
                 redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    server.main()
        failpoint.assert_called_once()
        records = read_instance_set(self.root / ".runtime")
        self.assertIsNotNone(records, stderr.getvalue())
        self.assertFalse(records["install"]["ever_started"])
        self.assertIsNone(records["active"])
        self.assertIsNone(records["last_good_paths"])
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())

    def test_failure_after_active_write_rolls_back_our_generation(self):
        from core.discovery import read_instance_set
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            listen = {"host": "127.0.0.1", "port": port,
                      "host_check_enabled": False, "allow_hosts": []}
            class FakeServer:
                socket = sock
                server_address = ("127.0.0.1", port)
                def __init__(self, *args): pass
                def server_close(self): pass
            real_write = server.write_instance_record
            def fail_after_write(state, name, record, **kwargs):
                real_write(state, name, record, **kwargs)
                if name == "active.json" and record["state"] == "STARTING":
                    raise OSError("fake post-write failure")
            with patch.object(server, "resolve_listen", return_value=listen), \
                 patch.object(server, "ProxyHTTPServer", FakeServer), \
                 patch.object(server, "_confirm_initialization", return_value="fake fixture"), \
                 patch.object(server, "write_instance_record", side_effect=fail_after_write), \
                 patch.object(server, "init_logging") as log, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    server.main()
            log.assert_not_called()
        records = read_instance_set(self.root / ".runtime")
        self.assertFalse(records["install"]["ever_started"])
        self.assertIsNone(records["active"])
        self.assertIsNone(records["last_good_paths"])

    def test_ready_write_failure_before_serving_rolls_back_generation(self):
        from core.discovery import read_instance_set
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            listen = {"host": "127.0.0.1", "port": port,
                      "host_check_enabled": False, "allow_hosts": []}
            class FakeServer:
                socket = sock
                server_address = ("127.0.0.1", port)
                def __init__(self, *args): pass
                def serve_forever(self): raise AssertionError("must not serve")
                def server_close(self): pass
            real_write = server.write_instance_record
            def fail_ready_after_write(state, name, record, **kwargs):
                real_write(state, name, record, **kwargs)
                if name == "active.json" and record["state"] == "READY":
                    raise OSError("fake ready post-write failure")
            with patch.object(server, "resolve_listen", return_value=listen), \
                 patch.object(server, "ProxyHTTPServer", FakeServer), \
                 patch.object(server, "_confirm_initialization", return_value="fake fixture"), \
                 patch.object(server, "write_instance_record", side_effect=fail_ready_after_write), \
                 patch.object(server, "init_logging"), \
                 patch.object(server, "UsageTotalsStore"), \
                 patch.object(server, "SessionOverridesSidecar"), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    server.main()
        records = read_instance_set(self.root / ".runtime")
        self.assertFalse(records["install"]["ever_started"])
        self.assertIsNone(records["active"])
        self.assertIsNone(records["last_good_paths"])

    def test_cleanup_failure_is_reported_and_records_stay_unknown(self):
        from core.discovery import read_instance_set
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            listen = {"host": "127.0.0.1", "port": port,
                      "host_check_enabled": False, "allow_hosts": []}
            class FakeServer:
                socket = sock
                server_address = ("127.0.0.1", port)
                def __init__(self, *args): pass
                def server_close(self): pass
            real_write = server.write_instance_record
            def fail_after_write(state, name, record, **kwargs):
                real_write(state, name, record, **kwargs)
                if name == "active.json":
                    raise OSError("fake post-write failure")
            stderr = io.StringIO()
            with patch.object(server, "resolve_listen", return_value=listen), \
                 patch.object(server, "ProxyHTTPServer", FakeServer), \
                 patch.object(server, "_confirm_initialization", return_value="fake fixture"), \
                 patch.object(server, "write_instance_record", side_effect=fail_after_write), \
                 patch.object(server, "delete_instance_record_if_matches", side_effect=OSError("fake cleanup failure")), \
                 redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    server.main()
        self.assertIn("record_cleanup_failed", stderr.getvalue())
        with self.assertRaises(server.PreflightError):
            read_instance_set(self.root / ".runtime")

    def test_existing_legacy_lock_blocks_before_log_or_ledger(self):
        import fcntl
        lock = self.paths["lock"]
        lock.write_text("legacy sentinel")
        lock.chmod(0o600)
        with lock.open("r+") as old:
            fcntl.flock(old.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(server, "init_logging") as log, \
                 patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    server.main()
            log.assert_not_called()
            totals.assert_not_called()
        self.assertEqual(lock.read_text(), "legacy sentinel")

    def test_initialization_api_rejects_without_explicit_confirmation(self):
        from core.discovery import initialize_installation, read_instance_set
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True, access_verifier=verify_local_access, anchor=anchor) as legacy:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("127.0.0.1", 0))
                    sock.listen()
                    listen = {"host": "127.0.0.1", "port": sock.getsockname()[1]}
                    with self.assertRaises(server.PreflightError):
                        initialize_installation(anchor, legacy, sock, listen=listen,
                                                source="fake fixture", confirmed=False)
                    self.assertIsNone(read_instance_set(state))
                    record = initialize_installation(anchor, legacy, sock, listen=listen,
                                                     source="fake fixture", confirmed=True)
                    self.assertEqual(record["initialization_source"], "fake fixture")
                    with self.assertRaises(server.PreflightError):
                        initialize_installation(anchor, legacy, sock, listen=listen,
                                                source="fake fixture", confirmed=True)

    def _assert_confirmation_change_stops_before_writes(self, change, *, during_resolution=False,
                                                        expected_error="startup_config_changed"):
        log_path, totals_path = self.paths["log"], self.paths["totals"]
        log_path.write_text("log sentinel")
        totals_text = json.dumps({"version": 3, "total": dict(server._zero_bucket(), sentinel="keep"),
                                  "days": {}, "months_archive": {}})
        totals_path.write_text(totals_text)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            listen = {"host": "127.0.0.1", "port": port,
                      "host_check_enabled": False, "allow_hosts": []}
            class FakeServer:
                socket = sock
                server_address = ("127.0.0.1", port)
                def __init__(self, *args): pass
                def server_close(self): pass
            def confirm():
                if not during_resolution:
                    change()
                return "fake isolated installation"
            def resolve_host(host, requested_port, family, socktype, proto):
                change()
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                         "", ("127.0.0.1", requested_port))]
            stderr = io.StringIO()
            with patch.object(server, "resolve_listen", return_value=listen), \
                 patch.object(server, "ProxyHTTPServer", FakeServer), \
                 patch.object(server, "_confirm_initialization", side_effect=confirm) as confirm_mock, \
                 patch.object(server, "init_logging") as log, \
                 patch.object(server, "UsageTotalsStore") as totals, \
                 patch("socket.getaddrinfo", side_effect=resolve_host if during_resolution else AssertionError("unexpected DNS")), \
                 redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    server.main()
            self.assertEqual(confirm_mock.call_count, 1, stderr.getvalue())
            log.assert_not_called()
            totals.assert_not_called()
            self.assertIn(expected_error, stderr.getvalue())
        self.assertEqual(log_path.read_text(), "log sentinel")
        self.assertEqual(totals_path.read_text(), totals_text)
        state = self.root / ".runtime"
        for name in ("install.json", "active.json", "last_good_paths.json"):
            self.assertFalse((state / name).exists())

    def test_legacy_pid_created_during_confirmation_is_not_cleared(self):
        self._assert_confirmation_change_stops_before_writes(
            lambda: self.paths["pid"].write_text("legacy appeared"), expected_error="legacy_state_unknown")
        self.assertEqual(self.paths["pid"].read_text(), "legacy appeared")

    def test_legacy_start_lock_created_during_confirmation_is_not_cleared(self):
        def create_lock():
            self.paths["start_lock"].mkdir()
            (self.paths["start_lock"] / "marker").write_text("legacy appeared")
        self._assert_confirmation_change_stops_before_writes(create_lock, expected_error="legacy_state_unknown")
        self.assertEqual((self.paths["start_lock"] / "marker").read_text(), "legacy appeared")

    def test_legacy_pid_appearing_during_dns_fails_before_record_commit(self):
        self._assert_confirmation_change_stops_before_writes(
            lambda: self.paths["pid"].write_text("legacy appeared"), during_resolution=True,
            expected_error="preflight_changed")
        self.assertEqual(self.paths["pid"].read_text(), "legacy appeared")

    def test_business_edit_during_confirmation_is_rejected(self):
        def change():
            self.config_file.write_text(json.dumps({"supplies": [], "routes": [],
                                                    "strategies": [], "generation": "changed"}))
        self._assert_confirmation_change_stops_before_writes(change)

    def test_business_atomic_replacement_during_confirmation_is_rejected(self):
        def change():
            replacement = self.config_file.parent / "replacement.json"
            replacement.write_text(self.config_file.read_text())
            os.replace(replacement, self.config_file)
        self._assert_confirmation_change_stops_before_writes(change)

    def test_business_deletion_during_confirmation_is_rejected(self):
        self._assert_confirmation_change_stops_before_writes(self.config_file.unlink)

    def test_runtime_path_file_edit_with_same_values_is_rejected(self):
        def change():
            self.path_file.write_text("  " + self.path_file.read_text() + "\n")
        self._assert_confirmation_change_stops_before_writes(change)

    def test_runtime_path_file_deletion_during_confirmation_is_rejected(self):
        self._assert_confirmation_change_stops_before_writes(self.path_file.unlink)

    def test_runtime_path_file_corruption_during_confirmation_is_rejected(self):
        self._assert_confirmation_change_stops_before_writes(lambda: self.path_file.write_text("{"))

    def test_listen_config_edit_during_confirmation_is_rejected(self):
        def change():
            self.config_file.write_text(json.dumps({"supplies": [], "routes": [], "strategies": [],
                                                    "listen": {"host": "127.0.0.1", "port": 31123}}))
        self._assert_confirmation_change_stops_before_writes(change)

    def test_runtime_path_value_change_during_confirmation_is_rejected(self):
        def change():
            paths = {key: str(value) for key, value in self.paths.items()}
            paths["log"] = str(self.root / "runtime" / "other-log")
            self.path_file.write_text(json.dumps(paths))
        self._assert_confirmation_change_stops_before_writes(change)

    def test_listen_environment_change_during_confirmation_is_rejected(self):
        with patch.dict(os.environ, {"MODEL_PROXY_PORT": "20001"}):
            self._assert_confirmation_change_stops_before_writes(
                lambda: os.environ.__setitem__("MODEL_PROXY_PORT", "20002"))

    def test_config_change_during_hostname_resolution_is_rechecked_before_commit(self):
        def change():
            self.config_file.write_text(json.dumps({"supplies": [], "routes": [],
                                                    "strategies": [], "generation": "during-dns"}))
        self._assert_confirmation_change_stops_before_writes(change, during_resolution=True)

    def test_runtime_change_during_hostname_resolution_is_rechecked_before_commit(self):
        def change():
            self.path_file.write_text(" " + self.path_file.read_text())
        self._assert_confirmation_change_stops_before_writes(change, during_resolution=True)

    def _initialize_hostname_fixture(self, candidates, *, expected_success):
        from core.discovery import initialize_installation, read_instance_set
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True, access_verifier=verify_local_access, anchor=anchor) as legacy:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("127.0.0.1", 0))
                    sock.listen()
                    port = sock.getsockname()[1]
                    listen = {"host": "service.fixture.invalid", "port": port}
                    def lookup(host, requested_port, family, socktype, proto):
                        if isinstance(candidates, BaseException):
                            raise candidates
                        if candidates == "wrong_family":
                            return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                     "", ("::1", requested_port, 0, 0))]
                        if candidates == "wrong_port":
                            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                     "", ("127.0.0.1", requested_port + 1))]
                        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                 "", (address, requested_port)) for address in candidates]
                    with patch("socket.getaddrinfo", side_effect=lookup) as resolver:
                        if expected_success:
                            record = initialize_installation(anchor, legacy, sock, listen=listen,
                                                             source="fake fixture", confirmed=True)
                            self.assertEqual(record["record"], "install")
                        else:
                            with self.assertRaises(server.PreflightError) as caught:
                                initialize_installation(anchor, legacy, sock, listen=listen,
                                                        source="fake fixture", confirmed=True)
                            self.assertEqual(caught.exception.code, "listen_unverified")
                        resolver.assert_called_once_with("service.fixture.invalid", port, socket.AF_INET,
                                                         socket.SOCK_STREAM, socket.IPPROTO_TCP)
        records = read_instance_set(state)
        self.assertEqual(records is not None, expected_success)
        self.assertFalse(self.paths["log"].exists())
        self.assertFalse(self.paths["totals"].exists())

    def test_resolvable_hostname_matching_one_bound_candidate_initializes(self):
        self._initialize_hostname_fixture(["127.0.0.2", "127.0.0.1"], expected_success=True)

    def test_resolvable_hostname_not_matching_bound_address_rejects(self):
        self._initialize_hostname_fixture(["127.0.0.2"], expected_success=False)

    def test_hostname_candidate_wrong_family_rejects(self):
        self._initialize_hostname_fixture("wrong_family", expected_success=False)

    def test_hostname_candidate_wrong_port_rejects(self):
        self._initialize_hostname_fixture("wrong_port", expected_success=False)

    def test_hostname_resolution_failure_rejects_without_record(self):
        self._initialize_hostname_fixture(socket.gaierror("fake resolution error"), expected_success=False)

    def test_hostname_empty_candidates_rejects_without_record(self):
        self._initialize_hostname_fixture([], expected_success=False)

    def test_unconfirmed_initialization_does_not_write_records_or_logs(self):
        class FakeServer:
            server_address = ("127.0.0.1", 49123)
            def __init__(self, *args): pass
            def server_close(self): pass
        with patch.object(server, "ProxyHTTPServer", FakeServer), \
             patch.object(server, "_confirm_initialization", side_effect=server.PreflightError("initialization_required", "fake")), \
             patch.object(server, "init_logging") as log, \
             patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        state = self.root / ".runtime"
        self.assertFalse((state / "install.json").exists())
        self.assertFalse((state / "active.json").exists())
        log.assert_not_called()
        totals.assert_not_called()

    def _seed_stopped_instance(self, old_sock):
        from core.discovery import initialize_installation, write_instance_record, read_instance_set
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True, access_verifier=verify_local_access, anchor=anchor) as legacy:
                old_port = old_sock.getsockname()[1]
                self._old_port = old_port
                install = initialize_installation(anchor, legacy, old_sock,
                    listen={"host": "127.0.0.1", "port": old_port}, source="fake old installation", confirmed=True)
                ids = legacy.snapshot.as_dict()
                locks = {}
                for name, held in (("anchor", anchor), ("legacy", legacy)):
                    info = os.fstat(held.fileno())
                    locks[name] = {"device": info.st_dev, "inode": info.st_ino}
                common = {"schema": 2, "install_id": install["install_id"],
                          "project_root": str(self.root), "instance_nonce": "b" * 32,
                          "listen": {"host": "127.0.0.1", "port": old_port},
                          "runtime_paths": {key: ids[key].canonical for key in PATH_KEYS}}
                active = dict(common, record="active", state="READY", pid=99999999,
                              process_start={"kind": "darwin_proc_bsdinfo", "seconds": 1, "microseconds": 0},
                              executable="/fake/python", locks=locks)
                last = dict(common, record="last_good_paths")
                install = dict(install, ever_started=True, last_instance_nonce=active["instance_nonce"])
                for name, value in (("last_good_paths.json", last), ("active.json", active), ("install.json", install)):
                    write_instance_record(state, name, value, anchor=anchor, legacy=legacy)
        return read_instance_set(state)

    def _write_listen_config(self, port):
        self.config_file.write_text(json.dumps({"supplies": [], "routes": [], "strategies": [],
                                                "listen": {"host": "127.0.0.1", "port": port}}))

    def _ledger_fixture(self):
        ledger = self.paths["totals"]
        total = dict(server._zero_bucket(), sentinel="keep")
        ledger.write_text(json.dumps({"version": 3, "since": "2026-01-01", "keep_days": 400,
                                      "total": total, "days": {}, "months_archive": {}}))
        return ledger

    def _unused_fixture_port(self):
        for _ in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
                if port != getattr(self, "_old_port", None):
                    return port
        self.fail("could not obtain a distinct fixture port")

    def _record_bytes(self):
        state = self.root / ".runtime"
        return {name: (state / name).read_bytes() for name in ("install.json", "active.json", "last_good_paths.json")}

    def _assert_stopped_instance_can_change_listen(self, *, reused=False):
        from core.discovery import read_instance_set
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            old_records = self._seed_stopped_instance(old)
            old_port = old.getsockname()[1]
        new_port = self._unused_fixture_port()
        self.assertNotEqual(old_port, new_port)
        self._write_listen_config(new_port)
        before = ledger.read_bytes()
        state = self.root / ".runtime"
        observed = []
        def serve_once(http):
            records = read_instance_set(state)
            observed.append(records)
            self.assertEqual(records["active"]["state"], "READY")
            self.assertEqual(records["active"]["listen"], {"host": "127.0.0.1", "port": new_port})
        real_identity = server.process_start_identity
        def process_identity(pid):
            if pid == 99999999:
                return {"kind": "darwin_proc_bsdinfo", "seconds": 2, "microseconds": 0}
            return real_identity(pid)
        probe = None if reused else ProcessLookupError()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=probe, return_value=None) as kill, \
             patch.object(server, "process_start_identity", side_effect=process_identity), \
             patch.object(server, "init_logging"), \
             patch.object(server, "usage_totals", server.usage_totals), \
             patch.object(server.ProxyHTTPServer, "serve_forever", serve_once), \
             patch.object(server.ProxyHTTPServer, "shutdown", lambda http: None):
            server.main()
        self.assertTrue(kill.call_args_list)
        self.assertTrue(all(call.args == (99999999, 0) for call in kill.call_args_list))
        self.assertEqual(len(observed), 1)
        new_records = read_instance_set(state)
        self.assertEqual(new_records["active"]["listen"]["port"], new_port)
        self.assertEqual(new_records["install"]["install_id"], old_records["install"]["install_id"])
        self.assertEqual(new_records["active"]["runtime_paths"], old_records["active"]["runtime_paths"])
        self.assertEqual(new_records["active"]["locks"], old_records["active"]["locks"])
        self.assertNotEqual(new_records["active"]["instance_nonce"], old_records["active"]["instance_nonce"])
        self.assertEqual(ledger.read_bytes(), before)

    def test_verified_legacy_ledger_preparation_does_not_mutate_snapshot_or_disk(self):
        from core.config_snapshot import read_stable_config
        ledger = self.paths["totals"]
        old = {"version": 2, "since": "2026-01-01", "keep_days": 400,
               "total": {"requests": 1, "ok": 1, "fail": 0, "sum_ms": 10,
                         "combos": {"supply=s1|route=r1|strategy=cc":
                                    {"requests": 1, "ok": 1, "fail": 0, "usage_in": 10, "usage_out": 20}}},
               "days": {}, "months_archive": {}}
        ledger.write_text(json.dumps(old))
        before = ledger.read_bytes()
        snapshot = read_stable_config(ledger)
        store = server.UsageTotalsStore.from_verified_snapshot(ledger, snapshot)
        self.assertEqual(store._data["version"], 3)
        self.assertEqual(snapshot.config["version"], 2)
        self.assertNotIn("max_ms", snapshot.config["total"])
        self.assertEqual(ledger.read_bytes(), before)

    def test_server_bound_entry_without_verified_guard_never_resets_ledger(self):
        ledger = self.paths["totals"]
        ledger.write_text("{bad fake ledger")
        before, inode = ledger.read_bytes(), ledger.stat().st_ino
        with patch.object(server, "init_logging") as logging, \
             patch.object(server, "UsageTotalsStore") as totals, \
             patch("os.replace", side_effect=AssertionError("unexpected rename")) as replace:
            with self.assertRaises(server.PreflightError) as caught:
                server._run_bound_server(None, self.config_file, {}, None, self.root, {}, {}, {}, None, None)
        self.assertEqual(caught.exception.code, "ledger_unverified")
        logging.assert_not_called()
        totals.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(ledger.stat().st_ino, inode)
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])

    def test_log_retention_can_trim_before_later_startup_failure(self):
        # Existing normal retention policy is intentionally not transactional.
        # This uses only a temporary file, never a real service or log handler.
        log_path = self.paths["log"]
        log_path.write_text("2000-01-01 00:00:00,000 old\n"
                            "2099-01-01 00:00:00,000 retained\n")
        ledger = self.paths["totals"]
        ledger.write_text("fake ledger untouched")
        proxy = Mock()
        version = Mock()
        version.read_text.return_value = "fake"
        failure = server.PreflightError("startup_config_changed", "fake")
        with patch.object(server, "_runtime_paths", {"log": log_path, "totals": ledger}), \
             patch.object(server, "_VERSION_FILE", version), \
             patch.object(server, "_VERSION", "unknown"), \
             patch.object(server, "usage_totals", None), \
             patch.object(server, "_verify_migration_guard", side_effect=[None, failure]), \
             patch.object(server.access_log, "handlers", []), \
             patch.object(server.access_log, "addHandler"), \
             patch("logging.FileHandler"), patch("logging.basicConfig"), \
             patch.object(server, "write_instance_record") as write:
            with self.assertRaises(server.PreflightError) as caught:
                server._run_bound_server(proxy, self.config_file, {}, None, self.root, {},
                                         {"serving_started": False}, {}, None, None,
                                         (None, None, None, None), {})
        self.assertEqual(caught.exception.code, "startup_config_changed")
        self.assertEqual(log_path.read_text(), "2099-01-01 00:00:00,000 retained\n")
        self.assertEqual(ledger.read_text(), "fake ledger untouched")
        write.assert_not_called()
        proxy.serve_forever.assert_not_called()

    def test_absent_verified_ledger_does_not_create_or_read_file(self):
        ledger = self.paths["totals"]
        with patch("builtins.open", side_effect=AssertionError("unexpected ledger read")):
            store = server.UsageTotalsStore.from_verified_snapshot(ledger, None)
        self.assertEqual(store._data["version"], 3)
        self.assertFalse(ledger.exists())

    def test_stopped_old_instance_can_change_listen_without_touching_ledger(self):
        self._assert_stopped_instance_can_change_listen()

    def test_explicitly_reused_pid_can_change_listen_without_signalling_it(self):
        self._assert_stopped_instance_can_change_listen(reused=True)

    def test_real_fixture_process_live_then_exit_allows_listen_migration(self):
        import select
        import subprocess
        import sys
        from core.discovery import (process_executable, process_start_identity, read_instance_set,
                                    write_instance_record)
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        fake_home = self.root / "fake-home"
        fake_temp = self.root / "fake-tmp"
        fake_home.mkdir(mode=0o700)
        fake_temp.mkdir(mode=0o700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            old_records = self._seed_stopped_instance(old)
            old_port = old.getsockname()[1]
        code = ("import socket,sys\n"
                "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
                "s.bind(('127.0.0.1',int(sys.argv[1])))\n"
                "s.listen(1)\n"
                "print('READY',flush=True)\n"
                "sys.stdin.read(1)\n"
                "s.close()\n")
        env = os.environ.copy()
        env.update(HOME=str(fake_home), USERPROFILE=str(fake_home), LOCALAPPDATA=str(fake_home),
                   TMPDIR=str(fake_temp), TMP=str(fake_temp), TEMP=str(fake_temp),
                   MODEL_PROXY_CONFIG=str(self.config_file), PYTHONDONTWRITEBYTECODE="1")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                    "all_proxy", "MODEL_PROXY_PORT", "MODEL_PROXY_START_NONCE"):
            env.pop(key, None)
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", code, str(old_port)],
                                cwd=self.root, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 5)
            self.assertTrue(ready, "fixture child did not report readiness")
            self.assertEqual(proc.stdout.readline().strip(), "READY")
            start, executable = process_start_identity(proc.pid), process_executable(proc.pid)
            self.assertIsNotNone(start)
            self.assertIsNotNone(executable)
            state = self.root / ".runtime"
            snapshot = inspect_runtime_identities(self.root, self.paths)
            with acquire_lock(snapshot, "anchor.lock", access_verifier=verify_local_access) as anchor:
                with acquire_lock(anchor.snapshot, "lock", access_verifier=verify_local_access, anchor=anchor) as legacy:
                    records = read_instance_set(state)
                    active = dict(records["active"], pid=proc.pid, process_start=start, executable=executable)
                    write_instance_record(state, "active.json", active, anchor=anchor, legacy=legacy)
            new_port = self._unused_fixture_port()
            self._write_listen_config(new_port)
            before, ledger_before = self._record_bytes(), ledger.read_bytes()
            stderr = io.StringIO()
            with patch.object(server, "resolve_listen", side_effect=real_resolve), \
                 patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    server.main()
            self.assertIn("process_still_active", stderr.getvalue())
            bind.assert_not_called()
            self.assertIsNone(proc.poll())
            self.assertEqual(self._record_bytes(), before)
            self.assertEqual(ledger.read_bytes(), ledger_before)
            proc.stdin.write("x")
            proc.stdin.flush()
            self.assertEqual(proc.wait(timeout=5), 0)
            observed = []
            def serve_once(http):
                observed.append(read_instance_set(state))
            with patch.object(server, "resolve_listen", side_effect=real_resolve), \
                 patch.object(server, "init_logging"), \
                 patch.object(server, "usage_totals", server.usage_totals), \
                 patch.object(server.ProxyHTTPServer, "serve_forever", serve_once), \
                 patch.object(server.ProxyHTTPServer, "shutdown", lambda http: None):
                server.main()
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["active"]["state"], "READY")
            self.assertEqual(observed[0]["active"]["listen"]["port"], new_port)
            self.assertEqual(observed[0]["install"]["install_id"], old_records["install"]["install_id"])
            self.assertEqual(ledger.read_bytes(), ledger_before)
        finally:
            if proc.poll() is None:
                try:
                    proc.stdin.write("x")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()  # Only the child created by this test.
                    proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin")
    def test_same_port_ipv4_to_ipv6_fails_without_changing_records_or_ledger(self):
        from core.discovery import read_instance_set
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            old_records = self._seed_stopped_instance(old)
            old_port = old.getsockname()[1]
        self.config_file.write_text(json.dumps({"supplies": [], "routes": [], "strategies": [],
                                                "listen": {"host": "::1", "port": old_port}}))
        ledger_before = ledger.read_bytes()
        records_before = self._record_bytes()
        state = self.root / ".runtime"
        # Neither listener construction nor bind is mocked. The old baseline
        # must itself reject IPv6 on its actual IPv4 socket.
        try:
            unexpected = server.ThreadingHTTPServer(("::1", 0), server.ModelProxyHandler)
        except OSError as exc:
            baseline_error_name = type(exc).__name__
        else:
            unexpected.server_close()
            self.fail("Darwin IPv4 baseline unexpectedly bound IPv6")
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "init_logging") as log, \
             patch.object(server.ProxyHTTPServer, "serve_forever") as serve, \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn(f"startup failed ({baseline_error_name})", stderr.getvalue())
        self.assertEqual(self._record_bytes(), records_before)
        self.assertEqual(read_instance_set(state), old_records)
        self.assertEqual(ledger.read_bytes(), ledger_before)
        log.assert_not_called()
        serve.assert_not_called()

    def test_listen_migration_allows_own_log_replacement_without_touching_ledger(self):
        from core.discovery import read_instance_set
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        self.paths["log"].write_text("old fake log")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        new_port = self._unused_fixture_port()
        self._write_listen_config(new_port)
        before = ledger.read_bytes()
        state = self.root / ".runtime"
        def fake_trim(path):
            replacement = path.with_name("fake-log-trim")
            replacement.write_text("trimmed fake log")
            os.replace(replacement, path)
        observed = []
        def serve_once(http):
            observed.append(read_instance_set(state))
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "init_logging", side_effect=fake_trim), \
             patch.object(server, "usage_totals", server.usage_totals), \
             patch.object(server.ProxyHTTPServer, "serve_forever", serve_once), \
             patch.object(server.ProxyHTTPServer, "shutdown", lambda http: None):
            server.main()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["active"]["state"], "READY")
        self.assertEqual(observed[0]["active"]["listen"]["port"], new_port)
        self.assertEqual(self.paths["log"].read_text(), "trimmed fake log")
        self.assertEqual(ledger.read_bytes(), before)

    def test_old_live_identity_blocks_listen_migration_before_bind(self):
        from core import platform_process
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            records = self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        before, ledger_before = self._record_bytes(), ledger.read_bytes()
        stderr = io.StringIO()
        state_patch = (patch.object(platform_process, "darwin_recorded_state", return_value="ALIVE")
                       if hasattr(platform_process, "darwin_recorded_state") else nullcontext())
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", return_value=None), \
             patch.object(server, "process_start_identity", return_value=records["active"]["process_start"]), \
             state_patch as state, \
             patch.object(server, "ProxyHTTPServer") as bind, \
             patch.object(server, "_reserve_previous_listener") as reserve, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        if state is not None and sys.platform == "darwin":
            state.assert_called_once_with(records["active"]["pid"], records["active"]["process_start"])
        self.assertIn("process_still_active", stderr.getvalue())
        bind.assert_not_called()
        reserve.assert_not_called()
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_old_port_occupied_blocks_listen_migration_without_touching_owner(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
            self._write_listen_config(self._unused_fixture_port())
            before, ledger_before = self._record_bytes(), ledger.read_bytes()
            stderr = io.StringIO()
            with patch.object(server, "resolve_listen", side_effect=real_resolve), \
                 patch.object(server.os, "kill", side_effect=ProcessLookupError()) as probe, \
                 patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    server.main()
            self.assertIn("old_listener_busy", stderr.getvalue())
            self.assertTrue(all(call.args == (99999999, 0) for call in probe.call_args_list))
            bind.assert_not_called()
            self.assertGreaterEqual(old.fileno(), 0)
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_new_port_occupied_does_not_modify_records_or_ledger(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self._write_listen_config(occupied.getsockname()[1])
            before, ledger_before = self._record_bytes(), ledger.read_bytes()
            with patch.object(server, "resolve_listen", side_effect=real_resolve), \
                 patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
                 patch.object(server, "init_logging") as logging, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    server.main()
            logging.assert_not_called()
            self.assertGreaterEqual(occupied.fileno(), 0)
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_legacy_lock_conflict_blocks_migration_without_touching_ledger(self):
        import fcntl
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        before, ledger_before = self._record_bytes(), ledger.read_bytes()
        stderr = io.StringIO()
        with self.paths["lock"].open("r+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(server, "resolve_listen", side_effect=real_resolve), \
                 patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    server.main()
            bind.assert_not_called()
        self.assertIn("lock_conflict", stderr.getvalue())
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_replaced_legacy_lock_identity_blocks_migration(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        old_lock = self.paths["lock"]
        old_inode = old_lock.stat().st_ino
        replacement = self.root / "runtime" / "lock-replacement"
        replacement.write_text("replacement lock")
        replacement.chmod(0o600)
        self.assertNotEqual(replacement.stat().st_ino, old_inode)
        os.replace(replacement, old_lock)
        self._write_listen_config(self._unused_fixture_port())
        before, ledger_before = self._record_bytes(), ledger.read_bytes()
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("lock_identity_changed", stderr.getvalue())
        bind.assert_not_called()
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_bytes(), ledger_before)
        self.assertEqual(old_lock.read_text(), "replacement lock")

    def test_config_change_during_old_port_reservation_aborts_migration(self):
        from contextlib import contextmanager
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        before, ledger_before = self._record_bytes(), ledger.read_bytes()
        real_reserve = server._reserve_previous_listener
        @contextmanager
        def change_config_during_reserve(listen):
            with real_reserve(listen) as listener:
                config = json.loads(self.config_file.read_text())
                config["generation"] = "changed during reserve"
                self.config_file.write_text(json.dumps(config))
                yield listener
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "_reserve_previous_listener", change_config_during_reserve), \
             patch.object(server, "init_logging") as logging, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("startup_config_changed", stderr.getvalue())
        logging.assert_not_called()
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_ledger_change_during_record_writes_rolls_records_back(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        before = self._record_bytes()
        changed = json.dumps({"version": 3, "since": "2026-01-01", "keep_days": 400,
                              "total": dict(server._zero_bucket(), sentinel="external-change"),
                              "days": {}, "months_archive": {}})
        real_write = server.write_instance_record
        injected = False
        def change_ledger(state, name, record, **kwargs):
            nonlocal injected
            real_write(state, name, record, **kwargs)
            if not injected and name == "active.json" and record["state"] == "STARTING":
                injected = True
                ledger.write_text(changed)
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "write_instance_record", side_effect=change_ledger), \
             patch.object(server, "init_logging") as logging, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertTrue(injected)
        self.assertIn("ledger_changed", stderr.getvalue())
        logging.assert_not_called()
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(ledger.read_text(), changed)
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])

    def test_migration_rollback_failure_is_not_suppressed(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        ledger_before = ledger.read_bytes()
        real_write = server.write_instance_record
        def fail_after_active(state, name, record, **kwargs):
            real_write(state, name, record, **kwargs)
            if name == "active.json" and record["state"] == "STARTING":
                raise OSError("fake post-write failure")
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "write_instance_record", side_effect=fail_after_active), \
             patch.object(server, "_restore_prestart_records", side_effect=OSError("fake rollback failure")), \
             redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("record_cleanup_failed", stderr.getvalue())
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_runtime_totals_path_drift_is_blocked_and_requires_migration(self):
        from core.listen_config import resolve_listen as real_resolve
        old_ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        new_ledger = self.root / "runtime" / "other-ledger"
        new_ledger.write_text("other ledger sentinel")
        self.paths["totals"] = new_ledger
        self.path_file.write_text(json.dumps({key: str(value) for key, value in self.paths.items()}))
        self._write_listen_config(self._unused_fixture_port())
        before = self._record_bytes()
        old_bytes, new_bytes = old_ledger.read_bytes(), new_ledger.read_bytes()
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("runtime_drift", stderr.getvalue())
        self.assertIn("migration required", stderr.getvalue())
        bind.assert_not_called()
        self.assertEqual(self._record_bytes(), before)
        self.assertEqual(old_ledger.read_bytes(), old_bytes)
        self.assertEqual(new_ledger.read_bytes(), new_bytes)

    def test_absent_ledger_migration_starts_empty_and_can_record(self):
        from core.discovery import read_instance_set
        from core.listen_config import resolve_listen as real_resolve
        ledger = self.paths["totals"]
        self.assertFalse(ledger.exists())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        new_port = self._unused_fixture_port()
        self._write_listen_config(new_port)
        state = self.root / ".runtime"
        observed = []
        def serve_once(http):
            self.assertFalse(ledger.exists())
            records = read_instance_set(state)
            self.assertEqual(records["active"]["state"], "READY")
            server.usage_totals.record({"supply": "s1", "route": "r1", "strategy": "cc",
                                        "status": 200, "usage_in": 4, "usage_out": 5,
                                        "attempts": 1, "attempt_errors": []}, 7)
            observed.append(records)
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "init_logging"), \
             patch.object(server, "usage_totals", server.usage_totals), \
             patch.object(server.ProxyHTTPServer, "serve_forever", serve_once), \
             patch.object(server.ProxyHTTPServer, "shutdown", lambda http: None):
            server.main()
        self.assertEqual(len(observed), 1)
        self.assertEqual(json.loads(ledger.read_text())["total"]["requests"], 1)
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])

    def test_semantically_invalid_ledgers_never_write_or_reset_during_migration(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        records_before = self._record_bytes()
        combo_key = "supply=s1|route=r1|strategy=cc"
        total = server._zero_bucket()
        total["combos"][combo_key] = server._zero_combo()
        day = server._zero_bucket()
        month = server._zero_bucket()
        good = {"version": 3, "since": "2026-01-01", "keep_days": 400,
                "total": total, "days": {"2026-01-01": day}, "months_archive": {"2026-01": month},
                "private_value": "FAKE_PRIVATE_VALUE"}
        cases = []
        bad = copy.deepcopy(good); del bad["total"]["ok"]; cases.append(("missing total ok", bad))
        bad = copy.deepcopy(good); del bad["days"]["2026-01-01"]["sum_ms"]; cases.append(("missing day sum", bad))
        bad = copy.deepcopy(good); bad["months_archive"]["2026-01"]["combos"] = []; cases.append(("bad month combos", bad))
        bad = copy.deepcopy(good); bad["total"]["combos"][combo_key]["usage_in"] = "bad"; cases.append(("bad combo", bad))
        bad = copy.deepcopy(good); bad["total"]["max_ms"] = None; cases.append(("bad optional field", bad))
        bad = copy.deepcopy(good); bad["total"]["requests"] = True; cases.append(("boolean counter", bad))
        bad = copy.deepcopy(good); bad["total"]["fail"] = -1; cases.append(("negative count", bad))
        bad = copy.deepcopy(good); bad["days"] = []; cases.append(("bad days", bad))
        bad = copy.deepcopy(good); bad["days"]["2026-02-30"] = server._zero_bucket(); cases.append(("bad day key", bad))
        bad = copy.deepcopy(good); bad["version"] = 99; cases.append(("unknown version", bad))
        for label, value in cases:
            with self.subTest(case=label):
                ledger.write_text(json.dumps(value))
                before, inode = ledger.read_bytes(), ledger.stat().st_ino
                stderr = io.StringIO()
                with patch.object(server, "resolve_listen", side_effect=real_resolve), \
                     patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
                     patch.object(server, "ProxyHTTPServer") as bind, \
                     patch.object(server, "_reserve_previous_listener") as reserve, \
                     patch.object(server, "init_logging") as logging, \
                     patch("os.replace", side_effect=AssertionError("unexpected write")) as replace, \
                     redirect_stderr(stderr):
                    with self.assertRaises(SystemExit):
                        server.main()
                self.assertIn("ledger_unverified", stderr.getvalue())
                self.assertNotIn("FAKE_PRIVATE_VALUE", stderr.getvalue())
                bind.assert_not_called()
                reserve.assert_not_called()
                logging.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(ledger.read_bytes(), before)
                self.assertEqual(ledger.stat().st_ino, inode)
                self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])
                self.assertEqual(self._record_bytes(), records_before)

    def _assert_migrated_ledger_records_new_usage(self, version, *, same_listen=False, omit_version=False):
        from core.discovery import read_instance_set
        from core.listen_config import resolve_listen as real_resolve
        ledger = self.paths["totals"]
        combo_key = "supply=s1|route=r1|strategy=cc"
        combo = {"requests": 2, "ok": 1, "fail": 1, "usage_in": 10, "usage_out": 20}
        bucket = {"requests": 2, "ok": 1, "fail": 1, "sum_ms": 30,
                  "combos": {combo_key: combo}}
        if version == 3:
            bucket.update(client_disconnect=0, max_ms=20)
            combo.update(client_disconnect=0, attempts=2, attempt_fail=1)
        day_key = server._cst_now().strftime("%Y-%m-%d")
        data = {"version": version, "since": "2026-01-01", "keep_days": 400,
                "total": copy.deepcopy(bucket), "days": {day_key: copy.deepcopy(bucket)},
                "months_archive": {}}
        if omit_version:
            self.assertEqual(version, 2)
            del data["version"]
        ledger.write_text(json.dumps(data))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
            old_port = old.getsockname()[1]
        new_port = old_port if same_listen else self._unused_fixture_port()
        self._write_listen_config(new_port)
        before, inode = ledger.read_bytes(), ledger.stat().st_ino
        state = self.root / ".runtime"
        observed = []
        def serve_once(http):
            records = read_instance_set(state)
            self.assertEqual(records["active"]["state"], "READY")
            self.assertEqual(ledger.read_bytes(), before)
            self.assertEqual(ledger.stat().st_ino, inode)
            self.assertEqual(server.usage_totals._data["version"], 3)
            self.assertEqual(server.usage_totals._data["total"]["requests"], 2)
            self.assertEqual(server.usage_totals._data["total"]["combos"][combo_key]["usage_in"], 10)
            server.usage_totals.record({"supply": "s1", "route": "r1", "strategy": "cc",
                                        "status": 200, "usage_in": 4, "usage_out": 5,
                                        "attempts": 1, "attempt_errors": []}, 7)
            observed.append(records)
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "init_logging"), \
             patch.object(server, "usage_totals", server.usage_totals), \
             patch.object(server.ProxyHTTPServer, "serve_forever", serve_once), \
             patch.object(server.ProxyHTTPServer, "shutdown", lambda http: None):
            server.main()
        self.assertEqual(len(observed), 1)
        actual = json.loads(ledger.read_text())
        self.assertEqual(actual["version"], 3)
        self.assertEqual(actual["total"]["requests"], 3)
        self.assertEqual(actual["total"]["combos"][combo_key]["usage_in"], 14)
        self.assertEqual(actual["days"][day_key]["requests"], 3)
        for persisted in (actual["total"], actual["days"][day_key]):
            self.assertEqual((persisted["ok"], persisted["fail"], persisted["sum_ms"]), (2, 1, 37))
            self.assertEqual(
                (persisted["combos"][combo_key]["requests"], persisted["combos"][combo_key]["ok"],
                 persisted["combos"][combo_key]["fail"], persisted["combos"][combo_key]["usage_in"],
                 persisted["combos"][combo_key]["usage_out"]),
                (3, 2, 1, 14, 25),
            )
        self.assertNotEqual(ledger.read_bytes(), before)
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])

    def test_valid_v2_migration_continues_real_recording(self):
        self._assert_migrated_ledger_records_new_usage(2)

    def test_missing_version_migration_continues_real_recording(self):
        self._assert_migrated_ledger_records_new_usage(2, omit_version=True)

    def test_valid_v3_migration_continues_real_recording(self):
        self._assert_migrated_ledger_records_new_usage(3)

    def test_same_listen_restart_revalidates_after_preflight(self):
        from core.config_snapshot import read_stable_config
        from core.listen_config import resolve_listen as real_resolve
        ledger = self._ledger_fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
            old_port = old.getsockname()[1]
        # Simulate successful CLI preflight, then a change after the old stop.
        server.validate_migration_ledger_snapshot(read_stable_config(ledger))
        data = json.loads(ledger.read_text())
        del data["total"]["ok"]
        ledger.write_text(json.dumps(data))
        self._write_listen_config(old_port)
        before, inode, records_before = ledger.read_bytes(), ledger.stat().st_ino, self._record_bytes()
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "ProxyHTTPServer") as bind, \
             patch.object(server, "init_logging") as logging, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("ledger_unverified", stderr.getvalue())
        bind.assert_not_called()
        logging.assert_not_called()
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(ledger.stat().st_ino, inode)
        self.assertEqual(self._record_bytes(), records_before)
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])

    def test_same_listen_restart_valid_ledger_continues_recording(self):
        self._assert_migrated_ledger_records_new_usage(3, same_listen=True)

    def _assert_first_start_invalid_ledger_untouched(self, contents):
        ledger = self.paths["totals"]
        ledger.write_text(contents)
        before, inode = ledger.read_bytes(), ledger.stat().st_ino
        stderr = io.StringIO()
        with patch.object(server, "ProxyHTTPServer") as bind, \
             patch.object(server, "_confirm_initialization") as confirm, \
             patch.object(server, "init_logging") as logging, \
             patch.object(server, "write_instance_record") as record, \
             patch("os.replace", side_effect=AssertionError("unexpected write")) as replace, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("ledger_unverified", stderr.getvalue())
        bind.assert_not_called()
        confirm.assert_not_called()
        logging.assert_not_called()
        record.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(ledger.stat().st_ino, inode)
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])
        state = self.root / ".runtime"
        for name in ("install.json", "active.json", "last_good_paths.json"):
            self.assertFalse((state / name).exists())

    def test_first_start_invalid_semantics_does_not_reset_ledger(self):
        data = {"version": 3, "total": {"requests": 1, "fail": 0, "sum_ms": 2, "combos": {}},
                "days": {}, "months_archive": {}}
        self._assert_first_start_invalid_ledger_untouched(json.dumps(data))

    def test_first_start_invalid_json_does_not_rename_ledger(self):
        self._assert_first_start_invalid_ledger_untouched("{bad fake ledger")

    def test_corrupt_ledger_blocks_migration_without_rename_or_reset(self):
        from core.listen_config import resolve_listen as real_resolve
        ledger = self.paths["totals"]
        ledger.write_text("{invalid fake ledger")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old:
            old.bind(("127.0.0.1", 0))
            old.listen()
            self._seed_stopped_instance(old)
        self._write_listen_config(self._unused_fixture_port())
        before = self._record_bytes()
        stderr = io.StringIO()
        with patch.object(server, "resolve_listen", side_effect=real_resolve), \
             patch.object(server.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(server, "ProxyHTTPServer") as bind, redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertIn("ledger_unverified", stderr.getvalue())
        bind.assert_not_called()
        self.assertEqual(ledger.read_text(), "{invalid fake ledger")
        self.assertEqual(list(ledger.parent.glob("totals.corrupt.*")), [])
        self.assertEqual(self._record_bytes(), before)

    def test_previous_live_or_unknown_pid_is_never_treated_as_stale(self):
        record = {"runtime_paths": {"lock": "fake"}, "listen": {"host": "127.0.0.1", "port": 1},
                  "locks": {"anchor": "fake"}, "pid": 999999, "process_start": {"kind": "fake"}}
        args = ({"active": record}, record["runtime_paths"], record["listen"], record["locks"])
        for identity in (None, {"kind": "fake"}):
            with patch.object(server.os, "kill"), patch.object(server, "process_start_identity", return_value=identity):
                with self.assertRaises(server.PreflightError):
                    server._ensure_stale_instance(*args)
        with patch.object(server.os, "kill", side_effect=PermissionError()):
            with self.assertRaises(server.PreflightError):
                server._ensure_stale_instance(*args)
        with patch.object(server.os, "kill", side_effect=ProcessLookupError()):
            server._ensure_stale_instance(*args)
        with patch.object(server.os, "kill"), patch.object(server, "process_start_identity", return_value={"kind": "reused"}):
            server._ensure_stale_instance(*args)

    def test_corrupt_existing_observation_is_not_overwritten(self):
        state = self.root / ".runtime"
        state.mkdir(mode=0o700)
        record = state / "active.json"
        record.write_text("corrupt")
        record.chmod(0o600)
        class FakeServer:
            server_address = ("127.0.0.1", 49123)
            def __init__(self, *args): pass
            def server_close(self): pass
        with patch.object(server, "ProxyHTTPServer", FakeServer), \
             patch.object(server, "init_logging") as log, \
             patch.object(server, "UsageTotalsStore") as totals, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertEqual(record.read_text(), "corrupt")
        log.assert_not_called()
        totals.assert_not_called()


class ServerListenFamilyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "requires native Darwin")
    def test_darwin_real_ipv4_loopback_and_ipv6_match_baseline(self):
        # Real kernel sockets, no mocks: compare to the original constructor.
        # Only IPv4 loopback is bound; port zero is selected by the OS.
        for constructor in (server.ThreadingHTTPServer, server.ProxyHTTPServer):
            with self.subTest(constructor=constructor.__name__, host="127.0.0.1"):
                http = constructor(("127.0.0.1", 0), server.ModelProxyHandler)
                try:
                    self.assertEqual(http.address_family, socket.AF_INET)
                    self.assertEqual(http.socket.family, socket.AF_INET)
                    self.assertEqual(http.server_address[0], "127.0.0.1")
                    self.assertGreater(http.server_address[1], 0)
                finally:
                    http.server_close()

        # Test both IPv6 inputs on real IPv4 sockets without binding a wildcard.
        for host in ("::1", "::"):
            for constructor in (server.ThreadingHTTPServer, server.ProxyHTTPServer):
                with self.subTest(constructor=constructor.__name__, host=host):
                    http = constructor((host, 0), server.ModelProxyHandler,
                                       bind_and_activate=False)
                    try:
                        self.assertEqual(http.address_family, socket.AF_INET)
                        self.assertEqual(http.socket.family, socket.AF_INET)
                        self.assertEqual(http.server_address, (host, 0))
                    finally:
                        http.server_close()

        # IPv6 loopback must actually fail as it did for the baseline; do not
        # treat an unexpected success or a missing IPv6 stack as a skip.
        failures = []
        for constructor in (server.ThreadingHTTPServer, server.ProxyHTTPServer):
            with self.subTest(constructor=constructor.__name__, host="::1"):
                try:
                    unexpected = constructor(("::1", 0), server.ModelProxyHandler)
                except OSError as exc:
                    failures.append((type(exc), exc.errno))
                else:
                    unexpected.server_close()
                    self.fail("Darwin IPv4 listener unexpectedly bound IPv6")
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0], failures[1])

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows")
    def test_windows_ipv6_loopback_real_bind_on_independent_ephemeral_port(self):
        # A later native-Windows check of the listener class, not proof of
        # exclusivity or authorization to bypass the startup gate.
        if not socket.has_ipv6:
            self.skipTest("IPv6 unavailable")
        try:
            http = server.ProxyHTTPServer(("::1", 0), server.ModelProxyHandler)
        except OSError as exc:
            import errno
            if exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT):
                self.skipTest("IPv6 loopback unavailable")
            raise
        try:
            self.assertEqual(http.address_family, socket.AF_INET6)
            self.assertEqual(http.socket.family, socket.AF_INET6)
            self.assertEqual(http.server_address[0], "::1")
            self.assertGreater(http.server_address[1], 0)
            self.assertEqual(http.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 1)
        finally:
            http.server_close()

    def test_ipv4_literals_and_localhost_remain_ipv4_without_lan_bind(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("unexpected DNS")):
            self.assertEqual(server._listen_address_family("127.0.0.1", 12345), socket.AF_INET)
            self.assertEqual(server._listen_address_family("0.0.0.0", 12345), socket.AF_INET)
            self.assertEqual(server._listen_address_family("localhost", 12345), socket.AF_INET)
            self.assertEqual(server._listen_address_family("LOCALHOST", 12345), socket.AF_INET)
        candidates = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 12345, 0, 0)),
                      (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 12345))]
        with patch("socket.getaddrinfo", return_value=candidates):
            self.assertEqual(server._listen_address_family("fixture.invalid", 12345), socket.AF_INET)

    def test_ipv6_hostname_uses_ipv6_when_no_ipv4_candidate(self):
        candidates = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 12345, 0, 0))]
        with patch("socket.getaddrinfo", return_value=candidates):
            self.assertEqual(server._listen_address_family("fixture.invalid", 12345), socket.AF_INET6)

    def test_windows_ipv6_candidates_and_exclusive_option_are_mock_only(self):
        from unittest.mock import MagicMock, call
        exclusive = 0x7F01  # Synthetic constant, not a native Windows result.
        for host, v6only in (("::1", 1), ("::", 0)):
            with self.subTest(host=host), \
                 patch.object(server.os, "name", "nt"), \
                 patch.object(socket, "SO_EXCLUSIVEADDRUSE", exclusive, create=True):
                calls = []
                def fake_init(instance, address, handler, bind_and_activate):
                    calls.append((instance.address_family, address, bind_and_activate))
                with patch.object(server.ThreadingHTTPServer, "__init__", fake_init):
                    instance = server.ProxyHTTPServer((host, 23456), object)
                self.assertEqual(calls, [(socket.AF_INET6, (host, 23456, 0, 0), True)])
                instance.server_address = (host, 23456, 0, 0)
                instance.socket = MagicMock()
                with patch.object(server.ThreadingHTTPServer, "server_bind") as bind:
                    instance.server_bind()
                self.assertEqual(instance.socket.setsockopt.call_args_list, [
                    call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only),
                    call(socket.SOL_SOCKET, exclusive, 1),
                ])
                bind.assert_called_once()


class WindowsLockIntegrationGateTests(unittest.TestCase):
    """No service, files, native calls, or platform patch beyond the call."""
    def test_windows_rejects_even_an_allowing_callback_before_any_io(self):
        import core.platform_lock as locks
        callback = lambda path, kind: True
        with patch.object(locks.os, "name", "nt"), \
             patch.object(locks, "_fresh", side_effect=AssertionError("preflight forbidden")), \
             patch.object(locks.os, "open", side_effect=AssertionError("open forbidden")), \
             patch.object(locks, "_lock_fd", side_effect=AssertionError("lock forbidden")):
            for role in ("anchor.lock", "control.lock", "lock"):
                with self.assertRaises(LockError) as caught:
                    locks.acquire_lock(object(), role, create=True, access_verifier=callback)
                self.assertEqual(caught.exception.code, "windows_security_unverified")
                self.assertFalse(caught.exception.created)

    def test_unlock_error_still_closes_owned_descriptor_and_reports_failure(self):
        import core.platform_lock as locks
        with patch.object(locks.os, "name", "nt"), \
             patch.object(locks.os, "close") as close, \
             patch.object(locks, "_lock_fd", side_effect=OSError("fake unlock")) as unlock:
            held = locks.HeldLock(42, "control.lock", None)
            with self.assertRaises(LockError) as caught:
                held.close()
            self.assertEqual(caught.exception.code, "unlock_failed")
            self.assertTrue(held.closed)
            held.close()
        unlock.assert_called_once_with(42, unlock=True)
        close.assert_called_once_with(42)

    def test_windows_lock_branch_delegates_borrowed_fd(self):
        import core.platform_lock as locks
        import core.platform_windows as win
        with patch.object(locks.os, "name", "nt"), patch.object(win, "lock_fd") as primitive:
            locks._lock_fd(9)
            locks._lock_fd(9, unlock=True)
        self.assertEqual(primitive.call_args_list[0].args, (9,))
        self.assertEqual(primitive.call_args_list[0].kwargs, {"unlock": False})
        self.assertEqual(primitive.call_args_list[1].kwargs, {"unlock": True})


if __name__ == "__main__":
    unittest.main()
