"""POSIX flock tests in a fake tree; ACL verification here is deliberately mocked."""

import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import core.platform_lock as lock_module
from core.discovery import inspect_runtime_identities
from core.platform_lock import HeldLock, LockError, acquire_lock
from core.runtime_paths import PATH_KEYS


class PosixLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS 上 /var 是 /private/var 的符号链接：生产不变量要求父链无链接，
        # 用未解析的临时路径会让 acquire_lock 的 identity 校验直接拒绝。
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.runtime = self.base / "runtime"
        self.root.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        # 固定运行目录自 v0.14 起统一为 <root>/.runtime（.model_proxy_runtime 是
        # 已废弃的旧名，core/ 里已无任何引用）。锁的私有目录断言要求
        # target == state_dir/role，用旧名会恒被判 access_unverified。
        self.state = self.root / ".runtime"
        self.state.mkdir(mode=0o700)
        self.paths = {key: self.runtime / key for key in PATH_KEYS}
        self.check_fake_acl = lambda path, kind: True

    def snapshot(self):
        return inspect_runtime_identities(self.root, self.paths)

    def _child_attempt(self):
        project = str(Path(__file__).resolve().parents[1])
        code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.discovery import inspect_runtime_identities
from core.platform_lock import LockError, acquire_lock
from core.runtime_paths import PATH_KEYS
root, runtime = Path(sys.argv[2]), Path(sys.argv[3])
paths = {key: runtime / key for key in PATH_KEYS}
try:
    with acquire_lock(inspect_runtime_identities(root, paths), "anchor.lock", access_verifier=lambda p, k: True):
        print("acquired")
except LockError as exc:
    print(exc.code)
