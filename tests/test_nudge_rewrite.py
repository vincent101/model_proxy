"""已知 SDK 注入文案改写（nudge rewrite）单测 + _forward 全程集成测试（脱网络）。

覆盖（设计记录 2026-08-09-cli-thinking-only-nudge文案proxy改写.md v3 改动 3）：
- 纯函数 _rewrite_known_injected_texts：
  - string 形态命中改写
  - text block 形态命中改写
  - 非 user role / 相似但不全等 / 空 messages / messages 非 list → 原样返回 False
- _forward 全程集成：含 nudge 的 anthropic 请求驱动 _forward，断言上游收到的 body
  中 nudge 已替换为新文案，且 Content-Length 与 body 长度一致

运行：cd tools/model_proxy && python3 -m unittest tests.test_nudge_rewrite -v
"""

import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import (  # noqa: E402
    ModelProxyHandler,
    _NUDGE_TEXT_ORIG,
    _NUDGE_TEXT_REWRITTEN,
    _rewrite_known_injected_texts,
)


# ---------------------------------------------------------------------------
# 单测：纯函数 _rewrite_known_injected_texts
# ---------------------------------------------------------------------------

class TestRewriteKnownInjectedTexts(unittest.TestCase):

    def test_string_form_hit(self):
        body = {"messages": [{"role": "user", "content": _NUDGE_TEXT_ORIG}]}
        self.assertTrue(_rewrite_known_injected_texts(body))
        self.assertEqual(body["messages"][0]["content"], _NUDGE_TEXT_REWRITTEN)

    def test_text_block_form_hit(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": _NUDGE_TEXT_ORIG}
        ]}]}
        self.assertTrue(_rewrite_known_injected_texts(body))
        self.assertEqual(body["messages"][0]["content"][0]["text"], _NUDGE_TEXT_REWRITTEN)

    def test_non_user_role_no_rewrite(self):
        body = {"messages": [{"role": "assistant", "content": _NUDGE_TEXT_ORIG}]}
        self.assertFalse(_rewrite_known_injected_texts(body))
        self.assertEqual(body["messages"][0]["content"], _NUDGE_TEXT_ORIG)

    def test_similar_but_not_equal_no_rewrite(self):
        # 相似但不全等（strip 后仍不等）：末尾加一个字符、中间改一个词各一例
        body = {"messages": [
            {"role": "user", "content": _NUDGE_TEXT_ORIG + " extra"},
            {"role": "user", "content": _NUDGE_TEXT_ORIG.replace("visible", "invisible")},
        ]}
        self.assertFalse(_rewrite_known_injected_texts(body))
        self.assertEqual(body["messages"][0]["content"], _NUDGE_TEXT_ORIG + " extra")
        self.assertEqual(body["messages"][1]["content"],
                         _NUDGE_TEXT_ORIG.replace("visible", "invisible"))

    def test_empty_messages_no_rewrite(self):
        body = {"messages": []}
        self.assertFalse(_rewrite_known_injected_texts(body))

    def test_messages_not_list_no_rewrite(self):
        body = {"messages": "not a list"}
        self.assertFalse(_rewrite_known_injected_texts(body))

    def test_mixed_messages_only_user_nudge_rewritten(self):
        # 混合消息：assistant 普通消息 + user nudge + user 普通消息
        body = {"messages": [
            {"role": "assistant", "content": "prev thinking"},
            {"role": "user", "content": _NUDGE_TEXT_ORIG},
            {"role": "user", "content": "real question"},
        ]}
        self.assertTrue(_rewrite_known_injected_texts(body))
        self.assertEqual(body["messages"][1]["content"], _NUDGE_TEXT_REWRITTEN)
        self.assertEqual(body["messages"][0]["content"], "prev thinking")
        self.assertEqual(body["messages"][2]["content"], "real question")


# ---------------------------------------------------------------------------
# 集成测试：复用 test_budget_retry.py 的 harness 驱动 _forward 全程
# ---------------------------------------------------------------------------

# 假上游响应（与 test_budget_retry 同款，仅本文件自用最小集）
class _FakeResp:
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


