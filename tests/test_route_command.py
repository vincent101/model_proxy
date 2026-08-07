"""$route in-band 命令层 + sidecar 单测（脱网络，纯标准库 unittest）。

对应设计文档 docs/designs/2026-08-04-in-band-route-command-design.md §8 验证项：
- V5  写路径无别名污染（deepcopy 生效，ConfigStore._config 不被就地改动）
- V9  fail-open 反例集（已在 test_command_match_rules.py 覆盖匹配规则本身，这里
      补覆盖端到端场景：非命令消息一律照常转发，不进入命令层）
- V10 旧式纯字符串 override 不被清理逻辑误删（人工手改 sidecar 纯字符串不被清理）
- V11 清理判据正确（6 天/8 天边界、当前 session 永不清理）
- V12 清理与变更同一次原子写（一次 $route 只产生一次 os.replace）
- V13 last_seen 不写热路径（命中 override 的普通请求只改内存，不触发写盘 IO）

运行：cd tools/model_proxy && python3 -m unittest tests.test_route_command -v
"""

import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.commands import (  # noqa: E402
    CommandContext,
    SessionOverridesSidecar,
    extract_last_user_message_content,
    handle_route_command,
    normalize_override_entry,
)
from core.server import (  # noqa: E402
    ConfigStore,
    CooldownStore,
    ModelProxyHandler,
    SyntaxPreferenceStore,
)


def _routes_map():
    return {
        "claude": {"id": "claude", "tiers": {"opus": ["claude-opus-k0"]}},
        "nation": {"id": "nation", "tiers": {"opus": ["nation-opus-k0"]}},
        "deepseek": {"id": "deepseek", "tiers": {"opus": ["deepseek-opus-k0"]}},
    }


def _make_handler():
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.wfile = io.BytesIO()
    h._acc = {"status": 0, "route": "", "builtin": "", "supply": ""}
    h.send_response = lambda status: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    return h


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 匹配/提取辅助
# ---------------------------------------------------------------------------

class TestExtractLastUserMessageContent(unittest.TestCase):

    def test_only_last_user_message_considered(self):
        body = {
            "messages": [
                {"role": "user", "content": "$route nation"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "hello"},
            ]
        }
        self.assertEqual(extract_last_user_message_content(body), "hello")

    def test_no_messages_key(self):
        self.assertIsNone(extract_last_user_message_content({}))

    def test_messages_not_list(self):
        self.assertIsNone(extract_last_user_message_content({"messages": "x"}))

    def test_no_user_role(self):
        body = {"messages": [{"role": "assistant", "content": "x"}]}
        self.assertIsNone(extract_last_user_message_content(body))


class TestNormalizeOverrideEntry(unittest.TestCase):

    def test_legacy_string(self):
        self.assertEqual(normalize_override_entry("nation"), "nation")

    def test_new_dict(self):
        self.assertEqual(
            normalize_override_entry({"route_id": "nation", "last_seen": "x"}), "nation")

    def test_empty_string(self):
        self.assertIsNone(normalize_override_entry(""))

    def test_dict_without_route_id(self):
        self.assertIsNone(normalize_override_entry({"last_seen": "x"}))

    def test_other_type(self):
        self.assertIsNone(normalize_override_entry(123))
        self.assertIsNone(normalize_override_entry(None))


# ---------------------------------------------------------------------------
# sidecar 基础读写
# ---------------------------------------------------------------------------

class TestSidecarBasics(unittest.TestCase):

    def test_missing_file_is_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            sc = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            self.assertEqual(sc.get_overrides_for("cc"), {})

    def test_corrupt_json_keeps_last_known_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sc = SessionOverridesSidecar(path)
            sc.apply_command("cc", "sess-1", "set", target_route_id="nation")
            self.assertEqual(sc.get_overrides_for("cc"), {"sess-1": "nation"})

            # 手工写入非法 JSON，模拟外部损坏
            path.write_text("{not valid json", encoding="utf-8")
            sc.maybe_reload()
            # 保留上一次成功加载的内存值，不中断
            self.assertEqual(sc.get_overrides_for("cc"), {"sess-1": "nation"})

    def test_mtime_reload_picks_up_external_valid_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sc = SessionOverridesSidecar(path)
            self.assertEqual(sc.get_overrides_for("cc"), {})
            # 外部（模拟人工）写入合法内容
            path.write_text(json.dumps({"cc": {"sess-x": {"route_id": "nation"}}}), encoding="utf-8")
            os.utime(path, (time.time() + 5, time.time() + 5))
            sc.maybe_reload()
            self.assertEqual(sc.get_overrides_for("cc"), {"sess-x": "nation"})


