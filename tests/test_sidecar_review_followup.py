"""Review regressions with synthetic files and adapters; not platform-security tests."""
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_tmp  # noqa: E402  (同目录测试基建)
from core.commands import (
    CommandContext, SessionOverridesSidecar, SidecarWriteError,
    _atomic_write_json, handle_route_command,
)
from core.windows_secure_files import SecureWriteError as WindowsSecureWriteError
from tests import test_route_command as route_fixtures
from tests.test_route_sidecar_unknown import FakeSecureIO


class MasqueradingError(Exception):
    state = "NOT_REPLACED"

    def __init__(self, candidate):
        super().__init__("synthetic adapter secret")
        self.candidate = candidate


class ReviewAdapter(FakeSecureIO):
    def commit(self, path, baseline, content):
        if self.mode == "masquerade_after":
            self.mode = "ok"
            candidate = super().commit(path, baseline, content)
            self.mode = "masquerade_after"
            raise MasqueradingError(candidate)
        if self.mode.startswith("secret_"):
            try:
                raise RuntimeError("synthetic-secret-from-adapter")
            except RuntimeError as exc:
                if self.mode == "secret_not_replaced":
                    raise SidecarWriteError("NOT_REPLACED") from exc
                if self.mode == "secret_unknown":
                    raise SidecarWriteError("UNKNOWN") from exc
                raise ValueError("synthetic-secret-from-adapter") from exc
        return super().commit(path, baseline, content)