class _FakeConfig:
    def __init__(self, supply_map, routes_map, strategies):
        self._supply_map = supply_map
        self._routes_map = routes_map
        self._strategies = strategies

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

    def get_cooldown_rules(self):
        return []


class _FakeCooldown:
    def __init__(self):
        self.cooled = []

    def is_cooling(self, sid):
        return False

    def cooldown(self, sid, secs, reason=""):
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


_FULL_ENUM = {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}


def _supply(sid, protocol):
    url = {"anthropic": "http://up/v1/messages",
           "chat": "http://up/v1/chat/completions",
           "responses": "http://up/v1/responses"}[protocol]
    return {"id": sid, "url": url, "protocol": protocol, "appkey": "k",
            "target_model": "m1", "reasoning_capability": dict(_FULL_ENUM)}


def _make_server(supplies, tier_supplies):
    supply_map = {s["id"]: s for s in supplies}
    routes_map = {"r1": {"id": "r1", "tiers": {"opus": tier_supplies}, "failover": "on"}}
    strategies = [{"client_token": "tok", "route_id": "r1"}]
    ns = SimpleNamespace(
        config_store=_FakeConfig(supply_map, routes_map, strategies),
        cooldown_store=_FakeCooldown(), pref_store=_FakePref(),
        sidecar_store=_FakeSidecar())
    return ns


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
        "final_error": "", "attempt_errors": [],
        "nudge_rewritten": "",
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


def _sent_requests(mock):
    """返回上游收到的 Request 对象列表。"""
    return [c.args[0] for c in mock.call_args_list]


class TestNudgeRewriteForwardIntegration(unittest.TestCase):

    def test_forward_rewrites_nudge_and_content_length_matches(self):
        """含 nudge 的 anthropic 请求驱动 _forward 全程：上游收到改写后 body，
        Content-Length 与 body 长度一致。"""
        ns = _make_server([_supply("s1", "anthropic")], ["s1"])
        # 构造含 nudge 的 messages：user 普通消息 + user nudge 消息
        body = {
            "model": "claude-opus",
            "max_tokens": 4096,
            "messages": [
                {"role": "user", "content": "what is 1+1"},
                {"role": "user", "content": _NUDGE_TEXT_ORIG},
            ],
        }
        h = _make_handler(ns, body)
        upstream_resp = {"id": "m1", "type": "message", "role": "assistant",
                         "model": "m1", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "2"}],
                         "usage": {"input_tokens": 3, "output_tokens": 10}}
        m = _run(h, [_FakeResp(upstream_resp)])

        # 断言 1：nudge_rewritten 置位
        self.assertEqual(h._acc["nudge_rewritten"], "1")

        # 断言 2：上游收到的 body 中 nudge 已替换为新文案
        reqs = _sent_requests(m)
        self.assertEqual(len(reqs), 1)
        sent_body = json.loads(reqs[0].data.decode())
        self.assertEqual(sent_body["messages"][1]["content"], _NUDGE_TEXT_REWRITTEN)
        # 非命中消息保持原样
        self.assertEqual(sent_body["messages"][0]["content"], "what is 1+1")

        # 断言 3：上游收到的 Content-Length 与 body 长度一致
        # urllib.request.Request 将 header 名规范化为 Content-length（小写 l）
        sent_cl = reqs[0].get_header("Content-length")
        self.assertEqual(int(sent_cl), len(reqs[0].data))

    def test_forward_no_nudge_not_rewritten(self):
        """不含 nudge 的 anthropic 请求：nudge_rewritten 保持空，body 原样透传。"""
        ns = _make_server([_supply("s1", "anthropic")], ["s1"])
        body = {"model": "claude-opus", "max_tokens": 4096,
                "messages": [{"role": "user", "content": "hello"}]}
        h = _make_handler(ns, body)
        upstream_resp = {"id": "m1", "type": "message", "role": "assistant",
                         "model": "m1", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "hi"}],
                         "usage": {"input_tokens": 3, "output_tokens": 10}}
        m = _run(h, [_FakeResp(upstream_resp)])

        self.assertEqual(h._acc["nudge_rewritten"], "")
        reqs = _sent_requests(m)
        self.assertEqual(len(reqs), 1)
        sent_body = json.loads(reqs[0].data.decode())
        self.assertEqual(sent_body["messages"][0]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