# ---------------------------------------------------------------------------
# 回归：apply_command 不得在外部改动 sidecar mtime 后死锁（bug 报告见
# docs/designs/2026-08-04-in-band-route-command-design.md 相关 review：
# apply_command 持锁期间调用 maybe_reload，maybe_reload 判定需要重载时会再次
# 获取同一把非可重入 Lock，导致永久阻塞）。
# ---------------------------------------------------------------------------

class TestApplyCommandNoDeadlockOnExternalMtimeBump(unittest.TestCase):

    def test_apply_command_after_external_mtime_bump_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sc = SessionOverridesSidecar(path)
            sc.apply_command("cc", "sess-1", "set", target_route_id="nation")

            # 外部（模拟人工手改）改动 sidecar 文件内容并推进 mtime，制造
            # apply_command 内部 maybe_reload 判定"需要重载"的条件。
            path.write_text(
                json.dumps({"cc": {"sess-1": {"route_id": "nation"}, "sess-x": {"route_id": "claude"}}}),
                encoding="utf-8",
            )
            os.utime(path, (time.time() + 5, time.time() + 5))

            result_holder: dict = {}
            error_holder: dict = {}

            def _run():
                try:
                    result_holder["result"] = sc.apply_command(
                        "cc", "sess-1", "set", target_route_id="deepseek")
                except Exception as e:  # noqa: BLE001
                    error_holder["error"] = e

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=5)

            self.assertFalse(
                t.is_alive(),
                "apply_command 在外部改动 sidecar mtime 后死锁：线程 5 秒内未返回"
                "（maybe_reload 在已持锁状态下被 apply_command 再次调用，非可重入"
                "Lock 导致永久阻塞）",
            )
            if "error" in error_holder:
                raise error_holder["error"]
            self.assertIn("result", result_holder)
            self.assertEqual(
                sc.get_overrides_for("cc"),
                {"sess-1": "deepseek", "sess-x": "claude"},
            )


# ---------------------------------------------------------------------------
# V12：一次 $route 只产生一次 os.replace（清理与变更同一次原子写）
# ---------------------------------------------------------------------------