class TestReviewFollowup(unittest.TestCase):
    def setUp(self):
        self.temp = platform_tmp.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "session_overrides.json"
        self.io = ReviewAdapter()
        self.store = SessionOverridesSidecar(self.path, secure_io=self.io)
        self.store.apply_command("fake-token", "fake-session", "set", "B")

    def command(self, route):
        return handle_route_command(CommandContext(
            route, "fake-token", "fake-session",
            {"route_pool": [{"route_id": "B", "weight": 1}]},
            {"B": {"id": "B"}, "C": {"id": "C"}}, self.store, "B"))

    def test_real_replace_then_masquerading_error_is_isolated(self):
        self.io.mode = "masquerade_after"
        result = self.command("C")
        self.assertFalse(result.wrote)
        self.assertIn("未知", result.receipt_text)
        self.assertEqual(json.loads(self.path.read_bytes())["fake-token"]["fake-session"]["route_id"], "C")
        newer = self.store._mtime + 10
        os.utime(self.path, (newer, newer))
        for _ in range(3):
            self.store.maybe_reload()
            self.assertEqual(self.store.get_overrides_for("fake-token"), {"fake-session": "B"})
        self.assertFalse(self.store.recover())
        count = self.io.commits
        self.assertFalse(self.command("B").wrote)
        self.assertEqual(self.io.commits, count)
        self.assertEqual(self.store.get_overrides_for("fake-token"), {"fake-session": "B"})

    def test_real_forward_request_does_not_publish_masquerading_candidate(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        cfg_path = route_fixtures.TestForwardInterceptEndToEnd()._write_cfg(str(self.root))
        with patch("core.server._mac_business_enabled", return_value=False), \
             patch("core.session_identity.DEFAULT_SESSIONS_DIR", sessions), \
             patch("core.claudian_titles.DEFAULT_CLAUDIAN_SESSIONS_DIR", sessions):
            handler = route_fixtures._make_forward_handler(cfg_path, self.path)
            handler.server.sidecar_store = SessionOverridesSidecar(self.path, secure_io=self.io)
            sid = "fake-forward-session"
            self.assertEqual(route_fixtures._send(handler, "$route claude", sid)[0]["route"], "claude")
            self.io.mode = "masquerade_after"
            acc, raw = route_fixtures._send(handler, "$route nation", sid)
            self.assertEqual(acc["route"], "claude")
            self.assertIn("未知", raw.decode("utf-8"))
            self.assertEqual(json.loads(self.path.read_bytes())["cc"][sid]["route_id"], "nation")
            self.assertEqual(route_fixtures._send(handler, "ordinary request", sid)[0]["route"], "claude")
            self.assertEqual(route_fixtures._send(handler, "$route", sid)[0]["route"], "claude")

    def test_recovered_c_then_new_not_replaced_receipt(self):
        self.io.mode = "unknown_after"
        self.assertIn("未知", self.command("C").receipt_text)
        self.store.touch("fake-token", "fake-session")
        touched = self.store._mem_last_seen["fake-token", "fake-session"]
        self.io.mode = "before"
        result = self.command("B")
        self.assertFalse(result.wrote)
        self.assertIn("先前", result.receipt_text)
        self.assertIn("恢复", result.receipt_text)
        self.assertIn("本次", result.receipt_text)
        self.assertIn("未提交", result.receipt_text)
        self.assertNotIn("当前 override 未变", result.receipt_text)
        self.assertNotIn("fake-token", result.receipt_text)
        self.assertNotIn("fake-session", result.receipt_text)
        self.assertEqual(self.store.get_overrides_for("fake-token"), {"fake-session": "C"})
        self.assertEqual(self.store._mem_last_seen["fake-token", "fake-session"], touched)
        self.assertEqual(json.loads(self.path.read_bytes())["fake-token"]["fake-session"]["route_id"], "C")
        self.io.mode = "ok"
        self.assertTrue(self.command("B").wrote)
        self.assertEqual(self.store.get_overrides_for("fake-token"), {"fake-session": "B"})

    def test_recovered_b_then_new_not_replaced_preserves_normal_receipt(self):
        self.io.mode = "unknown_before"
        self.assertIn("未知", self.command("C").receipt_text)
        self.io.mode = "before"
        result = self.command("C")
        self.assertFalse(result.wrote)
        self.assertIn("当前 override 未变", result.receipt_text)
        self.assertEqual(self.store.get_overrides_for("fake-token"), {"fake-session": "B"})

    def test_recovered_c_then_failed_reset_has_accurate_receipt(self):
        self.io.mode = "unknown_after"
        self.assertIn("未知", self.command("C").receipt_text)
        self.io.mode = "before"
        result = self.command("reset")
        self.assertFalse(result.wrote)
        self.assertIn("先前", result.receipt_text)
        self.assertIn("恢复", result.receipt_text)
        self.assertIn("本次", result.receipt_text)
        self.assertIn("未提交", result.receipt_text)
        self.assertEqual(self.store.get_overrides_for("fake-token"), {"fake-session": "C"})

    def test_classified_uncertain_cleanup_or_close_remains_unknown(self):
        for attr in ("cleanup_unknown", "close_unknown"):
            with self.subTest(attr=attr), platform_tmp.TemporaryDirectory() as d:
                path = Path(d) / "sidecar.json"
                adapter = ReviewAdapter()
                store = SessionOverridesSidecar(path, secure_io=adapter)
                token, session = "fake-token", "fake-session"
                store.apply_command(token, session, "set", "B")
                baseline = store._baseline
                before_bytes = path.read_bytes()
                before_commits = adapter.commits
                error = SidecarWriteError("NOT_REPLACED")
                setattr(error, attr, True)
                with patch.object(adapter, "commit", side_effect=error):
                    with self.assertRaises(SidecarWriteError) as caught:
                        store.apply_command(token, session, "set", "C")
                self.assertEqual(caught.exception.state, "UNKNOWN")
                self.assertTrue(getattr(caught.exception, attr))
                self.assertTrue(getattr(store._resource_unknown, attr))
                self.assertTrue(SessionOverridesSidecar._same_snapshot(adapter.capture(path), baseline))
                self.assertEqual(path.read_bytes(), before_bytes)
                # Even proved B does not prove release/cleanup. No later path
                # may publish C or submit another write.
                with patch.object(adapter, "commit", wraps=adapter.commit) as retry:
                    store.maybe_reload()
                    self.assertFalse(store.recover())
                    with self.assertRaises(SidecarWriteError) as blocked:
                        store.apply_command(token, session, "set", "C")
                self.assertEqual(blocked.exception.state, "UNKNOWN")
                self.assertTrue(getattr(blocked.exception, attr))
                self.assertEqual(store.get_overrides_for(token), {session: "B"})
                self.assertEqual(path.read_bytes(), before_bytes)
                self.assertEqual(adapter.commits, before_commits)
                retry.assert_not_called()

    def test_windows_release_unknown_after_verified_target_stays_isolated(self):
        for translated in (False, True):
            with self.subTest(translated=translated), platform_tmp.TemporaryDirectory() as d:
                path = Path(d) / "sidecar.json"
                adapter = ReviewAdapter()
                store = SessionOverridesSidecar(path, secure_io=adapter)
                token, session = "fake-token", "fake-session"
                store.apply_command(token, session, "set", "B")
                store.touch(token, session)
                before_mem = store._mem_last_seen.copy()
                commit = adapter.commit

                def replace_then_release_unknown(path_arg, baseline, payload):
                    candidate = commit(path_arg, baseline, payload)
                    native = WindowsSecureWriteError(
                        "synthetic-release-secret", state="UNKNOWN", target_state="UNKNOWN",
                        release_unknown=True, target_was_verified=True, candidate=candidate)
                    if not translated:
                        raise native
                    # Even a mistaken NOT_REPLACED translation cannot erase release uncertainty.
                    error = SidecarWriteError("NOT_REPLACED", candidate,
                                              release_unknown=native.release_unknown)
                    error.target_was_verified = native.target_was_verified
                    raise error from native

                ctx = CommandContext(
                    "C", token, session, {"route_pool": [{"route_id": "B", "weight": 1}]},
                    {"B": {"id": "B"}, "C": {"id": "C"}}, store, "B")
                with patch.object(adapter, "commit", side_effect=replace_then_release_unknown):
                    result = handle_route_command(ctx)
                self.assertFalse(result.wrote)
                self.assertIn("未知", result.receipt_text)
                self.assertIn("资源", result.receipt_text)
                self.assertNotIn(token, result.receipt_text)
                before_bytes = path.read_bytes()
                self.assertEqual(json.loads(before_bytes)[token][session]["route_id"], "C")
                self.assertTrue(store._resource_unknown.release_unknown)
                if translated:
                    # Even exact identity/content evidence for C cannot prove
                    # that the unrelated resource was released.
                    candidate = store._unknown[1]
                    self.assertIsNotNone(candidate)
                    self.assertTrue(SessionOverridesSidecar._same_snapshot(adapter.capture(path), candidate))
                else:
                    # An unclassified platform exception cannot contribute C evidence.
                    self.assertIsNone(store._unknown[1])
                before_commits = adapter.commits
                with patch.object(adapter, "commit", wraps=adapter.commit) as retry:
                    for _ in range(2):
                        store.maybe_reload()
                        self.assertEqual(store.get_overrides_for(token), {session: "B"})
                    self.assertFalse(store.recover())
                    with self.assertRaises(SidecarWriteError) as blocked:
                        store.apply_command(token, session, "set", "B")
                self.assertEqual(blocked.exception.state, "UNKNOWN")
                self.assertTrue(blocked.exception.release_unknown)
                self.assertEqual(store.get_overrides_for(token), {session: "B"})
                self.assertEqual(store._mem_last_seen, before_mem)
                self.assertEqual(path.read_bytes(), before_bytes)
                self.assertEqual(adapter.commits, before_commits)
                retry.assert_not_called()

    def test_direct_api_traceback_hides_adapter_exception(self):
        for mode, state in (("secret_not_replaced", "NOT_REPLACED"),
                            ("secret_unknown", "UNKNOWN"),
                            ("secret_unclassified", "UNKNOWN")):
            with self.subTest(mode=mode), platform_tmp.TemporaryDirectory() as d:
                adapter = ReviewAdapter()
                store = SessionOverridesSidecar(Path(d) / "sidecar.json", secure_io=adapter)
                adapter.mode = mode
                token, session = "fake-token", "fake-session"
                try:
                    store.apply_command(token, session, "set", "C")
                except SidecarWriteError as exc:
                    self.assertEqual(exc.state, state)
                    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                else:
                    self.fail("expected SidecarWriteError")
                self.assertNotIn("synthetic-secret-from-adapter", formatted)
                self.assertNotIn("fake-token", formatted)
                self.assertNotIn("fake-session", formatted)

    def test_legacy_atomic_writer_traceback_hides_original_exception(self):
        for target in ("tempfile.mkstemp", "os.replace"):
            with self.subTest(target=target):
                data = {"fake-token": {"fake-session": "C"}}
                with patch("core.commands." + target,
                           side_effect=OSError("synthetic-secret-from-writer")):
                    try:
                        _atomic_write_json(self.path, data)
                    except SidecarWriteError as exc:
                        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    else:
                        self.fail("expected SidecarWriteError")
                self.assertNotIn("synthetic-secret-from-writer", formatted)
                self.assertNotIn("fake-token", formatted)
                self.assertNotIn("fake-session", formatted)


class _FileCloseSpy:
    """Track ownership after fdopen while using a real temporary file."""
    def __init__(self, file, fail_close):
        self.file = file
        self.fail_close = fail_close
        self.close_calls = 0

    def write(self, value):
        return self.file.write(value)

    def close(self):
        self.close_calls += 1
        self.file.close()
        if self.fail_close:
            raise OSError("synthetic-close-secret")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestLegacyWriterResources(unittest.TestCase):
    def setUp(self):
        self.temp = platform_tmp.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "session_overrides.json"

    def test_fdopen_failure_closes_owned_fd_once(self):
        original_close = os.close
        attempted = []
        descriptors = []

        def fail_fdopen(fd, *args, **kwargs):
            descriptors.append(fd)
            raise OSError("synthetic-fdopen-secret")

        def close(fd):
            attempted.append(fd)
            original_close(fd)

        with patch("core.commands.os.fdopen", side_effect=fail_fdopen), \
             patch("core.commands.os.close", side_effect=close), \
             patch("core.commands.os.replace") as replace:
            with self.assertRaises(SidecarWriteError) as caught:
                _atomic_write_json(self.path, {})
        self.assertEqual(caught.exception.state, "NOT_REPLACED")
        self.assertEqual(attempted, descriptors)
        self.assertEqual(len(attempted), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        replace.assert_not_called()

    def test_fdopen_and_owned_fd_close_failure_is_unknown(self):
        original_close = os.close
        attempted = []

        def fail_close(fd):
            attempted.append(fd)
            original_close(fd)
            raise OSError("synthetic-close-secret")

        with patch("core.commands.os.fdopen", side_effect=OSError("synthetic-fdopen-secret")), \
             patch("core.commands.os.close", side_effect=fail_close), \
             patch("core.commands.os.replace") as replace:
            with self.assertRaises(SidecarWriteError) as caught:
                _atomic_write_json(self.path, {})
        self.assertEqual(caught.exception.state, "UNKNOWN")
        self.assertEqual(len(attempted), 1)
        self.assertEqual(list(self.root.iterdir()), [])
        replace.assert_not_called()
        formatted = "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__))
        self.assertNotIn("synthetic-fdopen-secret", formatted)
        self.assertNotIn("synthetic-close-secret", formatted)

    def test_fdopen_close_failure_before_closing_is_unknown_and_not_retried(self):
        original_close = os.close
        attempted = []

        def fail_close(fd):
            attempted.append(fd)
            raise OSError("synthetic-close-secret")

        try:
            with patch("core.commands.os.fdopen", side_effect=OSError("synthetic-fdopen-secret")), \
                 patch("core.commands.os.close", side_effect=fail_close):
                with self.assertRaises(SidecarWriteError) as caught:
                    _atomic_write_json(self.path, {})
            self.assertEqual(caught.exception.state, "UNKNOWN")
            self.assertEqual(len(attempted), 1)
            os.fstat(attempted[0])  # The injected failure left this fake-test fd open.
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            for fd in attempted:
                original_close(fd)

    def test_fdopen_failure_after_internal_close_cannot_prove_close_outcome(self):
        original_close = os.close
        descriptors = []

        def fail_after_closing(fd, *args, **kwargs):
            descriptors.append(fd)
            original_close(fd)
            raise OSError("synthetic-fdopen-secret")

        with patch("core.commands.os.fdopen", side_effect=fail_after_closing), \
             patch("core.commands.os.close", wraps=original_close) as close:
            with self.assertRaises(SidecarWriteError) as caught:
                _atomic_write_json(self.path, {})
        self.assertEqual(caught.exception.state, "UNKNOWN")
        close.assert_called_once_with(descriptors[0])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_close_failure_is_unknown_without_closing_transferred_fd_again(self):
        original_fdopen = os.fdopen
        original_close = os.close
        wrappers = []
        raw_closes = []

        def fdopen(fd, *args, **kwargs):
            wrapper = _FileCloseSpy(original_fdopen(fd, *args, **kwargs), True)
            wrappers.append(wrapper)
            return wrapper

        def close(fd):
            raw_closes.append(fd)
            original_close(fd)

        with patch("core.commands.os.fdopen", side_effect=fdopen), \
             patch("core.commands.os.close", side_effect=close), \
             patch("core.commands.os.replace") as replace:
            with self.assertRaises(SidecarWriteError) as caught:
                _atomic_write_json(self.path, {})
        self.assertEqual(caught.exception.state, "UNKNOWN")
        self.assertEqual(len(wrappers), 1)
        self.assertEqual(wrappers[0].close_calls, 1)
        self.assertTrue(wrappers[0].file.closed)
        self.assertEqual(raw_closes, [])
        self.assertEqual(list(self.root.iterdir()), [])
        replace.assert_not_called()
        formatted = "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__))
        self.assertNotIn("synthetic-close-secret", formatted)

    def test_write_failure_classifies_close_and_cleanup_outcomes(self):
        for close_failure, expected in ((False, "NOT_REPLACED"), (True, "UNKNOWN")):
            with self.subTest(close_failure=close_failure):
                original_fdopen = os.fdopen
                wrappers = []

                def fdopen(fd, *args, **kwargs):
                    wrapper = _FileCloseSpy(original_fdopen(fd, *args, **kwargs), close_failure)
                    wrappers.append(wrapper)
                    return wrapper

                with patch("core.commands.os.fdopen", side_effect=fdopen), \
                     patch("core.commands.json.dump", side_effect=ValueError("synthetic-write-secret")), \
                     patch("core.commands.os.replace") as replace:
                    with self.assertRaises(SidecarWriteError) as caught:
                        _atomic_write_json(self.path, {})
                self.assertEqual(caught.exception.state, expected)
                self.assertEqual(wrappers[0].close_calls, 1)
                self.assertTrue(wrappers[0].file.closed)
                self.assertEqual(list(self.root.iterdir()), [])
                replace.assert_not_called()

    def test_fdopen_failure_and_cleanup_failure_is_unknown(self):
        original_unlink = os.unlink
        removed = []

        def fail_cleanup(path, *args, **kwargs):
            removed.append(path)
            original_unlink(path, *args, **kwargs)
            raise OSError("synthetic-cleanup-secret")

        with patch("core.commands.os.fdopen", side_effect=OSError("synthetic-fdopen-secret")), \
             patch("core.commands.os.unlink", side_effect=fail_cleanup):
            with self.assertRaises(SidecarWriteError) as caught:
                _atomic_write_json(self.path, {})
        self.assertEqual(caught.exception.state, "UNKNOWN")
        self.assertEqual(len(removed), 1)
        self.assertEqual(list(self.root.iterdir()), [])
        formatted = "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__))
        self.assertNotIn("synthetic-cleanup-secret", formatted)
        self.assertNotIn("synthetic-fdopen-secret", formatted)

    def test_resource_failures_hide_original_traceback_from_direct_caller(self):
        secret = "synthetic-resource-secret"
        original_fdopen, original_close, original_unlink = os.fdopen, os.close, os.unlink

        def close_then_fail(fd):
            original_close(fd)
            raise OSError(secret)

        def unlink_then_fail(path):
            original_unlink(path)
            raise OSError(secret)

        def open_with_failing_close(fd, *args, **kwargs):
            return _FileCloseSpy(original_fdopen(fd, *args, **kwargs), True)

        for mode in ("fdopen", "fd_close", "file_close", "cleanup"):
            with self.subTest(mode=mode):
                patches = []
                if mode == "file_close":
                    patches.append(patch("core.commands.os.fdopen", side_effect=open_with_failing_close))
                else:
                    patches.append(patch("core.commands.os.fdopen", side_effect=OSError(secret)))
                if mode == "fd_close":
                    patches.append(patch("core.commands.os.close", side_effect=close_then_fail))
                if mode == "cleanup":
                    patches.append(patch("core.commands.os.unlink", side_effect=unlink_then_fail))
                for p in patches:
                    p.start()
                try:
                    try:
                        _atomic_write_json(self.path, {})
                    except SidecarWriteError as exc:
                        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    else:
                        self.fail("expected SidecarWriteError")
                finally:
                    for p in reversed(patches):
                        p.stop()
                self.assertNotIn(secret, formatted)
                self.assertNotIn("synthetic-close-secret", formatted)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_legacy_unknown_explicitly_blocks_recovery_and_new_writes(self):
        with self.assertLogs("core.commands", level="WARNING") as startup:
            store = SessionOverridesSidecar(self.path)
        self.assertIn("compatibility", " ".join(startup.output))
        self.assertIn("adapter", " ".join(startup.output))
        store.apply_command("fake-token", "fake-session", "set", "B")
        original_replace = os.replace

        def replace_then_raise(src, dst):
            original_replace(src, dst)
            raise OSError("synthetic-post-replace-secret")

        ctx = CommandContext("C", "fake-token", "fake-session",
                             {"route_pool": [{"route_id": "B", "weight": 1}]},
                             {"B": {"id": "B"}, "C": {"id": "C"}}, store, "B")
        with patch("core.commands.os.replace", side_effect=replace_then_raise):
            result = handle_route_command(ctx)
        self.assertFalse(result.wrote)
        self.assertIn("无法", result.receipt_text)
        self.assertIn("适配器", result.receipt_text)
        self.assertIn("暂停", result.receipt_text)
        self.assertNotIn("fake-token", result.receipt_text)
        self.assertEqual(json.loads(self.path.read_bytes())["fake-token"]["fake-session"]["route_id"], "C")
        store.touch("fake-token", "fake-session")
        touched = store._mem_last_seen["fake-token", "fake-session"]
        with self.assertLogs("core.commands", level="WARNING") as warning, \
             patch("builtins.open", side_effect=AssertionError("unexpected unprotected read")), \
             patch("core.commands.os.replace") as replace:
            store.maybe_reload()
            self.assertFalse(store.recover())
            second = handle_route_command(ctx)
            replace.assert_not_called()
        self.assertIn("UNKNOWN", " ".join(warning.output))
        self.assertIn("adapter", " ".join(warning.output))
        self.assertIn("适配器", second.receipt_text)
        self.assertFalse(second.wrote)
        self.assertEqual(store.get_overrides_for("fake-token"), {"fake-session": "B"})
        self.assertEqual(store._mem_last_seen["fake-token", "fake-session"], touched)


if __name__ == "__main__":
    unittest.main()
