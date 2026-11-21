"""Reload semantics with fake files and direct in-memory handlers; no sockets."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config_snapshot import ConfigSnapshotError
from core.server import ConfigReloadCallbackError, ConfigStore, ModelProxyHandler


def fake_config(generation="A", *, admin="FAKE_ADMIN", supplies=None):
    return {"admin_token": admin, "supplies": supplies or [], "routes": [],
            "strategies": [], "generation": generation}


def invalid_supply():
    return {"id": "fake", "protocol": "anthropic", "url": "http://fake.invalid/v1/messages",
            "appkey": "FAKE_SECRET", "appkey_file": "fake-key-file"}


class FakeCooldown:
    def __init__(self, *, fail_clear=False, fail_snapshot=False):
        self.entries = {"fake": 1}
        self.fail_clear = fail_clear
        self.fail_snapshot = fail_snapshot
        self.clear_calls = 0

    def snapshot(self):
        if self.fail_snapshot:
            raise RuntimeError("FAKE_SNAPSHOT_SECRET")
        return dict(self.entries)

    def clear_all(self):
        self.clear_calls += 1
        self.entries.clear()
        if self.fail_clear:
            raise RuntimeError("FAKE_CLEAR_SECRET")


class ReloadContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "fake-config.json"
        self.write(fake_config())

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        self.path.chmod(0o600)

    def atomic_write(self, value, *, mtime_ns=None):
        other = self.root / "fake-replacement.json"
        other.write_text(json.dumps(value), encoding="utf-8")
        other.chmod(0o600)
        if mtime_ns is not None:
            os.utime(other, ns=(mtime_ns, mtime_ns))
        os.replace(other, self.path)

    @staticmethod
    def handler():
        handler = ModelProxyHandler.__new__(ModelProxyHandler)
        sent = []
        handler._send_json = lambda status, body: sent.append((status, body))
        return handler, sent

    def test_startup_supply_validation_remains_fail_fast(self):
        self.write(fake_config(supplies=[invalid_supply()]))
        with self.assertRaises(ValueError):
            ConfigStore(self.path)

    def test_hot_reload_invalid_supply_is_rejected_and_keeps_old(self):
        """2026-11-22 语义变更：热重载校验失败即拒绝，不再「告警但提交」。

        此前一份启动期会被 fail-fast 拒掉的配置能成功热重载并生效（实测导致
        catpaw-ide 全量 500）。现在必须保留旧快照并返回 config_rejected。
        """
        store = ConfigStore(self.path)
        old_config = store._config
        marker = store._probe_marker
        self.write(fake_config("B", supplies=[invalid_supply()]))
        with self.assertLogs("core.server", level="WARNING"):
            self.assertIs(store.reload(), False)
        self.assertIs(store._config, old_config)
        self.assertEqual(store._config["generation"], "A")
        self.assertEqual(store._probe_marker, marker)
        self.assertEqual(store.get_supply_map(), {})

    def test_rejected_snapshot_is_not_reparsed_on_every_request(self):
        """被拒文件未变时热路径不重复读盘，也不推进生效配置标记。"""
        store = ConfigStore(self.path)
        old_config, marker = store._config, store._probe_marker
        self.write(fake_config("B", supplies=[invalid_supply()]))
        self.assertFalse(store.maybe_reload())
        self.assertIsNotNone(store._rejected_marker)
        calls = []
        import core.server as server_module
        original = server_module._read_business_config
        with patch("core.server._read_business_config",
                   side_effect=lambda path: calls.append(path) or original(path)):
            self.assertFalse(store.maybe_reload())
        self.assertEqual(calls, [], "同一份被拒文件不该再读一次")
        self.assertIs(store._config, old_config)
        self.assertEqual(store._probe_marker, marker)

    def test_fixing_rejected_file_takes_effect(self):
        """用户改好文件后（marker 变化）热路径自动重新加载。"""
        store = ConfigStore(self.path)
        self.write(fake_config("B", supplies=[invalid_supply()]))
        self.assertFalse(store.maybe_reload())
        self.write(fake_config("C"))
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config["generation"], "C")
        self.assertIsNone(store._rejected_marker)

    def test_manual_reload_reports_config_rejected(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        self.write(fake_config("B", supplies=[invalid_supply()]))
        handler, sent = self.handler()
        with self.assertLogs("core.server", level="WARNING"):
            handler._handle_reload(store, cooldown)
        status, body = sent[-1]
        self.assertEqual(status, 422)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"], "config_rejected")
        self.assertIs(body["loaded"], False)
        self.assertEqual(store._config["generation"], "A")
        # 显式 reload 仍会清 cooldown（进程侧状态，与被拒配置无关）
        self.assertEqual(cooldown.clear_calls, 1)

    def test_reload_returns_bool_and_failure_preserves_this_attempt_snapshot(self):
        store = ConfigStore(self.path)
        old, marker, mtime = store._config, store._probe_marker, store._mtime
        self.path.write_text('{"secret":"FAKE_SECRET",', encoding="utf-8")
        self.assertIs(store.reload(), False)
        self.assertIs(store._config, old)
        self.assertEqual(store._probe_marker, marker)
        self.assertEqual(store._mtime, mtime)
        self.write(fake_config("B"))
        self.assertIs(store.reload(), True)
        self.assertEqual(store._config["generation"], "B")

    def test_non_object_and_bad_utf8_are_failed_loads(self):
        store = ConfigStore(self.path)
        old, marker = store._config, store._probe_marker
        for content in (b"[]", b"null", b"\xff"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertIs(store.reload(), False)
                self.assertIs(store._config, old)
                self.assertEqual(store._probe_marker, marker)

    def test_manual_parse_failure_clears_cooldown_and_reports_no_commit(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        marker = store._probe_marker
        self.path.write_text('{"secret":"FAKE_SECRET",', encoding="utf-8")
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        self.assertEqual(len(sent), 1)
        status, body = sent[0]
        self.assertEqual(status, 422)
        self.assertEqual(body, {"ok": False, "error": "config_parse_error", "loaded": False,
                                "cooldown_cleared": True})
        self.assertEqual(cooldown.clear_calls, 1)
        self.assertEqual(cooldown.entries, {})
        self.assertEqual(store._probe_marker, marker)
        self.assertEqual(store._config["generation"], "A")
        self.assertNotIn("FAKE_SECRET", json.dumps(body))
        self.assertNotIn(str(self.path), json.dumps(body))

    def test_manual_read_failure_clears_cooldown(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        handler, sent = self.handler()
        with patch("core.server._read_business_config", side_effect=ConfigSnapshotError("config_read_error")):
            handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "config_read_error")
        self.assertIs(body["loaded"], False)
        self.assertIs(body["cooldown_cleared"], True)
        self.assertEqual(cooldown.clear_calls, 1)
        self.assertEqual(store._config["generation"], "A")

    def test_manual_success_retains_legacy_200_shape(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        self.write(fake_config("B"))
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        self.assertEqual(sent, [(200, {"ok": True})])
        self.assertEqual(store._config["generation"], "B")
        self.assertEqual(cooldown.clear_calls, 1)

    def test_callback_failure_reports_loaded_true_and_still_clears_cooldown(self):
        def fail_callback():
            raise RuntimeError("FAKE_CALLBACK_SECRET")
        store, cooldown = ConfigStore(self.path, on_reload=fail_callback), FakeCooldown()
        self.write(fake_config("B"))
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "config_callback_error")
        self.assertIs(body["loaded"], True)
        self.assertIs(body["cooldown_cleared"], True)
        self.assertEqual(store._config["generation"], "B")
        self.assertNotIn("FAKE_CALLBACK_SECRET", json.dumps(body))

    def test_direct_callback_failure_distinguishes_post_commit(self):
        def fail_callback():
            raise RuntimeError("FAKE_CALLBACK_SECRET")
        store = ConfigStore(self.path, on_reload=fail_callback)
        self.write(fake_config("B"))
        with self.assertRaises(ConfigReloadCallbackError) as caught:
            store.reload()
        self.assertTrue(caught.exception.loaded)
        self.assertEqual(store._config["generation"], "B")
        self.assertNotIn("FAKE_CALLBACK_SECRET", str(caught.exception))

    def test_cooldown_failure_is_unknown_even_if_clear_started(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown(fail_clear=True)
        self.write(fake_config("B"))
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "cooldown_clear_failed")
        self.assertIs(body["loaded"], True)
        self.assertIsNone(body["cooldown_cleared"])
        self.assertEqual(store._config["generation"], "B")
        self.assertNotIn("FAKE_CLEAR_SECRET", json.dumps(body))

    def test_snapshot_count_failure_does_not_prevent_clear(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown(fail_snapshot=True)
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        self.assertEqual(sent, [(200, {"ok": True})])
        self.assertEqual(cooldown.clear_calls, 1)

    def test_unexpected_reload_exception_has_unknown_commit_and_attempts_clear(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        handler, sent = self.handler()
        with patch.object(store, "reload_detailed", side_effect=RuntimeError("FAKE_SECRET")):
            handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "config_reload_unknown")
        self.assertIsNone(body["loaded"])
        self.assertIs(body["cooldown_cleared"], True)
        self.assertNotIn("FAKE_SECRET", json.dumps(body))

    def test_auth_rejection_and_wrong_path_do_not_clear_cooldown(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        handler, sent = self.handler()
        handler.server = SimpleNamespace(config_store=store, cooldown_store=cooldown)
        handler.path = "/model_proxy/reload"
        handler.headers = {"X-Proxy-Admin-Token": "FAKE_WRONG"}
        handler._dispatch_control("POST")
        self.assertEqual(sent[0][0], 401)
        self.assertEqual(cooldown.clear_calls, 0)

        sent.clear()
        handler.path = "/model_proxy/not-reload"
        handler.headers = {"X-Proxy-Admin-Token": "FAKE_ADMIN"}
        handler._dispatch_control("POST")
        self.assertEqual(sent[0][0], 404)
        self.assertEqual(cooldown.clear_calls, 0)

        self.write(fake_config(admin=""))
        self.assertIs(store.reload(), True)
        sent.clear()
        handler.path = "/model_proxy/reload"
        handler._dispatch_control("POST")
        self.assertEqual(sent[0][0], 503)
        self.assertEqual(cooldown.clear_calls, 0)

    def test_admin_token_disk_rotation_requires_current_memory_token(self):
        """Document existing pre-auth semantics; never use real credentials or sockets."""
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        handler, sent = self.handler()
        handler.server = SimpleNamespace(config_store=store, cooldown_store=cooldown)
        handler.path = "/model_proxy/reload"
        self.atomic_write(fake_config("B", admin="FAKE_NEW_ADMIN"))

        handler.headers = {"X-Proxy-Admin-Token": "FAKE_NEW_ADMIN"}
        handler._dispatch_control("POST")
        self.assertEqual(sent[-1][0], 401)
        self.assertEqual(store.get_admin_token(), "FAKE_ADMIN")
        self.assertEqual(cooldown.clear_calls, 0)

        sent.clear()
        handler.headers = {"X-Proxy-Admin-Token": "FAKE_ADMIN"}
        handler._dispatch_control("POST")
        self.assertEqual(sent, [(200, {"ok": True})])
        self.assertEqual(store.get_admin_token(), "FAKE_NEW_ADMIN")
        self.assertEqual(cooldown.clear_calls, 1)

        sent.clear()
        handler.path = "/model_proxy/status"
        handler.headers = {"X-Proxy-Admin-Token": "FAKE_NEW_ADMIN"}
        handler._handle_status = lambda cs, cd: handler._send_json(200, {"ok": True})
        handler._dispatch_control("GET")
        self.assertEqual(sent, [(200, {"ok": True})])

    def test_automatic_reload_detects_atomic_equal_and_earlier_mtime(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        for index, delta in enumerate((0, -10_000_000_000), start=1):
            with self.subTest(delta=delta):
                old = store._probe_marker
                self.atomic_write(fake_config(f"B{index}"), mtime_ns=old.mtime_ns + delta)
                self.assertTrue(store.maybe_reload())
                self.assertEqual(store._config["generation"], f"B{index}")
                self.assertEqual(cooldown.clear_calls, 0)
                self.assertEqual(cooldown.entries, {"fake": 1})

    def test_failed_automatic_reload_does_not_advance_marker_and_retries(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        marker = store._probe_marker
        self.path.write_text("{", encoding="utf-8")
        self.assertFalse(store.maybe_reload())
        self.assertEqual(store._probe_marker, marker)
        self.assertEqual(store._config["generation"], "A")
        self.atomic_write(fake_config("B"), mtime_ns=marker.mtime_ns - 1_000_000_000)
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config["generation"], "B")
        self.assertEqual(cooldown.clear_calls, 0)

    def test_replace_after_last_check_keeps_read_marker_and_next_probe_loads_new_file(self):
        import core.server as server_module
        store = ConfigStore(self.path)
        self.atomic_write(fake_config("READ_A"))
        next_file = self.root / "fake-next.json"
        next_file.write_text(json.dumps(fake_config("NEXT_B")), encoding="utf-8")
        next_file.chmod(0o600)
        original = server_module._read_business_config

        def read_then_replace(path):
            snapshot = original(path)
            os.replace(next_file, self.path)
            return snapshot

        with patch("core.server._read_business_config", side_effect=read_then_replace):
            self.assertIs(store.reload(), True)
        self.assertEqual(store._config["generation"], "READ_A")
        self.assertTrue(store.maybe_reload())
        self.assertEqual(store._config["generation"], "NEXT_B")


    def test_competing_auto_probe_does_not_double_commit_or_clear_cooldown(self):
        import core.server as server_module
        callbacks = []
        store = ConfigStore(self.path, on_reload=lambda: callbacks.append('callback'))
        cooldown = FakeCooldown()
        handler, sent = self.handler()
        self.atomic_write(fake_config('B'))
        entered, release, probed = threading.Event(), threading.Event(), threading.Event()
        original_read, original_marker = server_module._read_business_config, server_module._business_marker
        results, errors = [], []
        def blocked_read(path):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test read release timeout')
            return original_read(path)
        def observed_marker(path):
            value = original_marker(path)
            probed.set()
            return value
        def manual():
            try:
                handler._handle_reload(store, cooldown)
            except BaseException as exc:
                errors.append(exc)
        def automatic():
            try:
                results.append(store.maybe_reload())
            except BaseException as exc:
                errors.append(exc)
        with patch.object(server_module, '_read_business_config', side_effect=blocked_read), \
             patch.object(server_module, '_business_marker', side_effect=observed_marker):
            m = threading.Thread(target=manual)
            a = threading.Thread(target=automatic)
            m.start()
            try:
                self.assertTrue(entered.wait(5))
                a.start()
                self.assertTrue(probed.wait(5))
            finally:
                release.set()
                m.join(5)
                if a.ident is not None:
                    a.join(5)
            self.assertFalse(m.is_alive())
            self.assertFalse(a.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False])
        self.assertEqual(callbacks, ['callback'])
        self.assertEqual(sent, [(200, {'ok': True})])
        self.assertEqual(cooldown.clear_calls, 1)
        self.assertEqual(store._config['generation'], 'B')

    def test_racing_failed_manual_reload_clears_once_and_does_not_commit(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        handler, sent = self.handler()
        old, marker = store._config, store._probe_marker
        self.atomic_write(fake_config('B'))
        with patch('core.server._read_business_config',
                   side_effect=ConfigSnapshotError('config_unstable')):
            handler._handle_reload(store, cooldown)
        self.assertEqual(sent[0][1]['error'], 'config_unstable')
        self.assertIs(sent[0][1]['loaded'], False)
        self.assertIs(sent[0][1]['cooldown_cleared'], True)
        self.assertEqual(cooldown.clear_calls, 1)
        self.assertIs(store._config, old)
        self.assertEqual(store._probe_marker, marker)
        self.assertTrue(store.maybe_reload())
        self.assertEqual(cooldown.clear_calls, 1)
        self.assertEqual(store._config['generation'], 'B')

    def test_parse_and_cooldown_failures_report_both_known_states(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown(fail_clear=True)
        self.path.write_text('{"secret":"FAKE_SECRET",', encoding="utf-8")
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "cooldown_clear_failed")
        self.assertEqual(body["load_error"], "config_parse_error")
        self.assertIs(body["loaded"], False)
        self.assertIsNone(body["cooldown_cleared"])
        self.assertEqual(store._config["generation"], "A")
        self.assertNotIn("FAKE_SECRET", json.dumps(body))

    def test_callback_and_cooldown_failures_report_committed_config(self):
        def fail_callback():
            raise RuntimeError("FAKE_SECRET")
        store = ConfigStore(self.path, on_reload=fail_callback)
        cooldown = FakeCooldown(fail_clear=True)
        self.write(fake_config("B"))
        handler, sent = self.handler()
        handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "cooldown_clear_failed")
        self.assertEqual(body["load_error"], "config_callback_error")
        self.assertIs(body["loaded"], True)
        self.assertIsNone(body["cooldown_cleared"])
        self.assertEqual(store._config["generation"], "B")


    def test_unverified_filesystem_never_reports_reload_success(self):
        store, cooldown = ConfigStore(self.path), FakeCooldown()
        old, marker = store._config, store._probe_marker
        handler, sent = self.handler()
        with patch("core.server._read_business_config",
                   side_effect=ConfigSnapshotError("filesystem_unverified")):
            handler._handle_reload(store, cooldown)
        status, body = sent[0]
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "filesystem_unverified")
        self.assertIs(body["loaded"], False)
        self.assertIs(body["cooldown_cleared"], True)
        self.assertEqual(cooldown.clear_calls, 1)
        self.assertIs(store._config, old)
        self.assertEqual(store._probe_marker, marker)


if __name__ == "__main__":
    unittest.main()
