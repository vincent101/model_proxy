"""④b 预算治理（反应式 ×2 阶梯重试）+ ② 反向缺省预算 + ⑤ 监控字段 单测（脱网络）。

驱动 ModelProxyHandler._forward 全链路：patch core.server.urllib.request.urlopen
按队列返回假上游响应/异常，断言出站 body 的预算字段、重试次数与轨迹、_acc 观测
字段、与语法重试/failover 的共存语义。

覆盖（设计记录 2026-08-07 §②/§④/§⑤ + 架构审查 R1-R4）：
- 三协议截断判定驱动重试（anthropic stop=max_tokens / chat finish=length /
  responses incomplete+max_output_tokens，且正文缺失才触发）
- ×2 阶梯、封顶 131072（next==current 停止）、次数上限 5
- 首轮 stamp 原值（客户端给定值一字不改发出）；重试轮 stamp 放大值
- R2：stamp 字段名分协议（max_tokens / max_completion_tokens / max_output_tokens，
  含 responses→responses 透传）
- 重试不 cooldown、不进 tried_set、不计 failover（同 supply 重选）
- 与 400 语法重试共存（独立状态，互不消耗次数）
- R4：爬升途中 failover 换 supply，放大后的预算被下一 supply 继承（有意行为）
- 流式不重试，仅收口记 budget_truncated=1（字节已下发无法回追）
- ② 反向缺省：responses→anthropic 不传 max_tokens 时 THINKING→16384 / 否则→4096
- budget_retry.enabled=false 时退回原透传行为

运行：cd tools/model_proxy && python3 -m unittest tests.test_budget_retry -v
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


def _http_error(status, body: bytes):
    return urllib.error.HTTPError("http://up", status, "err", {}, io.BytesIO(body))


class _FakeConfig:
    def __init__(self, supply_map, routes_map, strategies, budget_retry=None):
        self._supply_map = supply_map
        self._routes_map = routes_map
        self._strategies = strategies
        self._budget_retry = budget_retry

    def maybe_reload(self):
        return False

    def get_strategies(self):
        return self._strategies

    def get_routes_map(self):
        return self._routes_map

    def get_supply_map(self):
        return self._supply_map

    def get_default_cooldown(self):
        return 300

    def get_budget_retry(self):
        return self._budget_retry or {"enabled": True, "max_retries": 5, "ceiling": 131072}


class _FakeCooldown:
    def __init__(self):
        self.cooled = []

    def is_cooling(self, sid):
        return False

    def cooldown(self, sid, secs):
        self.cooled.append(sid)


class _FakePref:
    def __init__(self):
        self.learned = []
        self._cache = {}

    def snapshot(self, model):
        return self._cache.get(model, {})

    def learn(self, model, variant):
        self.learned.append((model, variant))
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


def _supply(sid, protocol):
    url = {"anthropic": "http://up/v1/messages",
           "chat": "http://up/v1/chat/completions",
           "responses": "http://up/v1/responses"}[protocol]
    return {"id": sid, "url": url, "protocol": protocol, "appkey": "k",
            "target_model": "m1", "reasoning_capability": dict(_FULL_ENUM)}


def _anth_truncated():
    return {"id": "m1", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "思考占满预算"}],
            "usage": {"input_tokens": 3, "output_tokens": 16000}}


def _anth_good():
    return {"id": "m2", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "想"},
                        {"type": "text", "text": "答"}],
            "usage": {"input_tokens": 3, "output_tokens": 100}}


def _chat_truncated():
    return {"choices": [{"finish_reason": "length",
                         "message": {"content": "", "reasoning_content": "思考占满"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 16000}}


def _chat_good():
    return {"choices": [{"finish_reason": "stop",
                         "message": {"content": "答"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 50}}


def _resp_truncated():
    return {"status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "summary": []}],
            "usage": {"input_tokens": 3, "output_tokens": 16000}}


def _resp_good():
    return {"status": "completed",
            "output": [{"type": "message",
                        "content": [{"type": "output_text", "text": "答"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 50}}


def _anth_client_body(**over):
    body = {"model": "claude-opus", "max_tokens": 16000,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(over)
    return body


def _resp_client_body(**over):
    body = {"model": "claude-opus", "input": "hi"}
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# handler 驱动
# ---------------------------------------------------------------------------

def _make_server(supplies, tier_supplies, budget_retry=None, failover="on"):
    supply_map = {s["id"]: s for s in supplies}
    routes_map = {"r1": {"id": "r1", "tiers": {"opus": tier_supplies}, "failover": failover}}
    strategies = [{"client_token": "tok", "route_id": "r1"}]
    cd = _FakeCooldown()
    pref = _FakePref()
    ns = SimpleNamespace(
        config_store=_FakeConfig(supply_map, routes_map, strategies, budget_retry),
        cooldown_store=cd, pref_store=pref, sidecar_store=_FakeSidecar())
    return ns, cd, pref


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
    return [json.loads(c.args[0].data.decode()) for c in mock.call_args_list]


# ---------------------------------------------------------------------------
# ④b 核心：阶梯 / 封顶 / 次数 / 字段名 / 观测
# ---------------------------------------------------------------------------

class TestBudgetRetryPassthrough(unittest.TestCase):

    def test_retry_once_then_success(self):
        ns, cd, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_truncated()), _FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        # 首轮原值（客户端给 16000 就发 16000），重试轮 ×2
        self.assertEqual([b["max_tokens"] for b in bodies], [16000, 32000])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(h._acc["budget_retried"], "16000→32000")
        self.assertEqual(h._acc["budget_truncated"], 0)
        self.assertEqual(h._acc["stop_reason"], "end_turn")
        # 不 cooldown、不进 tried_set（同一 supply 被重选才有第二次调用）、不计 failover
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(h._acc["attempts"], 2)
        # 客户端拿到的是重试后的正常响应
        final = json.loads(h._responses[-1][1])
        self.assertEqual(final["stop_reason"], "end_turn")

    def test_ladder_clamps_at_ceiling_then_returns_truncated(self):
        ns, cd, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_truncated()) for _ in range(6)])
        bodies = _sent_bodies(m)
        # 16000→32000→64000→128000→131072（钳到封顶），next==current 停止：共 5 发
        self.assertEqual([b["max_tokens"] for b in bodies],
                         [16000, 32000, 64000, 128000, 131072])
        self.assertEqual(m.call_count, 5)
        self.assertEqual(h._acc["budget_truncated"], 1)
        self.assertEqual(h._acc["budget_retried"],
                         "16000→32000,32000→64000,64000→128000,128000→131072")
        self.assertEqual(h._acc["stop_reason"], "max_tokens")
        # 到顶如实返回截断响应（stop=max_tokens 原样保留）
        final = json.loads(h._responses[-1][1])
        self.assertEqual(final["stop_reason"], "max_tokens")

    def test_retry_count_capped_at_5(self):
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _anth_client_body(max_tokens=4096))
        m = _run(h, [_FakeResp(_anth_truncated()) for _ in range(8)])
        bodies = _sent_bodies(m)
        # 4096→8192→16384→32768→65536→131072：5 次重试打满（共 6 发），第 7 发不再发生
        self.assertEqual([b["max_tokens"] for b in bodies],
                         [4096, 8192, 16384, 32768, 65536, 131072])
        self.assertEqual(m.call_count, 6)
        self.assertEqual(h._acc["budget_truncated"], 1)

    def test_disabled_config_falls_back_to_passthrough(self):
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"],
                                budget_retry={"enabled": False,
                                              "max_retries": 5, "ceiling": 131072})
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_truncated()), _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 1)   # 不重试
        self.assertEqual(h._acc["budget_retried"], "")
        self.assertEqual(h._acc["budget_truncated"], 0)
        final = json.loads(h._responses[-1][1])
        self.assertEqual(final["stop_reason"], "max_tokens")   # 原样透传

    def test_truncated_without_budget_baseline_marks_only(self):
        # 客户端没给 max_tokens（无爬升基线）→ 记 budget_truncated，无法重试
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        body = _anth_client_body()
        del body["max_tokens"]
        h = _make_handler(ns, body)
        m = _run(h, [_FakeResp(_anth_truncated())])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["budget_truncated"], 1)
        self.assertEqual(h._acc["budget_retried"], "")

    def test_not_truncated_no_retry(self):
        # stop=max_tokens 但有正文 → 非「正文缺失」，不重试
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        partial = _anth_good()
        partial["stop_reason"] = "max_tokens"
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(partial)])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["budget_retried"], "")
        self.assertEqual(h._acc["budget_truncated"], 0)


class TestBudgetRetryConvertedModes(unittest.TestCase):

    def test_chat_direction_retry_and_fallback_not_masking(self):
        """chat 截断响应（reasoning_content 会 fallback 填 text）仍在原始响应上识别
        并重试；stamp 字段为 max_completion_tokens。"""
        ns, _, _ = _make_server([_supply("s1", "chat")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_chat_truncated()), _FakeResp(_chat_good())])
        bodies = _sent_bodies(m)
        self.assertEqual([b["max_completion_tokens"] for b in bodies], [16000, 32000])
        self.assertNotIn("max_tokens", bodies[0])
        self.assertEqual(h._acc["budget_retried"], "16000→32000")
        # 客户端拿到重试后的正常转换结果（而非首发截断响应的 fallback 填充版）
        final = json.loads(h._responses[-1][1])
        self.assertEqual(final["content"][0], {"type": "text", "text": "答"})
        self.assertEqual(final["stop_reason"], "end_turn")

    def test_responses_direction_retry(self):
        ns, _, _ = _make_server([_supply("s1", "responses")], ["s1"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_resp_truncated()), _FakeResp(_resp_good())])
        bodies = _sent_bodies(m)
        self.assertEqual([b["max_output_tokens"] for b in bodies], [16000, 32000])
        self.assertEqual(h._acc["budget_retried"], "16000→32000")
        final = json.loads(h._responses[-1][1])
        self.assertEqual(final["stop_reason"], "end_turn")

    def test_passthrough_responses_uses_max_output_tokens(self):
        """R2：responses→responses 透传 stamp 字段是 max_output_tokens 不是 max_tokens。"""
        ns, _, _ = _make_server([_supply("s1", "responses")], ["s1"])
        h = _make_handler(ns, _resp_client_body(max_output_tokens=16000),
                          path="/v1/responses")
        m = _run(h, [_FakeResp(_resp_truncated()), _FakeResp(_resp_good())])
        bodies = _sent_bodies(m)
        self.assertEqual([b["max_output_tokens"] for b in bodies], [16000, 32000])
        self.assertNotIn("max_tokens", bodies[1])
        self.assertEqual(m.call_count, 2)

    def test_reverse_direction_retry(self):
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _resp_client_body(max_output_tokens=16000),
                          path="/v1/responses")
        m = _run(h, [_FakeResp(_anth_truncated()), _FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        # 反向转换 max_output_tokens→max_tokens，stamp anthropic 字段
        self.assertEqual([b["max_tokens"] for b in bodies], [16000, 32000])
        self.assertEqual(h._acc["budget_retried"], "16000→32000")


class TestReverseDefaultBudget(unittest.TestCase):
    """② 反向缺省预算：responses→anthropic 客户端不传 max_tokens 时按 remap 分档。"""

    def test_thinking_default_16384(self):
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _resp_client_body(reasoning={"effort": "high"}),
                          path="/v1/responses")
        m = _run(h, [_FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        self.assertEqual(bodies[0]["max_tokens"], 16384)
        # thinking 字段按 adaptive 语法下发（THINKING 确为本次 remap 结果）
        self.assertIn("thinking", bodies[0])

    def test_non_thinking_default_4096(self):
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _resp_client_body(), path="/v1/responses")
        m = _run(h, [_FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        self.assertEqual(bodies[0]["max_tokens"], 4096)
        self.assertNotIn("thinking", bodies[0])

    def test_retry_starts_from_thinking_default(self):
        # 缺省 16384 仍不够 → ④b 从 16384 起爬（默认值只决定从哪开始爬）
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _resp_client_body(reasoning={"effort": "high"}),
                          path="/v1/responses")
        m = _run(h, [_FakeResp(_anth_truncated()), _FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        self.assertEqual([b["max_tokens"] for b in bodies], [16384, 32768])
        self.assertEqual(h._acc["budget_retried"], "16384→32768")


class TestCoexistenceWithExistingRetries(unittest.TestCase):

    def test_syntax_retry_then_budget_retry_independent(self):
        """先 400 语法重试、后 200 预算重试：独立状态，互不消耗次数。"""
        ns, cd, pref = _make_server([_supply("s1", "anthropic")], ["s1"])
        body = _anth_client_body(
            thinking={"type": "adaptive"}, output_config={"effort": "high"})
        h = _make_handler(ns, body)
        err = _http_error(400, b'{"error":{"message":"output_config is not permitted"}}')
        m = _run(h, [err, _FakeResp(_anth_truncated()), _FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        self.assertEqual(m.call_count, 3)
        # 语法重试发生且只一次（adaptive → enabled 学到偏好）
        self.assertEqual(pref.learned, [("m1", "anthropic_enabled")])
        self.assertEqual(bodies[1].get("thinking", {}).get("type"), "enabled")
        # 预算重试在语法重试之后照常发生（16000→32000），不被 _reasoning_retried 阻塞
        self.assertEqual(bodies[1]["max_tokens"], 16000)
        self.assertEqual(bodies[2]["max_tokens"], 32000)
        self.assertEqual(h._acc["budget_retried"], "16000→32000")
        self.assertEqual(h._acc["budget_truncated"], 0)
        # 两类重试都不 cooldown / 不进 tried_set / 不计 failover
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._acc["failover"], 0)

    def test_failover_inherits_raised_budget(self):
        """R4 有意行为：爬升途中 failover 换 supply，放大后的预算被下一 supply 继承。"""
        ns, cd, _ = _make_server([_supply("s1", "anthropic"), _supply("s2", "anthropic")],
                                 ["s1", "s2"])
        h = _make_handler(ns, _anth_client_body())
        m = _run(h, [_FakeResp(_anth_truncated()),      # s1: 截断 → 16000→32000
                     _http_error(500, b"upstream boom"),  # s1: failover
                     _FakeResp(_anth_good())])            # s2: 继承 32000 直接成功
        bodies = _sent_bodies(m)
        self.assertEqual([b["max_tokens"] for b in bodies], [16000, 32000, 32000])
        self.assertEqual(cd.cooled, ["s1"])          # failover 正常冷却 s1
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(h._acc["budget_retried"], "16000→32000")
        self.assertEqual(h._acc["budget_truncated"], 0)
        self.assertEqual(json.loads(h._responses[-1][1])["stop_reason"], "end_turn")


class TestStreamingNoRetry(unittest.TestCase):

    def test_stream_passthrough_anthropic_no_retry_logs_only(self):
        ns, cd, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        sse = (
            b'event:content_block_start\n'
            b'data:{"type":"content_block_start","index":0,'
            b'"content_block":{"type":"thinking","thinking":""}}\n\n'
            b'event:content_block_delta\n'
            b'data:{"type":"content_block_delta","index":0,'
            b'"delta":{"type":"thinking_delta","thinking":"\xe6\x83\xb3"}}\n\n'
            b'event:message_delta\n'
            b'data:{"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
            b'"usage":{"input_tokens":3,"output_tokens":16000}}\n\n'
            b'event:message_stop\ndata:{"type":"message_stop"}\n\n'
        )
        m = _run(h, [_FakeResp(sse), _FakeResp(_anth_good())])
        self.assertEqual(m.call_count, 1)            # 流式不重试
        self.assertEqual(h._acc["budget_retried"], "")
        self.assertEqual(h._acc["budget_truncated"], 1)   # 仅收口记标记
        self.assertEqual(h._acc["stop_reason"], "max_tokens")
        self.assertGreater(len(h.wfile.getvalue()), 0)    # 字节照常下发

    def test_stream_passthrough_with_text_not_flagged(self):
        # 流内产出过 text block：即便 stop=max_tokens 也不是「正文缺失」，不记
        ns, _, _ = _make_server([_supply("s1", "anthropic")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        sse = (
            b'event:content_block_start\n'
            b'data:{"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
            b'event:message_delta\n'
            b'data:{"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
            b'"usage":{"output_tokens":16000}}\n\n'
            b'event:message_stop\ndata:{"type":"message_stop"}\n\n'
        )
        m = _run(h, [_FakeResp(sse)])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["budget_truncated"], 0)
        self.assertEqual(h._acc["stop_reason"], "max_tokens")

    def test_stream_chat_no_retry_logs_only(self):
        ns, _, _ = _make_server([_supply("s1", "chat")], ["s1"])
        h = _make_handler(ns, _anth_client_body(stream=True))
        sse = (
            b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x83\xb3"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
            b'data: [DONE]\n\n'
        )
        m = _run(h, [_FakeResp(sse), _FakeResp(_chat_good())])
        self.assertEqual(m.call_count, 1)            # 流式不重试
        self.assertEqual(h._acc["budget_retried"], "")
        self.assertEqual(h._acc["budget_truncated"], 1)
        self.assertEqual(h._acc["stop_reason"], "max_tokens")   # length 已映射


if __name__ == "__main__":
    unittest.main(verbosity=2)
