"""thinking 签名 400 自适应剥离重试链路集成测（假上游驱动，脱网络）。

照抄 test_body_error.py 的 urlopen 队列 patch + SimpleNamespace server 模式。
覆盖（设计记录 docs/designs/2026-10-28-thinking空签名剥离与supply级自适应.md
§验证方式 6-10/12）：
- 核心链路：400 签名错误 → learn + 同 supply 剥离重发 → 200
- 重试幂等：双 400 → flag 置位不无限循环 → 落 cooldown+failover（矩阵 #5）
- 语法+签名叠加（矩阵 #3）：语法400 → 签名400 → 200，两 flag 独立
- fail-open（矩阵 #6）：guard 不满足（target≠anthropic）→ 不 learn 不剥离，现状路径
- raw_body 逃逸回归（推演·具象3）：无任何其他 re-dump 触发源的 supply + 已 learn
  的 store → 出站 body 确实已剥离
- ACCESS 行观测：触发重试 thinking_strip=1；未触发 thinking_strip=0

运行：cd tools/model_proxy && python3 -m unittest tests.test_thinking_strip_retry -v
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import ModelProxyHandler  # noqa: E402
from core.thinking_guard import ThinkingStripStore  # noqa: E402


# ---------------------------------------------------------------------------
# 假上游 / 假配置
# ---------------------------------------------------------------------------

class _FakeResp:
    """模拟 http.client 响应：read() 一次性返回全部字节。"""

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


def _http_error(status, body: bytes):
    return urllib.error.HTTPError("http://up", status, "err", {}, io.BytesIO(body))


_SIG_400 = (b'{"error":{"type":"invalid_request_error","message":'
            b'"Invalid `signature` in `thinking` block: signature VerboseB"}')

_COOLDOWN_RULES = [
    {"errorcode": [400, 401, 429, 500, 502, 503, 504], "cooldown_seconds": 60},
]

_FULL_ENUM = {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}


class _FakeConfig:
    def __init__(self, supply_map, routes_map, strategies,
                 cooldown_rules=None):
        self._supply_map = supply_map
        self._routes_map = routes_map
        self._strategies = strategies
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
        return []

    def get_cooldown_rules(self):
        return list(self._cooldown_rules)


class _FakeCooldown:
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
# 夹具：supply / 客户端 body（带 mcli 空签名 thinking 历史）
# ---------------------------------------------------------------------------

def _supply(sid, url="http://up/v1/messages", target_model="m1"):
    return {"id": sid, "url": url, "protocol": "anthropic", "appkey": "k",
            "target_model": target_model, "reasoning_capability": dict(_FULL_ENUM)}


def _strip_client_body(**over):
    """anthropic 请求：assistant 历史含 1 空签名 thinking 块 + 1 aigc 自签块。"""
    body = {
        "model": "claude-opus", "max_tokens": 16000,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "mcli 产块", "signature": ""},
                {"type": "thinking", "thinking": "aigc 自签块",
                 "signature": "EqQBCabc123"},
                {"type": "text", "text": "上一轮回复"},
            ]},
            {"role": "user", "content": "go on"},
        ],
    }
    body.update(over)
    return body


def _anth_good():
    return {"id": "m2", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "正常回复"}],
            "usage": {"input_tokens": 3, "output_tokens": 100}}


def _empty_sig_blocks(body_json):
    """数出出站 body 中残留的空签名 thinking 块数。"""
    n = 0
    for msg in body_json.get("messages", []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "thinking" \
                        and not b.get("signature"):
                    n += 1
    return n


# ---------------------------------------------------------------------------
# handler 驱动
# ---------------------------------------------------------------------------

def _make_server(supplies, tier_supplies, store=None, failover="on",
                 cooldown_rules=None):
    supply_map = {s["id"]: s for s in supplies}
    routes_map = {"r1": {"id": "r1", "tiers": {"opus": tier_supplies},
                         "failover": failover}}
    strategies = [{"client_token": "tok", "route_id": "r1"}]
    cd = _FakeCooldown()
    if store is None:
        store = ThinkingStripStore()
    ns = SimpleNamespace(
        config_store=_FakeConfig(supply_map, routes_map, strategies,
                                 cooldown_rules=cooldown_rules),
        cooldown_store=cd, pref_store=_FakePref(), sidecar_store=_FakeSidecar(),
        thinking_strip_store=store)
    return ns, cd, store


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
        "nudge_rewritten": "", "thinking_strip": 0,
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


def _sent_bodies(mock):
    return [json.loads(c.args[0].data) for c in mock.call_args_list]


def _sent_urls(mock):
    return [c.args[0].full_url for c in mock.call_args_list]


# ---------------------------------------------------------------------------
# 核心链路（§验证方式 6）
# ---------------------------------------------------------------------------

class TestCoreRetryChain(unittest.TestCase):

    def test_signature_400_learns_strips_and_retries_once(self):
        """aigc 单 supply：[签名400, 200] → learn + 剥离重发 → 200，零 cooldown。"""
        ns, cd, store = _make_server([_supply("s1", "http://up/v1/messages")], ["s1"])
        h = _make_handler(ns, _strip_client_body())
        m = _run(h, [_http_error(400, _SIG_400), _FakeResp(_anth_good())])
        # 出站 2 次，同 supply（无 cooldown/tried_set）
        self.assertEqual(m.call_count, 2)
        self.assertEqual(_sent_urls(m), ["http://up/v1/messages"] * 2)
        bodies = _sent_bodies(m)
        # 第 1 次：store 未 learn → 不剥离（空签名块原样出站）
        self.assertEqual(_empty_sig_blocks(bodies[0]), 1)
        # 第 2 次：剥离生效——空签名块删除、aigc 自签块保留
        self.assertEqual(_empty_sig_blocks(bodies[1]), 0)
        kept = [b for b in bodies[1]["messages"][1]["content"]
                if b.get("type") == "thinking"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["signature"], "EqQBCabc123")
        # 无 cooldown、最终 200、store 已 learn、观测置位
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc["status"], 200)
        self.assertEqual(json.loads(h._responses[-1][1])["content"][0]["text"],
                         "正常回复")
        self.assertTrue(store.snapshot("s1"))
        self.assertEqual(h._acc["thinking_strip"], 1)
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(h._acc["attempts"], 2)

    def test_second_request_pre_stripped(self):
        """稳态：learn 后的下一请求 D 点前置剥离，单次出站零额外尝试。"""
        ns, cd, store = _make_server([_supply("s1")], ["s1"])
        h1 = _make_handler(ns, _strip_client_body())
        _run(h1, [_http_error(400, _SIG_400), _FakeResp(_anth_good())])
        h2 = _make_handler(ns, _strip_client_body())
        m2 = _run(h2, [_FakeResp(_anth_good())])
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(_empty_sig_blocks(_sent_bodies(m2)[0]), 0)
        self.assertEqual(h2._acc["thinking_strip"], 0)   # 未触发重试
        self.assertEqual(cd.calls, [])


# ---------------------------------------------------------------------------
# 重试幂等 / 剥离不掉兜底（§验证方式 7，矩阵 #5）
# ---------------------------------------------------------------------------

class TestRetryIdempotent(unittest.TestCase):

    def test_double_400_falls_back_to_cooldown_failover(self):
        """[400, 400]：flag 置位不无限循环 → 落既有 cooldown+failover 路径。"""
        ns, cd, store = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _strip_client_body())
        m = _run(h, [_http_error(400, _SIG_400), _http_error(400, _SIG_400)])
        self.assertEqual(m.call_count, 2)
        # 重试轮已剥离（D 点命中 learn），但上游仍 400 → 剥离不掉走现状路径
        self.assertEqual(_empty_sig_blocks(_sent_bodies(m)[1]), 0)
        self.assertEqual(cd.calls, [("s1", 60, "http_400")])
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(h._acc["attempt_errors"], [("s1", "http_400")])
        # 单 supply 耗尽 → 503
        self.assertEqual(h._responses[-1][0], 503)
        self.assertEqual(h._acc["thinking_strip"], 1)


# ---------------------------------------------------------------------------
# 语法+签名叠加（§验证方式 8，矩阵 #3）
# ---------------------------------------------------------------------------

class TestStackedWithReasoningRetry(unittest.TestCase):

    def test_syntax_400_then_signature_400_then_200(self):
        """语法400 → 签名400 → 200：两 flag 独立各一次，3 次出站。"""
        ns, cd, store = _make_server([_supply("s1")], ["s1"])
        body = _strip_client_body()
        body["thinking"] = {"type": "enabled", "budget_tokens": 10000}
        h = _make_handler(ns, body)
        syntax_400 = (b'{"error":{"message":'
                      b'"thinking.type.adaptive is not supported by this model"}}')
        m = _run(h, [_http_error(400, syntax_400),
                     _http_error(400, _SIG_400),
                     _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 3)
        bodies = _sent_bodies(m)
        # 第 1 次：默认 adaptive 语法（未剥离）
        self.assertEqual(bodies[0]["thinking"]["type"], "adaptive")
        self.assertEqual(_empty_sig_blocks(bodies[0]), 1)
        # 第 2 次：语法重试切到 enabled（reasoning learn），尚未剥离（strip 未 learn）
        self.assertEqual(bodies[1]["thinking"]["type"], "enabled")
        self.assertEqual(_empty_sig_blocks(bodies[1]), 1)
        # 第 3 次：已剥离 + enabled 语法保持
        self.assertEqual(bodies[2]["thinking"]["type"], "enabled")
        self.assertEqual(_empty_sig_blocks(bodies[2]), 0)
        # 终态 200，零 cooldown，观测只记 thinking_strip（语法重试无独立观测位）
        self.assertEqual(h._acc["status"], 200)
        self.assertEqual(cd.calls, [])
        self.assertEqual(h._acc["thinking_strip"], 1)


# ---------------------------------------------------------------------------
# fail-open（§验证方式 9，矩阵 #6：guard 不满足 → 不 learn 不剥离，现状路径）
# ---------------------------------------------------------------------------

class TestFailOpenGuard(unittest.TestCase):

    def test_target_not_anthropic_no_learn_no_strip(self):
        """responses→responses（target≠anthropic）：签名 400 不触发 learn/剥离，
        走既有 cooldown+failover 换源。"""
        supplies = [
            {"id": "s1", "url": "http://up1/v1/responses", "protocol": "responses",
             "appkey": "k", "target_model": "m1"},
            {"id": "s2", "url": "http://up2/v1/responses", "protocol": "responses",
             "appkey": "k", "target_model": "m1"},
        ]
        ns, cd, store = _make_server(supplies, ["s1", "s2"])
        h = _make_handler(ns, {"model": "claude-opus", "input": "hi"},
                          path="/v1/responses")
        good = {"id": "r1", "output": [], "usage": {"total_tokens": 5}}
        m = _run(h, [_http_error(400, _SIG_400), _FakeResp(good)])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(_sent_urls(m), ["http://up1/v1/responses",
                                         "http://up2/v1/responses"])
        # 未 learn（不污染）、未置观测、cooldown+failover 现状路径
        self.assertFalse(store.snapshot("s1"))
        self.assertEqual(h._acc["thinking_strip"], 0)
        self.assertEqual(cd.calls, [("s1", 60, "http_400")])
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(h._acc["status"], 200)


# ---------------------------------------------------------------------------
# raw_body 逃逸回归（§验证方式 10，推演·具象3）
# ---------------------------------------------------------------------------

class TestRawBodyEscapeRegression(unittest.TestCase):

    def test_strip_reaches_wire_without_other_redumps(self):
        """无 target_model、无 reasoning_wire、无 system_inject、预算无变化的
        supply：send_body 本会停留在 raw_body——D 点强制 re-dump 必须生效，
        出站 body 确实已剥离。"""
        supply = {"id": "s1", "url": "http://up/v1/messages", "protocol": "anthropic",
                  "appkey": "k"}   # 无 target_model / 无 reasoning_capability
        ns, cd, store = _make_server([supply], ["s1"])
        store.learn("s1")   # 已 learn：D 点 guard 命中
        h = _make_handler(ns, _strip_client_body())
        m = _run(h, [_FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 1)
        sent = json.loads(m.call_args_list[0].args[0].data)
        self.assertEqual(_empty_sig_blocks(sent), 0)
        kept = [b for b in sent["messages"][1]["content"]
                if b.get("type") == "thinking"]
        self.assertEqual(kept[0]["signature"], "EqQBCabc123")
        # model 未改写（无 target_model），剥离经 re-dump 落到线上
        self.assertEqual(sent["model"], "claude-opus")
        self.assertEqual(h._acc["thinking_strip"], 0)   # 无重试，仅剥离
        self.assertEqual(cd.calls, [])


# ---------------------------------------------------------------------------
# ACCESS 行观测（§验证方式 12）
# ---------------------------------------------------------------------------

class TestAccessLogObservation(unittest.TestCase):

    def _access_line(self, h, queue):
        with patch("core.server.urllib.request.urlopen",
                   side_effect=list(queue)):
            with self.assertLogs("model_proxy.access", level="INFO") as cap:
                h._forward_logged("POST")
        lines = [r.getMessage() for r in cap.records
                 if "ACCESS " in r.getMessage()]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_triggered_access_line_has_thinking_strip_1(self):
        ns, cd, store = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _strip_client_body())
        line = self._access_line(
            h, [_http_error(400, _SIG_400), _FakeResp(_anth_good())])
        self.assertIn("thinking_strip=1", line)
        self.assertIn("status=200", line)

    def test_untriggered_access_line_has_thinking_strip_0(self):
        ns, cd, store = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns, _strip_client_body())
        line = self._access_line(h, [_FakeResp(_anth_good())])
        self.assertIn("thinking_strip=0", line)
        self.assertIn("status=200", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
