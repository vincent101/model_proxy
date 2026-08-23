"""cooldown 策略组单层全局架构专项测试（脱网络）。

覆盖：
1. 402 命中策略 → failover + cooldown 21600s
2. URLError 命中策略 → failover + cooldown
3. 未命中 code（如 418）→ 透传 + log warning + 不冷却不 failover
4. URLError 未配策略 → 透传 502 + 告警
5. unconfigured_hits 计数 + status 暴露
6. 多条策略组命中同 code → 首条优先
7. 无 cooldown_rules 配置 → 所有 code 透传+告警
8. 校验器: rule 缺 cooldown_seconds → 跳过+warning
9. resolve_cooldown_seconds 纯函数单测

运行：cd tools/model_proxy && python3 -m unittest tests.test_cooldown_rules -v
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

from core import translate as pt  # noqa: E402
from core.server import (  # noqa: E402
    ModelProxyHandler,
    resolve_cooldown_seconds,
    _record_unconfigured,
    _snapshot_unconfigured_hits,
    _unconfigured_hits,
)


# ---------------------------------------------------------------------------
# 假上游 / 假配置
# ---------------------------------------------------------------------------

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


def _http_error(status, body: bytes):
    return urllib.error.HTTPError("http://up", status, "err", {}, io.BytesIO(body))


_DEFAULT_RULES = [
    {"errorcode": [401, 403, 429, 500, 502, 503, 504], "cooldown_seconds": 60},
    {"errorcode": [402], "cooldown_seconds": 21600},
    {"errorcode": ["URLError"], "cooldown_seconds": 60},
]


class _FakeConfig:
    def __init__(self, supply_map, routes_map, strategies, rules=None):
        self._supply_map = supply_map
        self._routes_map = routes_map
        self._strategies = strategies
        self._rules = rules if rules is not None else _DEFAULT_RULES

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
        return list(self._rules)


class _FakeCooldown:
    def __init__(self):
        self.cooled = []
        self.cooled_secs = []
        self.cooled_reasons = []

    def is_cooling(self, sid):
        return False

    def cooldown(self, sid, secs, reason=""):
        self.cooled.append(sid)
        self.cooled_secs.append(secs)
        self.cooled_reasons.append(reason)

    def snapshot(self):
        return {}

    def clear_all(self):
        self.cooled.clear()
        self.cooled_secs.clear()
        self.cooled_reasons.clear()


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


_FULL_ENUM = {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}


def _supply(sid, protocol="anthropic"):
    return {"id": sid, "url": "http://up/v1/messages", "protocol": protocol,
            "appkey": "k", "target_model": "m1", "reasoning_capability": dict(_FULL_ENUM)}


def _make_server(supplies, tier_supplies, rules=None, failover="on"):
    supply_map = {s["id"]: s for s in supplies}
    routes_map = {"r1": {"id": "r1", "tiers": {"opus": tier_supplies}, "failover": failover}}
    strategies = [{"client_token": "tok", "route_id": "r1"}]
    cd = _FakeCooldown()
    ns = SimpleNamespace(
        config_store=_FakeConfig(supply_map, routes_map, strategies, rules),
        cooldown_store=cd, pref_store=_FakePref(), sidecar_store=_FakeSidecar())
    return ns, cd


def _make_handler(server_ns, body=None):
    body = body or {"model": "claude-opus", "max_tokens": 100, "messages": [
        {"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.server = server_ns
    h.path = "/v1/messages"
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
    # 重置 rfile 指针到开头（同一 handler 多次 _run 时第一次已读完）
    h.rfile.seek(0)
    # 重置 wfile（避免累积输出）
    h.wfile = io.BytesIO()
    with patch("core.server.urllib.request.urlopen",
               side_effect=list(upstream_queue)) as m:
        h._forward("POST")
    return m


# ---------------------------------------------------------------------------
# 1. 402 命中策略 → failover + cooldown 21600s
# ---------------------------------------------------------------------------

class TestExhaustedFinalStatus(unittest.TestCase):
    def test_http_only_exhaustion_stays_503(self):
        ns, _ = _make_server([_supply("s1")], ["s1"])
        h = _make_handler(ns)
        _run(h, [_http_error(429, b'{"error":{"message":"busy"}}')])
        self.assertEqual(h._responses[-1][0], 503)

    def test_empty_stream_commits_without_failover(self):
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        body = {"model": "claude-opus", "max_tokens": 100, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        h = _make_handler(ns, body)
        m = _run(h, [_FakeResp(b"")])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["response_committed"], 1)
        self.assertEqual(h._acc["stream_integrity"], "invalid")
        self.assertEqual(h._acc["terminal_reason"], "empty_stream")
        self.assertEqual(cd.cooled, [])


class Test402CooldownFailover(unittest.TestCase):

    def test_402_triggers_failover_and_cooldown_21600(self):
        """402 → failover 到 s2 + s1 冷却 21600s。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns)
        good = _FakeResp({"type": "message", "content": [{"type": "text", "text": "ok"}],
                          "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
        m = _run(h, [_http_error(402, b'{"error":{"message":"quota"}}'), good])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(cd.cooled, ["s1"])
        self.assertEqual(cd.cooled_secs, [21600])
        self.assertEqual(cd.cooled_reasons, ["http_402"])
        self.assertEqual(h._acc["status"], 200)


# ---------------------------------------------------------------------------
# 2. URLError 命中策略 → failover + cooldown
# ---------------------------------------------------------------------------

class TestURLErrorCooldownFailover(unittest.TestCase):

    def test_urlerror_triggers_failover_and_cooldown(self):
        """URLError → failover 到 s2 + s1 冷却 60s。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns)
        good = _FakeResp({"type": "message", "content": [{"type": "text", "text": "ok"}],
                          "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
        m = _run(h, [urllib.error.URLError("conn refused"), good])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(cd.cooled, ["s1"])
        self.assertEqual(cd.cooled_secs, [60])
        self.assertTrue(cd.cooled_reasons[0].startswith("net_error:"))


# ---------------------------------------------------------------------------
# 3. 未命中 code → 透传 + 不冷却不 failover
# ---------------------------------------------------------------------------

class TestUnconfiguredPassthrough(unittest.TestCase):

    def test_418_passthrough_no_cooldown_no_failover(self):
        """418 不在 cooldown_rules → 透传 418 + 不冷却不 failover。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"])
        h = _make_handler(ns)
        m = _run(h, [_http_error(418, b'{"error":{"message":"teapot"}}')])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._acc["status"], 418)


# ---------------------------------------------------------------------------
# 4. URLError 未配策略 → 透传 502 + 告警
# ---------------------------------------------------------------------------

class TestURLErrorNoRule(unittest.TestCase):

    def test_urlerror_no_rule_passes_502(self):
        """无 URLError 策略 → 透传 502 + 不冷却不 failover。"""
        rules_no_urlerror = [
            {"errorcode": [401, 403, 429, 500, 502, 503, 504], "cooldown_seconds": 60},
            {"errorcode": [402], "cooldown_seconds": 21600},
        ]
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"], rules=rules_no_urlerror)
        h = _make_handler(ns)
        m = _run(h, [urllib.error.URLError("conn refused")])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._acc["status"], 502)