class TestSingleAtomicWrite(unittest.TestCase):

    def test_apply_command_triggers_exactly_one_replace(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sc = SessionOverridesSidecar(path)
            with patch("os.replace") as mock_replace:
                sc.apply_command("cc", "sess-1", "set", target_route_id="nation")
                self.assertEqual(mock_replace.call_count, 1)

    def test_reset_triggers_exactly_one_replace(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sc = SessionOverridesSidecar(path)
            sc.apply_command("cc", "sess-1", "set", target_route_id="nation")
            with patch("os.replace") as mock_replace:
                sc.apply_command("cc", "sess-1", "reset")
                self.assertEqual(mock_replace.call_count, 1)


# ---------------------------------------------------------------------------
# V11：清理判据正确性
# ---------------------------------------------------------------------------

class TestCleanupThreshold(unittest.TestCase):

    def _seed(self, path, entries):
        """entries: {client_token: {session_id: last_seen_iso_or_None}}"""
        data = {}
        for ct, sessions in entries.items():
            data[ct] = {}
            for sid, ls in sessions.items():
                data[ct][sid] = {"route_id": "nation", "last_seen": ls, "created": ls}
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_only_expired_beyond_7_days_removed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            now = datetime.now(timezone.utc)
            six_days_ago = _iso(now - timedelta(days=6))
            eight_days_ago = _iso(now - timedelta(days=8))
            self._seed(path, {
                "cc": {
                    "sess-6d": six_days_ago,
                    "sess-8d": eight_days_ago,
                }
            })
            sc = SessionOverridesSidecar(path)
            # 用一个无关的当前 session 触发写操作（不是 sess-6d/sess-8d 自己）
            result = sc.apply_command("cc", "sess-current", "set", target_route_id="claude")
            cleaned_sids = {sid for _ct, sid in result["cleaned"]}
            self.assertIn("sess-8d", cleaned_sids)
            self.assertNotIn("sess-6d", cleaned_sids)
            remaining = sc.get_overrides_for("cc")
            self.assertIn("sess-6d", remaining)
            self.assertNotIn("sess-8d", remaining)

    def test_current_session_never_cleaned_even_if_expired(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            now = datetime.now(timezone.utc)
            ten_days_ago = _iso(now - timedelta(days=10))
            self._seed(path, {"cc": {"sess-old": ten_days_ago}})
            sc = SessionOverridesSidecar(path)
            # 当前操作的 session 恰好就是这个已过期的 sess-old（例如它自己重新发了 $route）
            sc.apply_command("cc", "sess-old", "set", target_route_id="claude")
            remaining = sc.get_overrides_for("cc")
            self.assertIn("sess-old", remaining)
            self.assertEqual(remaining["sess-old"], "claude")

    def test_no_last_seen_never_cleaned(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            # 手工塞入 sidecar 的旧式字符串条目（无 last_seen）
            path.write_text(json.dumps({"cc": {"sess-legacy": "nation"}}), encoding="utf-8")
            sc = SessionOverridesSidecar(path)
            result = sc.apply_command("cc", "sess-current", "set", target_route_id="claude")
            cleaned_sids = {sid for _ct, sid in result["cleaned"]}
            self.assertNotIn("sess-legacy", cleaned_sids)
            self.assertIn("sess-legacy", sc.get_overrides_for("cc"))


# ---------------------------------------------------------------------------
# V5：写路径无别名污染（deepcopy 生效，ConfigStore._config 不被就地改动）
# ---------------------------------------------------------------------------

class TestNoAliasPollution(unittest.TestCase):

    def test_config_store_internal_dict_untouched_after_switch(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "model_proxy_config.json"
            cfg = {
                "admin_token": "x",
                "supplies": [],
                "routes": [{"id": "claude", "tiers": {}}, {"id": "nation", "tiers": {}}],
                "strategies": [{
                    "client_token": "cc",
                    "route_pool": [{"route_id": "claude", "weight": 1}],
                }],
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            cs = ConfigStore(cfg_path)
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")

            # 拍一份深拷贝快照，作为"改动前"基线比对（避免依赖 cs 私有属性名之外的假设）
            before_strategies = copy.deepcopy(cs.get_strategies())

            strategy = cs.get_strategies()[0]
            routes_map = cs.get_routes_map()

            # 模拟 server.py 的读路径：读 sidecar 构造浅拷贝视图（不得改动 strategy 本身）
            overrides = sidecar.get_overrides_for(strategy.get("client_token", ""))
            view = dict(strategy)
            view_dispatch = dict(strategy.get("dispatch") or {})
            view_dispatch["session_overrides"] = overrides
            view["dispatch"] = view_dispatch
            self.assertIsNot(view, strategy)
            self.assertIsNot(view.get("dispatch"), strategy.get("dispatch"))

            # 模拟写路径：$route 切换
            ctx = CommandContext(
                arg="nation", client_token="cc", session_key="sess-b",
                strategy=strategy, routes_map=routes_map, sidecar=sidecar,
                resolved_route_id="nation",
            )
            handle_route_command(ctx)

            # ConfigStore 内部配置必须与写操作前完全一致（未被就地改动）
            after_strategies = cs.get_strategies()
            self.assertEqual(before_strategies, after_strategies)


# ---------------------------------------------------------------------------
# V13：last_seen 不写热路径（命中 override 的普通请求只改内存，不触发写盘 IO）
# ---------------------------------------------------------------------------

class TestHotPathNoDiskIO(unittest.TestCase):

    def test_touch_does_not_write_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sc = SessionOverridesSidecar(path)
            self.assertFalse(path.exists())
            with patch("os.replace") as mock_replace, \
                 patch("tempfile.mkstemp") as mock_mkstemp:
                for _ in range(50):
                    sc.touch("cc", "sess-hot")
                mock_replace.assert_not_called()
                mock_mkstemp.assert_not_called()
            # 命中期间文件仍不存在（纯内存记账）
            self.assertFalse(path.exists())


# ---------------------------------------------------------------------------
# V9（端到端补充）：非命令消息一律照常转发，不进入命令层
# ---------------------------------------------------------------------------

class TestFailOpenEndToEnd(unittest.TestCase):

    def test_non_command_message_is_not_intercepted(self):
        body = {"messages": [{"role": "user", "content": "帮我看看这段代码"}]}
        content = extract_last_user_message_content(body)
        from core.commands import parse_route_command
        is_cmd, _ = parse_route_command(content)
        self.assertFalse(is_cmd)

    def test_multiline_containing_command_is_not_intercepted(self):
        body = {"messages": [{"role": "user", "content": "帮我看下\n$route nation\n这个"}]}
        content = extract_last_user_message_content(body)
        from core.commands import parse_route_command
        is_cmd, _ = parse_route_command(content)
        self.assertFalse(is_cmd)


# ---------------------------------------------------------------------------
# 命令层骨架：切换/查询/reset 的基本行为 + 合法性校验 + sidecar 读取
# ---------------------------------------------------------------------------

class TestRouteCommandHandler(unittest.TestCase):

    def _strategy(self):
        return {
            "client_token": "cc",
            "route_pool": [{"route_id": "claude", "weight": 1}],
        }

    def test_legacy_route_id_strategy_rejects_switch_no_write(self):
        """旧式单值 route_id（无 route_pool）：extract_route_candidates 不读取
        session_overrides，写入 override 不会产生任何效果，必须拒绝而非假成功。
        """
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            legacy_strategy = {"client_token": "cc", "route_id": "claude"}
            ctx = CommandContext(
                arg="nation", client_token="cc", session_key="sess-x",
                strategy=legacy_strategy, routes_map=_routes_map(), sidecar=sidecar,
            )
            result = handle_route_command(ctx)
            self.assertFalse(result.wrote)
            self.assertEqual(sidecar.get_overrides_for("cc"), {})

    def test_legacy_route_id_strategy_rejects_reset_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            legacy_strategy = {"client_token": "cc", "route_id": "claude"}
            ctx = CommandContext(
                arg="reset", client_token="cc", session_key="sess-x",
                strategy=legacy_strategy, routes_map=_routes_map(), sidecar=sidecar,
            )
            result = handle_route_command(ctx)
            self.assertFalse(result.wrote)

    def test_switch_to_nonexistent_route_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            ctx = CommandContext(
                arg="not-a-route", client_token="cc", session_key="sess-x",
                strategy=self._strategy(), routes_map=_routes_map(), sidecar=sidecar,
            )
            result = handle_route_command(ctx)
            self.assertFalse(result.wrote)
            self.assertIn("不存在", result.receipt_text)
            self.assertEqual(sidecar.get_overrides_for("cc"), {})

    def test_switch_allows_target_outside_route_pool(self):
        """§5.3：允许切到 route_pool 之外的 route（现网 5 条全部指向 pool 外的 nation）。"""
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            ctx = CommandContext(
                arg="deepseek", client_token="cc", session_key="sess-x",
                strategy=self._strategy(), routes_map=_routes_map(), sidecar=sidecar,
            )
            result = handle_route_command(ctx)
            self.assertTrue(result.wrote)
            self.assertEqual(sidecar.get_overrides_for("cc")["sess-x"], "deepseek")

    def test_reset_removes_sidecar_entry(self):
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            sidecar.apply_command("cc", "sess-x", "set", target_route_id="deepseek")
            ctx = CommandContext(
                arg="reset", client_token="cc", session_key="sess-x",
                strategy=self._strategy(), routes_map=_routes_map(), sidecar=sidecar,
            )
            result = handle_route_command(ctx)
            self.assertTrue(result.wrote)
            self.assertNotIn("sess-x", sidecar.get_overrides_for("cc"))

    def test_query_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session_overrides.json"
            sidecar = SessionOverridesSidecar(path)
            ctx = CommandContext(
                arg=None, client_token="cc", session_key="sess-legacy",
                strategy=self._strategy(), routes_map=_routes_map(), sidecar=sidecar,
                resolved_route_id="claude",
            )
            result = handle_route_command(ctx)
            self.assertFalse(result.wrote)
            self.assertFalse(path.exists())  # 纯读零副作用，不触发清理/落盘

    def test_query_on_legacy_route_id_strategy_does_not_show_misleading_route(self):
        """回归：strategy 无 route_pool（旧式单值 route_id）时，即便 sidecar
        里有该 session 的残留 override 记录，查询也不能展示为"生效
        route"——extract_route_candidates 对这种 strategy 完全不读取
        session_overrides，真实生效路由永远是 strategy["route_id"]。
        """
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            sidecar.apply_command("cc", "sess-x", "set", target_route_id="nation")
            legacy_strategy = {"client_token": "cc", "route_id": "claude"}
            ctx = CommandContext(
                arg=None, client_token="cc", session_key="sess-x",
                strategy=legacy_strategy, routes_map=_routes_map(), sidecar=sidecar,
                resolved_route_id="claude",
            )
            result = handle_route_command(ctx)
            self.assertFalse(result.wrote)
            # 不能展示 "生效 route: nation"（实际转发固定打到 claude）
            self.assertNotIn("生效 route: nation", result.receipt_text)
            self.assertIn("route_pool", result.receipt_text)
            self.assertIn("claude", result.receipt_text)
            self.assertIn("不生效", result.receipt_text)

    def test_query_total_overrides_counts_sidecar_only(self):
        """sidecar 是唯一来源，总条数即 sidecar 条数。"""
        with tempfile.TemporaryDirectory() as d:
            sidecar = SessionOverridesSidecar(Path(d) / "session_overrides.json")
            sidecar.apply_command("cc", "sess-legacy", "set", target_route_id="deepseek")
            sidecar.apply_command("cc", "sess-other", "set", target_route_id="nation")
            ctx = CommandContext(
                arg=None, client_token="cc", session_key="sess-legacy",
                strategy=self._strategy(), routes_map=_routes_map(), sidecar=sidecar,
                resolved_route_id="claude",
            )
            result = handle_route_command(ctx)
            self.assertIn("总条数: 2", result.receipt_text)


# ---------------------------------------------------------------------------
# 端到端：经真实 ModelProxyHandler._forward 拦截点（不打网络，纯本地）
# ---------------------------------------------------------------------------

class _FakeServer:
    pass


def _make_forward_handler(cfg_path, sidecar_path):
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.send_response = lambda status: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    fs = _FakeServer()
    fs.config_store = ConfigStore(cfg_path)
    fs.cooldown_store = CooldownStore()
    fs.pref_store = SyntaxPreferenceStore()
    fs.sidecar_store = SessionOverridesSidecar(sidecar_path)
    h.server = fs
    return h


def _send(h, text, session_id, stream=False):
    body = json.dumps({
        "model": "claude-sonnet",
        "stream": stream,
        "metadata": {"user_id": json.dumps({"session_id": session_id})},
        "messages": [{"role": "user", "content": text}],
    }).encode()
    h.wfile = io.BytesIO()
    h.headers = {"Content-Length": str(len(body)), "Authorization": "Bearer cc"}
    h.rfile = io.BytesIO(body)
    h.path = "/v1/messages"
    h._acc = {
        "status": 0, "source": "", "route": "", "tier": "", "supply": "",
        "failover": 0, "attempts": 0, "token": "", "usage_in": 0, "usage_out": 0,
        "strategy": "", "session": "", "route_failover": 0, "builtin": "",
    }
    h._forward("POST")
    return h._acc, h.wfile.getvalue()


class TestForwardInterceptEndToEnd(unittest.TestCase):

    def _write_cfg(self, d):
        cfg_path = Path(d) / "model_proxy_config.json"
        cfg = {
            "admin_token": "x",
            "supplies": [],
            "routes": [{"id": "claude", "tiers": {}}, {"id": "nation", "tiers": {}}],
            "strategies": [{
                "client_token": "cc",
                "route_pool": [{"route_id": "claude", "weight": 1}],
            }],
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg_path

    def test_switch_query_reset_query_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._write_cfg(d)
            sidecar_path = Path(d) / "session_overrides.json"
            h = _make_forward_handler(cfg_path, sidecar_path)
            sid = "sess-e2e-1"

            acc, _ = _send(h, "$route nation", sid)
            self.assertEqual(acc["status"], 200)
            self.assertEqual(acc["builtin"], "route")
            self.assertEqual(acc["route"], "nation")
            self.assertEqual(acc["supply"], "(builtin)")

            acc, _ = _send(h, "$route", sid)
            self.assertEqual(acc["route"], "nation")

            acc, _ = _send(h, "$route reset", sid)
            self.assertEqual(acc["route"], "claude")  # 落回 route_pool 首项

            acc, _ = _send(h, "$route", sid)
            self.assertEqual(acc["route"], "claude")

            self.assertEqual(h.server.sidecar_store.get_overrides_for("cc"), {})

    def test_stream_response_event_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._write_cfg(d)
            sidecar_path = Path(d) / "session_overrides.json"
            h = _make_forward_handler(cfg_path, sidecar_path)
            acc, raw = _send(h, "$route nation", "sess-e2e-stream", stream=True)
            self.assertEqual(acc["status"], 200)
            text = raw.decode("utf-8", "replace")
            for etype in ("message_start", "ping", "content_block_start",
                          "content_block_delta", "content_block_stop",
                          "message_delta", "message_stop"):
                self.assertIn(f"event: {etype}", text)
            self.assertTrue(raw.endswith(b"0\r\n\r\n"))

    def test_non_command_message_forwards_normally_and_gets_401_without_supply(self):
        """fail-open 端到端：非指令消息不进入命令层，走原有转发逻辑
        （本例因 supplies 为空，最终应表现为原有的路由/供给错误，而不是命令回执）。
        """
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._write_cfg(d)
            sidecar_path = Path(d) / "session_overrides.json"
            h = _make_forward_handler(cfg_path, sidecar_path)
            acc, _ = _send(h, "帮我看看这段代码", "sess-e2e-normal")
            self.assertEqual(acc["builtin"], "")
            self.assertNotEqual(acc["status"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
