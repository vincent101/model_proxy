"""正文错误识别（body error）与 failover/cooldown 衔接单测（脱网络）。

驱动 ModelProxyHandler._forward 全链路：patch core.server.urllib.request.urlopen
按队列返回假上游响应（参照 test_budget_retry 假上游模式）。

覆盖（设计记录 2026-10-10 §2/§4 + 复核修订中2/中3/轻4/轻5/轻7）：
- match_body_error 纯函数：contains/exact/prefix 三档、结构条件（tool/thinking）、
  长度短路（默认 200 / rule 级 max_text_len 覆盖）、空规则返回 None
- 非流式 PASSTHROUGH：命中 → 四连换源重发（cooldown/failover/attempt_errors/
  tried_set）+ resp.close()（轻4）；未配 code → 原样写回 resp_body（中3）；
  正常响应 / 无 body_error_rules 键零行为；全耗尽 503 err_summary 含 body_*
- 流式：SSE text 错误流 → 透传字节与无规则时逐字节一致 + cooldown 触发 +
  ACCESS body_error 字段；tool_use 流不触发；长正文不触发；
  客户端断开（判定挂 finally，中2）也触发判定
- 配置校验：非法 body_error rule 跳过 + warning；cooldown_rules errorcode 元素
  放开为任意非空字符串（中1：body_* 虚拟 code 合并式配置零 warning）

运行：cd tools/model_proxy && python3 -m pytest tests/test_body_error.py -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import (  # noqa: E402
    ConfigStore,
    ModelProxyHandler,
    match_body_error,
    _snapshot_unconfigured_hits,
    _unconfigured_hits,
)


# ---------------------------------------------------------------------------
# 假上游 / 假配置
# ---------------------------------------------------------------------------

class _FakeResp:
    """模拟 http.client 响应：read() 一次性返回全部字节（流式/非流式通用）。"""

    def __init__(self, payload, status=200):
        self.status = status
        self._data = payload if isinstance(payload, (bytes, bytearray)) \
            else json.dumps(payload).encode()
        self._read = False
        self.closed = False

    def read(self, n=-1):
        if self._read:
            return b""
        self._read = True
        return self._data

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def close(self):
        self.closed = True


class _ChunkedResp:
    """分块返回的流式假响应（客户端断开测试需要中途 chunk 边界）。"""

    def __init__(self, chunks, status=200):
        self.status = status
        self._chunks = list(chunks)
        self.closed = False

    def read(self, n=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def getheaders(self):
        return [("Content-Type", "text/event-stream")]

    def close(self):
        self.closed = True


_BODY_RULES = [
    {"pattern": "当前小时请求过于频繁", "match": "contains",
     "errorcode": "body_rate_limited"},
    {"pattern": "未命中任何路由策略", "match": "contains",
     "errorcode": "body_route_miss"},
]

_COOLDOWN_RULES = [
    {"errorcode": [429, "body_rate_limited"], "cooldown_seconds": 3600},
    {"errorcode": [401, 402, 403, "body_route_miss"], "cooldown_seconds": 21600},
]


class _FakeConfig:
    def __init__(self, supply_map, routes_map, strategies,
                 body_rules=None, cooldown_rules=None):
        self._supply_map = supply_map
        self._routes_map = routes_map
        self._strategies = strategies
        self._body_rules = body_rules if body_rules is not None else _BODY_RULES
        self._cooldown_rules = (cooldown_rules if cooldown_rules is not None
                                else _COOLDOWN_RULES)

    def maybe_reload(self):
        return False

    def get_strategies(self):
        return self._strategies

    def get_routes_map(self):
        return self._routes_map

    def get_supply_map(self):
        return self._supply_map

    def get_upstream_timeout(self):
        return 1800

    def get_budget_retry(self):
        return {"enabled": True, "max_retries": 5}

    def get_body_error_rules(self):
        return list(self._body_rules)

    def get_cooldown_rules(self):
        return list(self._cooldown_rules)


class _FakeCooldown:
    """记录 (supply_id, secs, reason)；is_cooling 真实生效以验证下一请求换源。"""

    def __init__(self):
        self.calls = []
        self._cooling = set()

    def is_cooling(self, sid):
        return sid in self._cooling

    def cooldown(self, sid, secs, reason=""):
        self.calls.append((sid, secs, reason))
        self._cooling.add(sid)

    def snapshot(self):
        return {}

    def clear_all(self):
        self._cooling.clear()


class _FakePref:
    def __init__(self):
        self._cache = {}

    def snapshot(self, model):
        return self._cache.get(model, {})

    def learn(self, model, variant):
        self._cache[model] = {"variant": variant}


class _FakeSidecar:
    def maybe_reload(self):
        return False

    def get_overrides_for(self, token):
        return {}

    def touch(self, token, session):
        pass


# ---------------------------------------------------------------------------
# 夹具：supply / 上游响应 / 客户端 body
# ---------------------------------------------------------------------------

_FULL_ENUM = {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}

_ERR_RATE = "当前小时请求过于频繁，请下个整点重试"
_ERR_ROUTE = "本次请求未命中任何路由策略，且当前场景未配置兜底模型。"


def _supply(sid, url="http://up/v1/messages"):
    return {"id": sid, "url": url, "protocol": "anthropic", "appkey": "k",
            "target_model": "m1", "reasoning_capability": dict(_FULL_ENUM)}


def _anth_body_error(text=_ERR_RATE):
    """200 + SSE 语义正常的 anthropic 错误响应（错误文案在 content[].text）。"""
    return {"id": "m1", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 5}}


def _anth_good():
    return {"id": "m2", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "正常回复"}],
            "usage": {"input_tokens": 3, "output_tokens": 100}}


def _anth_client_body(**over):
    body = {"model": "claude-opus", "max_tokens": 16000,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(over)
    return body


def _sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _anth_sse_text(text):
    """SSE 正常结构 + text 内容块携带 text（终态 valid，§1.5 主形态）。"""
    return ("".join([
        _sse_event("message_start",
                   {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _sse_event("content_block_start",
                   {"type": "content_block_start", "index": 0,
                    "content_block": {"type": "text", "text": ""}}),
        _sse_event("content_block_delta",
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text}}),
        _sse_event("content_block_stop",
                   {"type": "content_block_stop", "index": 0}),
        _sse_event("message_delta",
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 5}}),
        _sse_event("message_stop", {"type": "message_stop"}),
    ])).encode("utf-8")


def _anth_sse_tool_use(text=_ERR_RATE):
    """流内含 tool_use 块：结构条件 1 拦截，不判错误。"""
    return ("".join([
        _sse_event("message_start",
                   {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _sse_event("content_block_start",
                   {"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "t1", "name": "f",
                                      "input": {}}}),
        _sse_event("content_block_delta",
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text}}),
        _sse_event("content_block_stop",
                   {"type": "content_block_stop", "index": 0}),
        _sse_event("message_delta",
                   {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5}}),
        _sse_event("message_stop", {"type": "message_stop"}),
    ])).encode("utf-8")


# ---------------------------------------------------------------------------
# handler 驱动
# ---------------------------------------------------------------------------

def _make_server(supplies, tier_supplies, body_rules=None, cooldown_rules=None,
                 failover="on"):
    supply_map = {s["id"]: s for s in supplies}
    routes_map = {"r1": {"id": "r1", "tiers": {"opus": tier_supplies},
                         "failover": failover}}
    strategies = [{"client_token": "tok", "route_id": "r1"}]
    cd = _FakeCooldown()
    ns = SimpleNamespace(
        config_store=_FakeConfig(supply_map, routes_map, strategies,
                                 body_rules=body_rules,
                                 cooldown_rules=cooldown_rules),
        cooldown_store=cd, pref_store=_FakePref(), sidecar_store=_FakeSidecar())
    return ns, cd


def _make_handler(server_ns, body: dict, path="/v1/messages"):
    raw = json.dumps(body).encode()
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.server = server_ns
    h.path = path
    h.headers = {"Authorization": "Bearer tok", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h._acc = {
        "status": 0, "source": "", "route": "", "tier": "",
        "supply": "", "failover": 0, "attempts": 0, "token": "",
        "usage_in": 0, "usage_out": 0,
        "strategy": "", "session": "", "route_failover": 0,
        "builtin": "", "budget_retried": "", "budget_truncated": 0, "stop_reason": "",
        "final_error": "", "attempt_errors": [], "body_error": "",
    }
    h._responses = []

    def _write(status, headers, body_bytes):
        h._acc["status"] = status
        h._responses.append((status, body_bytes))

    h._write_buffered_response = _write
    h.send_response = lambda status: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    return h


def _run(h, upstream_queue):
    with patch("core.server.urllib.request.urlopen",
               side_effect=list(upstream_queue)) as m:
        h._forward("POST")
    return m


def _sent_urls(mock):
    return [c.args[0].full_url for c in mock.call_args_list]


# ---------------------------------------------------------------------------
# match_body_error 纯函数
# ---------------------------------------------------------------------------

class TestMatchBodyError(unittest.TestCase):

    def test_contains_hit(self):
        self.assertEqual(
            match_body_error(_ERR_RATE, _BODY_RULES,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)),
            "body_rate_limited")

    def test_contains_substring_hit(self):
        text = f"前置说明：{_ERR_RATE}"
        self.assertEqual(
            match_body_error(text, _BODY_RULES,
                             saw_tool_or_thinking=False, text_len=len(text)),
            "body_rate_limited")

    def test_exact_hit_and_miss(self):
        rules = [{"pattern": _ERR_RATE, "match": "exact",
                  "errorcode": "body_rate_limited"}]
        self.assertEqual(
            match_body_error(_ERR_RATE, rules,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)),
            "body_rate_limited")
        # 子串包含不等于 exact → 未命中
        text = f"prefix {_ERR_RATE}"
        self.assertIsNone(
            match_body_error(text, rules,
                             saw_tool_or_thinking=False, text_len=len(text)))

    def test_prefix_hit_and_miss(self):
        rules = [{"pattern": "当前小时", "match": "prefix",
                  "errorcode": "body_rate_limited"}]
        self.assertEqual(
            match_body_error(_ERR_RATE, rules,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)),
            "body_rate_limited")
        text = f"x{_ERR_RATE}"
        self.assertIsNone(
            match_body_error(text, rules,
                             saw_tool_or_thinking=False, text_len=len(text)))

    def test_match_defaults_to_contains(self):
        rules = [{"pattern": "过于频繁", "errorcode": "body_rate_limited"}]
        self.assertEqual(
            match_body_error(_ERR_RATE, rules,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)),
            "body_rate_limited")

    def test_tool_or_thinking_blocks_block_match(self):
        # 结构条件 1：出过 tool_use/thinking 即排除
        self.assertIsNone(
            match_body_error(_ERR_RATE, _BODY_RULES,
                             saw_tool_or_thinking=True, text_len=len(_ERR_RATE)))

    def test_length_short_circuit_default_200(self):
        # 结构条件 2：text 全文超 200 字符 → 不判（一次整数比较短路）
        text = _ERR_RATE + "x" * 300
        self.assertIsNone(
            match_body_error(text, _BODY_RULES,
                             saw_tool_or_thinking=False, text_len=len(text)))

    def test_rule_level_max_text_len_override(self):
        rules = [{"pattern": "过于频繁", "errorcode": "body_rate_limited",
                  "max_text_len": 400}]
        text = _ERR_RATE + "x" * 250
        self.assertEqual(
            match_body_error(text, rules,
                             saw_tool_or_thinking=False, text_len=len(text)),
            "body_rate_limited")
        rules_tight = [{"pattern": "过于频繁", "errorcode": "body_rate_limited",
                        "max_text_len": 10}]
        self.assertIsNone(
            match_body_error(text, rules_tight,
                             saw_tool_or_thinking=False, text_len=len(text)))

    def test_empty_rules_returns_none(self):
        self.assertIsNone(
            match_body_error(_ERR_RATE, [],
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)))
        self.assertIsNone(
            match_body_error(_ERR_RATE, None,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)))

    def test_first_matching_rule_wins(self):
        rules = [
            {"pattern": "过于频繁", "errorcode": "body_first"},
            {"pattern": _ERR_RATE, "errorcode": "body_second"},
        ]
        self.assertEqual(
            match_body_error(_ERR_RATE, rules,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)),
            "body_first")

    def test_invalid_rules_skipped(self):
        # 非法 rule（缺 pattern / 缺 errorcode / 非 dict）跳过不炸
        rules = [
            {"match": "contains", "errorcode": "body_x"},
            {"pattern": "过于频繁"},
            "not-a-dict",
            {"pattern": "过于频繁", "errorcode": "body_rate_limited"},
        ]
        self.assertEqual(
            match_body_error(_ERR_RATE, rules,
                             saw_tool_or_thinking=False, text_len=len(_ERR_RATE)),
            "body_rate_limited")


# ---------------------------------------------------------------------------
# 非流式 PASSTHROUGH：body 错误 → 四连换源重发
# ---------------------------------------------------------------------------

class TestNonStreamBodyErrorFailover(unittest.TestCase):

    def setUp(self):
        _unconfigured_hits.clear()

    def test_body_error_triggers_failover_and_cooldown(self):
        """200+错误文本 → 四连（log/failover+attempt_errors/cooldown+tried_set）
        换源重发，客户端只见重发后的正常响应。"""
        ns, cd = _make_server([_supply("s1", "http://up1/v1/messages"),
                               _supply("s2", "http://up2/v1/messages")],
                              ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_body_error()), _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 2)
        # 第二发打的是 s2（tried_set + cooldown 双重排除 s1）
        self.assertEqual(_sent_urls(m), ["http://up1/v1/messages",
                                         "http://up2/v1/messages"])
        # 四连：failover / attempt_errors / cooldown / tried_set（经 call_count 体现）
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(h._acc["attempt_errors"], [("s1", "body_rate_limited")])
        self.assertEqual(cd.calls, [("s1", 3600, "body_rate_limited")])
        self.assertEqual(h._acc["body_error"], "body_rate_limited")
        self.assertEqual(h._acc["attempts"], 2)
        # 客户端拿到的是换源后的正常响应，错误文本从未写出
        self.assertEqual(len(h._responses), 1)
        final = json.loads(h._responses[-1][1])
        self.assertEqual(final["content"][0]["text"], "正常回复")
        self.assertEqual(h._acc["status"], 200)

    def test_body_error_resp_closed_before_continue(self):
        """轻4：continue 前补 resp.close()（对标 budget_retry 命中后 close）。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        bad = _FakeResp(_anth_body_error())
        _run(h, [bad, _FakeResp(_anth_good())])
        self.assertTrue(bad.closed)

    def test_cooldown_drives_next_request_avoidance(self):
        """流内命中的冷却跨请求生效：下一请求 select_supply 自动避开 s1。"""
        ns, cd = _make_server([_supply("s1", "http://up1/v1/messages"),
                               _supply("s2", "http://up2/v1/messages")],
                              ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        _run(h, [_FakeResp(_anth_body_error()), _FakeResp(_anth_good())])
        h2 = _make_handler(ns, _anth_client_body())
        m2 = _run(h2, [_FakeResp(_anth_good())])
        self.assertEqual(_sent_urls(m2), ["http://up2/v1/messages"])

    def test_route_miss_error_code_maps_21600(self):
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_body_error(_ERR_ROUTE)), _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(cd.calls, [("s1", 21600, "body_route_miss")])

    def test_all_supplies_exhausted_returns_503_with_body_code(self):
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_body_error()),
                     _FakeResp(_anth_body_error())])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(h._responses[-1][0], 503)
        self.assertIn(b"body_rate_limited", h._responses[-1][1])
        self.assertEqual(h._acc["attempt_errors"],
                         [("s1", "body_rate_limited"), ("s2", "body_rate_limited")])

    def test_failover_off_passes_through_error_body(self):
        """failover=off 的 route：不换源不冷却，原样写回（对齐 HTTPError 未配语义）。"""
        ns, cd = _make_server([_supply("s1")], ["s1"], failover="off")
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_body_error())])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        # 原样写回（未配分支形态）
        self.assertEqual(h._responses[-1][1], json.dumps(_anth_body_error()).encode())
        self.assertEqual(_snapshot_unconfigured_hits().get("body_rate_limited"), 1)