# ---------------------------------------------------------------------------
# 5. unconfigured_hits 计数 + status 暴露
# ---------------------------------------------------------------------------

class TestUnconfiguredHits(unittest.TestCase):

    def setUp(self):
        """每个测试前清零全局计数器。"""
        _unconfigured_hits.clear()

    def test_402_three_times_then_status_shows(self):
        """撞 402 三次（402 在策略中但此处用无策略配置使其 unconfigured）→ status 显示 {"402":3}。"""
        # 用空 rules 使 402 变成 unconfigured
        ns, cd = _make_server([_supply("s1")], ["s1"], rules=[])
        h = _make_handler(ns)
        _run(h, [_http_error(402, b'{"error":{"message":"quota"}}')])
        _run(h, [_http_error(402, b'{"error":{"message":"quota"}}')])
        _run(h, [_http_error(402, b'{"error":{"message":"quota"}}')])
        snap = _snapshot_unconfigured_hits()
        self.assertEqual(snap.get("402"), 3)

    def test_unconfigured_mixed_codes(self):
        """撞 402 和 URLError → status 显示两种 code 计数。"""
        ns, cd = _make_server([_supply("s1")], ["s1"], rules=[])
        h = _make_handler(ns)
        _run(h, [_http_error(402, b'err')])
        _run(h, [urllib.error.URLError("timeout")])
        _run(h, [_http_error(418, b'err')])
        snap = _snapshot_unconfigured_hits()
        self.assertEqual(snap.get("402"), 1)
        self.assertEqual(snap.get("URLError"), 1)
        self.assertEqual(snap.get("418"), 1)