'''
        return subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code,
                               project, str(self.root), str(self.runtime)],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_real_two_process_flock_conflicts_without_truncating(self):
        anchor_path = self.state / "anchor.lock"
        anchor_path.write_text("fake-lock-bytes", encoding="utf-8")
        anchor_path.chmod(0o600)
        inode = anchor_path.stat().st_ino
        with acquire_lock(self.snapshot(), "anchor.lock", access_verifier=self.check_fake_acl) as held:
            self.assertFalse(os.get_inheritable(held.fileno()))
            self.assertEqual(self._child_attempt(), "lock_conflict")
            self.assertEqual(anchor_path.read_text(encoding="utf-8"), "fake-lock-bytes")
            self.assertEqual(anchor_path.stat().st_ino, inode)
        self.assertEqual(self._child_attempt(), "acquired")
        self.assertEqual(anchor_path.read_text(encoding="utf-8"), "fake-lock-bytes")
        self.assertTrue(anchor_path.exists())

    def test_new_anchor_is_private_and_persists_after_unlock(self):
        anchor_path = self.state / "anchor.lock"
        with acquire_lock(self.snapshot(), "anchor.lock", create=True,
                          access_verifier=self.check_fake_acl):
            self.assertEqual(stat.S_IMODE(anchor_path.stat().st_mode), 0o600)
        self.assertTrue(anchor_path.exists())
        self.assertEqual(anchor_path.read_bytes(), b"")

    def test_legacy_requires_anchor_and_anchor_cannot_close_first(self):
        snapshot = self.snapshot()
        with self.assertRaises(LockError) as caught:
            acquire_lock(snapshot, "lock", create=True, access_verifier=self.check_fake_acl)
        self.assertEqual(caught.exception.code, "lock_order")
        anchor = acquire_lock(snapshot, "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        legacy = acquire_lock(anchor.snapshot, "lock", create=True,
                              access_verifier=self.check_fake_acl, anchor=anchor)
        with self.assertRaises(LockError) as caught:
            anchor.close()
        self.assertEqual(caught.exception.code, "lock_order")
        self.assertFalse(anchor.closed)
        legacy.close()
        anchor.close()
        self.assertTrue(anchor.closed)

    def test_absent_or_rejected_acl_verifier_prevents_creation(self):
        with self.assertRaises(LockError) as caught:
            acquire_lock(self.snapshot(), "anchor.lock", create=True)
        self.assertEqual(caught.exception.code, "access_unverified")
        with self.assertRaises(LockError) as caught:
            acquire_lock(self.snapshot(), "anchor.lock", create=True,
                         access_verifier=lambda path, kind: False)
        self.assertEqual(caught.exception.code, "access_unverified")
        self.assertFalse((self.state / "anchor.lock").exists())

    def test_verifier_exception_does_not_leak_its_message(self):
        def fail(path, kind):
            raise RuntimeError("FAKE_SECRET")
        with self.assertRaises(LockError) as caught:
            acquire_lock(self.snapshot(), "anchor.lock", create=True, access_verifier=fail)
        self.assertNotIn("FAKE_SECRET", str(caught.exception))
        self.assertFalse((self.state / "anchor.lock").exists())

    def test_post_create_verifier_failure_reports_created_file(self):
        with self.assertRaises(LockError) as caught:
            acquire_lock(self.snapshot(), "anchor.lock", create=True,
                         access_verifier=lambda path, kind: kind == "directory")
        self.assertEqual(caught.exception.code, "access_unverified")
        self.assertTrue(caught.exception.created)
        anchor_path = self.state / "anchor.lock"
        self.assertEqual(anchor_path.read_bytes(), b"")
        with acquire_lock(self.snapshot(), "anchor.lock", access_verifier=self.check_fake_acl):
            pass

    def test_stale_preflight_rejected_before_lock_creation(self):
        snapshot = self.snapshot()
        self.paths["log"].write_text("fake log", encoding="utf-8")
        with self.assertRaises(LockError) as caught:
            acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=self.check_fake_acl)
        self.assertEqual(caught.exception.code, "preflight_changed")
        self.assertFalse((self.state / "anchor.lock").exists())
        self.assertEqual(self.paths["log"].read_text(encoding="utf-8"), "fake log")

    def test_insecure_existing_permissions_are_not_silently_changed(self):
        anchor_path = self.state / "anchor.lock"
        anchor_path.write_text("fake", encoding="utf-8")
        anchor_path.chmod(0o644)
        with self.assertRaises(LockError):
            acquire_lock(self.snapshot(), "anchor.lock", access_verifier=self.check_fake_acl)
        self.assertEqual(stat.S_IMODE(anchor_path.stat().st_mode), 0o644)
        self.assertEqual(anchor_path.read_text(encoding="utf-8"), "fake")

    def test_unsafe_state_dir_does_not_create_a_lock(self):
        self.state.chmod(0o755)
        with self.assertRaises(LockError) as caught:
            acquire_lock(self.snapshot(), "anchor.lock", create=True, access_verifier=self.check_fake_acl)
        self.assertEqual(caught.exception.code, "access_unverified")
        self.assertFalse((self.state / "anchor.lock").exists())

    def test_symlink_created_between_check_and_open_is_not_followed(self):
        self.paths["log"].write_text("fake important data", encoding="utf-8")
        snapshot = self.snapshot()
        original_open = os.open
        anchor_path = self.state / "anchor.lock"

        def replace_before_open(path, *args, **kwargs):
            if Path(path) == anchor_path:
                anchor_path.symlink_to(self.paths["log"])
            return original_open(path, *args, **kwargs)

        with patch("core.platform_lock.os.open", side_effect=replace_before_open):
            with self.assertRaises(LockError) as caught:
                acquire_lock(snapshot, "anchor.lock", create=True, access_verifier=self.check_fake_acl)
        self.assertEqual(caught.exception.code, "lock_open_failed")
        self.assertEqual(self.paths["log"].read_text(encoding="utf-8"), "fake important data")

    def test_anchor_close_waits_until_legacy_is_registered(self):
        anchor = acquire_lock(self.snapshot(), "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        entered = threading.Event()
        resume = threading.Event()
        close_started = threading.Event()
        close_done = threading.Event()
        results = {}
        close_lock_attempted = threading.Event()
        underlying_lock = anchor._state_lock

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "fake-anchor-closer":
                    close_lock_attempted.set()
                underlying_lock.acquire()
                return self

            def __exit__(self, exc_type, exc, traceback):
                underlying_lock.release()

        anchor._state_lock = ObservedLock()
        original_fresh = lock_module._fresh

        def paused_fresh(snapshot):
            if not entered.is_set():
                entered.set()
                if not resume.wait(6):
                    raise RuntimeError("fake test barrier timed out")
            return original_fresh(snapshot)

        def acquire_legacy():
            try:
                results["legacy"] = acquire_lock(anchor.snapshot, "lock", create=True,
                                                  access_verifier=self.check_fake_acl, anchor=anchor)
            except Exception as exc:
                results["acquire_error"] = exc

        def close_anchor():
            close_started.set()
            try:
                anchor.close()
                results["close"] = "closed"
            except LockError as exc:
                results["close"] = exc.code
            finally:
                close_done.set()

        acquire_thread = threading.Thread(target=acquire_legacy)
        close_thread = threading.Thread(target=close_anchor, name="fake-anchor-closer")
        close_thread_started = False
        try:
            with patch("core.platform_lock._fresh", side_effect=paused_fresh):
                acquire_thread.start()
                self.assertTrue(entered.wait(3), "legacy acquisition did not reach fake barrier")
                close_thread.start()
                close_thread_started = True
                self.assertTrue(close_started.wait(3))
                # The fork guard is acquired before the per-anchor mutex.
                self.assertFalse(close_done.is_set(), "anchor closed while legacy acquisition was pending")
                # A separate fake process still cannot acquire the same anchor.
                self.assertEqual(self._child_attempt(), "lock_conflict")
                resume.set()
                acquire_thread.join(5)
                close_thread.join(5)
                self.assertFalse(acquire_thread.is_alive())
                self.assertFalse(close_thread.is_alive())
                self.assertNotIn("acquire_error", results)
                self.assertEqual(results.get("close"), "lock_order")
                self.assertIn("legacy", results)
                self.assertFalse(anchor.closed)
                self.assertFalse(results["legacy"].closed)
                self.assertEqual(self._child_attempt(), "lock_conflict")
        finally:
            resume.set()
            if acquire_thread.ident is not None:
                acquire_thread.join(5)
            if close_thread_started:
                close_thread.join(5)
            if "legacy" in results and not results["legacy"].closed:
                results["legacy"].close()
            if not anchor.closed:
                anchor.close()

    def test_same_thread_reentrant_close_cannot_bypass_pending_guard(self):
        anchor = acquire_lock(self.snapshot(), "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        seen = []

        def reentrant_verifier(path, kind):
            if not seen:
                try:
                    anchor.close()
                except LockError as exc:
                    seen.append(exc.code)
                else:
                    seen.append("closed")
            return True

        try:
            legacy = acquire_lock(anchor.snapshot, "lock", create=True,
                                  access_verifier=reentrant_verifier, anchor=anchor)
            try:
                self.assertEqual(seen, ["lock_order"])
                self.assertFalse(anchor.closed)
            finally:
                legacy.close()
        finally:
            if not anchor.closed:
                anchor.close()

    def test_failed_legacy_acquisition_releases_pending_guard(self):
        anchor = acquire_lock(self.snapshot(), "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        try:
            with self.assertRaises(LockError) as caught:
                acquire_lock(anchor.snapshot, "lock", create=True,
                             access_verifier=lambda path, kind: False, anchor=anchor)
            self.assertEqual(caught.exception.code, "access_unverified")
            self.assertFalse(self.paths["lock"].exists())
        finally:
            anchor.close()
        self.assertTrue(anchor.closed)

    def test_anchor_close_wins_before_acquisition_without_creating_legacy(self):
        anchor = acquire_lock(self.snapshot(), "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        snapshot = anchor.snapshot
        anchor.close()
        with self.assertRaises(LockError) as caught:
            acquire_lock(snapshot, "lock", create=True,
                         access_verifier=self.check_fake_acl, anchor=anchor)
        self.assertEqual(caught.exception.code, "lock_order")
        self.assertFalse(self.paths["lock"].exists())

    def test_concurrent_close_is_idempotent(self):
        anchor = acquire_lock(self.snapshot(), "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        start = threading.Event()
        errors = []

        def close_it():
            start.wait(3)
            try:
                anchor.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=close_it) for _ in range(4)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertTrue(anchor.closed)


    @unittest.skipUnless(hasattr(os, "fork"), "fork unavailable")
    def test_child_closes_inherited_lock_without_unlocking_parent(self):
        anchor = acquire_lock(self.snapshot(), "anchor.lock", create=True,
                              access_verifier=self.check_fake_acl)
        ready_r, ready_w = os.pipe()
        release_r, release_w = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(ready_r)
            os.close(release_w)
            try:
                os.write(ready_w, b"closed" if anchor.closed else b"open")
                os.read(release_r, 1)
            finally:
                os._exit(0)
        os.close(ready_w)
        os.close(release_r)
        try:
            self.assertEqual(os.read(ready_r, 16), b"closed")
            self.assertEqual(self._child_attempt(), "lock_conflict")
            anchor.close()
            # Child is still alive: inherited descriptor must not keep lock alive.
            self.assertEqual(self._child_attempt(), "acquired")
        finally:
            if not anchor.closed:
                anchor.close()
            os.write(release_w, b"x")
            os.close(release_w)
            os.close(ready_r)
            os.waitpid(child, 0)


if __name__ == "__main__":
    unittest.main()