class TestNonStreamUnconfiguredAndZeroChange(unittest.TestCase):

    def setUp(self):
        _unconfigured_hits.clear()

    def test_unconfigured_code_writes_back_as_is(self):
        """命中但 cooldown_rules 未配该 code → _record_unconfigured + 原样写回
        resp_body（中3：真 fail-open，客户端见上游原文）。"""
        ns, cd = _make_server([_supply("s1")], ["s1"], cooldown_rules=[])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_body_error())])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(h._responses[-1][0], 200)
        # 原样写回：客户端收到的是上游原文（错误文本本身），非 proxy error 体
        self.assertEqual(h._responses[-1][1],
                         json.dumps(_anth_body_error()).encode())
        self.assertEqual(_snapshot_unconfigured_hits().get("body_rate_limited"), 1)
        self.assertEqual(h._acc["body_error"], "body_rate_limited")

    def test_normal_response_zero_change_with_rules(self):
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(h._acc["body_error"], "")
        self.assertEqual(json.loads(h._responses[-1][1])["content"][0]["text"],
                         "正常回复")

    def test_no_rules_key_zero_behavior(self):
        """无 body_error_rules 键 → 功能整体关闭，错误文本原样透传。"""
        ns, cd = _make_server([_supply("s1")], ["s1"], body_rules=[])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_body_error())])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._responses[-1][1],
                         json.dumps(_anth_body_error()).encode())
        self.assertEqual(h._acc["body_error"], "")

    def test_tool_use_content_not_triggered(self):
        """结构条件 1（非流式）：content 含 tool_use 块 + 错误串 → 不触发。"""
        body = _anth_body_error()
        body["content"] = [
            {"type": "text", "text": _ERR_RATE},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}}]
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(body)])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc["body_error"], "")

    def test_thinking_content_not_triggered(self):
        body = _anth_body_error()
        body["content"] = [
            {"type": "thinking", "thinking": _ERR_RATE},
            {"type": "text", "text": "答"}]
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(body)])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])

    def test_long_text_not_triggered(self):
        """结构条件 2：text 全文 > 200 → 不触发（讨论错误文案的正常长回复）。"""
        body = _anth_body_error(_ERR_RATE + "x" * 300)
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(body)])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])

    def test_raw_non_json_body_triggered(self):
        """兜底：JSON 解析失败 → raw UTF-8 全文匹配。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_ERR_RATE.encode("utf-8")), _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(cd.calls, [("s1", 3600, "body_rate_limited")])

    def test_error_field_json_body_triggered(self):
        """轻5 兜底：JSON 合法但无 content text（error 字段形态）→ 全文兜底匹配。"""
        payload = json.dumps({"type": "error",
                              "error": {"type": "rate_limit", "message": _ERR_RATE}},
                             ensure_ascii=False).encode("utf-8")
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(payload), _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(cd.calls, [("s1", 3600, "body_rate_limited")])


# ---------------------------------------------------------------------------
# 流式：旁路收口判定 + 跨请求冷却（当前请求不救）
# ---------------------------------------------------------------------------

def _decode_chunked(raw: bytes) -> bytes:
    body = bytearray()
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\r\n", pos)
        size = int(raw[pos:end], 16)
        pos = end + 2
        if not size:
            break
        body.extend(raw[pos:pos + size])
        pos += size + 2
    return bytes(body)


class TestStreamingBodyError(unittest.TestCase):

    def setUp(self):
        _unconfigured_hits.clear()

    def test_error_stream_bytes_identical_and_cooldown_triggered(self):
        """SSE text 错误流：透传字节与无规则时逐字节一致 + cooldown + body_error。"""
        sse = _anth_sse_text(_ERR_RATE)
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        m = _run(h, [_FakeResp(sse)])
        self.assertEqual(m.call_count, 1)                      # 流式不救当前请求
        self.assertEqual(cd.calls, [("s1", 3600, "body_rate_limited")])
        self.assertEqual(h._acc["body_error"], "body_rate_limited")
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(h._acc["attempts"], 1)
        # 透传字节零变化：与无规则基线逐字节一致
        ns0, cd0 = _make_server([_supply("s1")], ["s1"], body_rules=[])
        h0 = _make_handler(ns0, _anth_client_body(stream=True))
        _run(h0, [_FakeResp(sse)])
        self.assertEqual(h.wfile.getvalue(), h0.wfile.getvalue())
        # 观测：流终态正常（业务错误非协议完整性错误）
        self.assertEqual(h._acc["stream_integrity"], "valid")

    def test_error_stream_access_log_body_error_field(self):
        """ACCESS 行含 body_error=<code>（观测闭环，§2.5）。"""
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        with patch("core.server.urllib.request.urlopen",
                   side_effect=[_FakeResp(_anth_sse_text(_ERR_RATE))]):
            with self.assertLogs("model_proxy.access", level="INFO") as cap:
                h._forward_logged("POST")
        acc_lines = [r.getMessage() for r in cap.records if "ACCESS " in r.getMessage()]
        self.assertEqual(len(acc_lines), 1)
        self.assertIn("body_error=body_rate_limited", acc_lines[0])
        self.assertEqual(cd.calls, [("s1", 3600, "body_rate_limited")])

    def test_tool_use_stream_not_triggered(self):
        """流内 tool_use 块 + 错误串 → 结构条件拦截，不冷却。"""
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        m = _run(h, [_FakeResp(_anth_sse_tool_use(_ERR_RATE))])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc.get("body_error", ""), "")

    def test_long_text_stream_not_triggered(self):
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        m = _run(h, [_FakeResp(_anth_sse_text(_ERR_RATE + "x" * 300))])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])

    def test_no_rules_key_stream_zero_behavior(self):
        ns, cd = _make_server([_supply("s1")], ["s1"], body_rules=[])
        h = _make_handler(ns, _anth_client_body(stream=True))
        m = _run(h, [_FakeResp(_anth_sse_text(_ERR_RATE))])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc.get("body_error", ""), "")

    def test_unconfigured_code_no_cooldown(self):
        ns, cd = _make_server([_supply("s1")], ["s1"], cooldown_rules=[])
        h = _make_handler(ns, _anth_client_body(stream=True))
        m = _run(h, [_FakeResp(_anth_sse_text(_ERR_RATE))])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(cd.calls, [])
        self.assertEqual(_snapshot_unconfigured_hits().get("body_rate_limited"), 1)
        self.assertEqual(h._acc["body_error"], "body_rate_limited")

    def test_client_disconnect_still_triggers_judgement(self):
        """中2：判定挂 finally——客户端断开（BrokenPipe 提前退出）也触发判定+冷却。
        错误 text 所在 chunk 已成功写出并喂 observer（设计 §2.4 第 3 条）。"""
        sse = _anth_sse_text(_ERR_RATE)
        # 在 text_delta 事件后切一刀：chunk1（含错误 text）写客户端成功，chunk2 断开
        cut = sse.index(b"event: message_delta")
        chunk1, chunk2 = sse[:cut], sse[cut:]
        ns, cd = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))

        class _BoomWfile:
            """前 3 次 write 成功（chunk1 的 3 次 chunked 写），之后 BrokenPipe。"""

            def __init__(self):
                self.ok = 3
                self.buf = io.BytesIO()

            def write(self, data):
                if self.ok <= 0:
                    raise BrokenPipeError()
                self.ok -= 1
                return self.buf.write(data)

            def flush(self):
                pass

        h.wfile = _BoomWfile()
        m = _run(h, [_ChunkedResp([chunk1, chunk2])])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["stream_integrity"], "client_disconnect")
        self.assertEqual(cd.calls, [("s1", 3600, "body_rate_limited")])
        self.assertEqual(h._acc["body_error"], "body_rate_limited")

    def test_cooldown_drives_next_request_avoidance(self):
        """流式命中的冷却驱动下一请求自动换 supply（自愈路径，§2.4 第 4 条）。"""
        ns, cd = _make_server([_supply("s1", "http://up1/v1/messages"),
                               _supply("s2", "http://up2/v1/messages")],
                              ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        _run(h, [_FakeResp(_anth_sse_text(_ERR_RATE))])
        h2 = _make_handler(ns, _anth_client_body())
        m2 = _run(h2, [_FakeResp(_anth_good())])
        self.assertEqual(_sent_urls(m2), ["http://up2/v1/messages"])


# ---------------------------------------------------------------------------
# 配置校验（容错：非法 rule 跳过 + warning）
# ---------------------------------------------------------------------------

class TestBodyErrorConfigValidation(unittest.TestCase):

    def _store(self, cfg) -> ConfigStore:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            return ConfigStore(path)

    def test_valid_rules_loaded_and_returned(self):
        cfg = {"body_error_rules": _BODY_RULES,
               "cooldown_rules": _COOLDOWN_RULES}
        with self.assertNoLogs("core.server", level="WARNING"):
            store = self._store(cfg)
        self.assertEqual(store.get_body_error_rules(), _BODY_RULES)

    def test_missing_key_returns_empty(self):
        store = self._store({})
        self.assertEqual(store.get_body_error_rules(), [])

    def test_invalid_rules_warn_and_skip(self):
        cfg = {"body_error_rules": [
            {"match": "contains", "errorcode": "body_x"},          # 缺 pattern
            {"pattern": "p1"},                                      # 缺 errorcode
            {"pattern": "p1", "errorcode": "body_x", "match": "regex"},  # 非法 match
            {"pattern": "p1", "errorcode": "body_x", "max_text_len": 0},  # 非法长度
            {"pattern": "p1", "errorcode": "body_x"},               # 合法
        ]}
        with self.assertLogs("core.server", level="WARNING") as cap:
            store = self._store(cfg)
        joined = "\n".join(r.getMessage() for r in cap.records)
        self.assertIn("body_error_rules", joined)
        # 校验只告警不剔除（getter 返回原列表），运行期由 match_body_error 容错跳过
        self.assertEqual(len(store.get_body_error_rules()), 5)

    def test_non_list_rules_warn(self):
        with self.assertLogs("core.server", level="WARNING") as cap:
            self._store({"body_error_rules": {"pattern": "p"}})
        self.assertIn("not a list", "\n".join(r.getMessage() for r in cap.records))

    def test_cooldown_rules_body_string_code_no_warning(self):
        """中1：errorcode 元素放开为任意非空字符串——合并式 body_* 行零 warning。"""
        cfg = {"cooldown_rules": [
            {"errorcode": [429, "body_rate_limited"], "cooldown_seconds": 3600},
            {"errorcode": [401, "URLError", "body_route_miss"],
             "cooldown_seconds": 21600},
        ]}
        with self.assertNoLogs("core.server", level="WARNING"):
            self._store(cfg)

    def test_cooldown_rules_empty_string_code_still_warns(self):
        cfg = {"cooldown_rules": [
            {"errorcode": [429, ""], "cooldown_seconds": 60}]}
        with self.assertLogs("core.server", level="WARNING") as cap:
            self._store(cfg)
        self.assertIn("invalid", "\n".join(r.getMessage() for r in cap.records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