# ---------------------------------------------------------------------------
# 6. 多条策略组命中同 code → 首条优先
# ---------------------------------------------------------------------------

class TestFirstMatchWins(unittest.TestCase):

    def test_first_match_wins(self):
        """同 code 出现在多条策略组 → 首条（配置顺序优先）的 cooldown_seconds 生效。"""
        rules = [
            {"errorcode": [402], "cooldown_seconds": 99},
            {"errorcode": [402], "cooldown_seconds": 21600},
        ]
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"], rules=rules)
        h = _make_handler(ns)
        good = _FakeResp({"type": "message", "content": [{"type": "text", "text": "ok"}],
                          "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
        _run(h, [_http_error(402, b'err'), good])
        self.assertEqual(cd.cooled_secs, [99])


# ---------------------------------------------------------------------------
# 7. 无 cooldown_rules 配置 → 所有 code 透传+告警
# ---------------------------------------------------------------------------

class TestNoRulesAllPassthrough(unittest.TestCase):

    def setUp(self):
        _unconfigured_hits.clear()

    def test_no_rules_500_passthrough(self):
        """无 cooldown_rules → 500 透传 + 不冷却不 failover。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"], rules=[])
        h = _make_handler(ns)
        m = _run(h, [_http_error(500, b'{"error":{"message":"boom"}}')])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._acc["status"], 500)

    def test_no_rules_402_passthrough(self):
        """无 cooldown_rules → 402 透传 + 不冷却不 failover。"""
        ns, cd = _make_server([_supply("s1"), _supply("s2")], ["s1", "s2"], rules=[])
        h = _make_handler(ns)
        _run(h, [_http_error(402, b'{"error":{"message":"quota"}}')])
        self.assertEqual(h._acc["failover"], 0)
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._acc["status"], 402)


# ---------------------------------------------------------------------------
# 8. resolve_cooldown_seconds 纯函数单测
# ---------------------------------------------------------------------------

class _FakeCS:
    """最小 ConfigStore mock for resolve_cooldown_seconds。"""
    def __init__(self, rules):
        self._rules = rules

    def get_cooldown_rules(self):
        return list(self._rules)


class TestResolveCooldownSeconds(unittest.TestCase):

    def test_int_code_hit(self):
        cs = _FakeCS([{"errorcode": [401, 403], "cooldown_seconds": 60}])
        self.assertEqual(resolve_cooldown_seconds(403, cs), 60)

    def test_int_code_miss(self):
        cs = _FakeCS([{"errorcode": [401, 403], "cooldown_seconds": 60}])
        self.assertIsNone(resolve_cooldown_seconds(418, cs))

    def test_urlerror_hit(self):
        cs = _FakeCS([{"errorcode": ["URLError"], "cooldown_seconds": 30}])
        self.assertEqual(resolve_cooldown_seconds("URLError", cs), 30)

    def test_urlerror_miss(self):
        cs = _FakeCS([{"errorcode": [401], "cooldown_seconds": 60}])
        self.assertIsNone(resolve_cooldown_seconds("URLError", cs))

    def test_empty_rules(self):
        cs = _FakeCS([])
        self.assertIsNone(resolve_cooldown_seconds(500, cs))
        self.assertIsNone(resolve_cooldown_seconds("URLError", cs))

    def test_first_match_wins(self):
        cs = _FakeCS([
            {"errorcode": [402], "cooldown_seconds": 99},
            {"errorcode": [402], "cooldown_seconds": 21600},
        ])
        self.assertEqual(resolve_cooldown_seconds(402, cs), 99)


if __name__ == "__main__":
    unittest.main()
