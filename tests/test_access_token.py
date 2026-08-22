"""ACCESS 日志 token 字段：记完整 client_token，不截断。

背景：client_token 只是路由查表键（无密钥校验语义），此前 ACCESS 记尾 4 位
（"codex"→"odex"），在 status 跨协议链路段产生不存在的名字。改为记全名。
旧日志行的尾 4 位值不迁移（历史行滚动消失，见 server.py 改动处注释）。
"""

import io
import json
import unittest
from types import SimpleNamespace

from core.server import ModelProxyHandler


class _FakeConfig:
    def __init__(self):
        self.strategies = [{"client_token": "tok", "route_id": "r1"}]
        self.routes_map = {"r1": {"id": "r1", "tiers": {"opus": ["s1"]}, "failover": "on"}}

    def maybe_reload(self):
        pass

    def get_strategies(self):
        return self.strategies

    def get_routes_map(self):
        return self.routes_map

    def get_supply_map(self):
        return {"s1": {"id": "s1", "url": "http://up/v1/messages", "protocol": "anthropic",
                       "appkey": "k", "target_model": "m1"}}

    def get_upstream_timeout(self):
        return 1800

    def get_budget_retry(self):
        return {"enabled": False, "max_retries": 0}

    def get_cooldown_rules(self):
        return []


class _FakeStore:
    def __getattr__(self, name):
        return lambda *a, **k: {} if name.startswith("get") else None


def _make_handler(auth_header):
    raw = json.dumps({"model": "claude-opus", "max_tokens": 10, "messages": [
        {"role": "user", "content": "hi"}]}).encode()
    headers = {"Content-Length": str(len(raw))}
    if auth_header:
        headers.update(auth_header)
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.server = SimpleNamespace(config_store=_FakeConfig(), cooldown_store=_FakeStore(),
                               pref_store=_FakeStore(), sidecar_store=_FakeStore())
    h.path = "/v1/messages"
    h.headers = headers
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h._responses = []

    def _write(status, resp_headers, body_bytes):
        h._acc["status"] = status
        h._responses.append((status, body_bytes))

    h._write_buffered_response = _write
    return h


class AccessTokenLoggingTest(unittest.TestCase):
    def _access_record(self, h):
        """跑 _forward_logged 并返回 ACCESS 日志 record（msg % args 后的 message）。"""
        with self.assertLogs("model_proxy.access", level="INFO") as cap:
            h._forward_logged("POST")
        recs = [r for r in cap.records if "ACCESS " in (r.getMessage() or "")]
        self.assertEqual(len(recs), 1)
        return recs[0].getMessage()

    def test_long_token_recorded_in_full(self):
        """token=XTEST123 → ACCESS 行 token=XTEST123（不截成尾4位）。"""
        h = _make_handler({"Authorization": "Bearer XTEST123"})
        self.assertIn("token=XTEST123 ", self._access_record(h))
        self.assertEqual(h._acc["token"], "XTEST123")

    def test_short_token_not_mangled(self):
        """token=codex → ACCESS 行 token=codex（旧逻辑会显示 odex）。"""
        h = _make_handler({"x-api-key": "codex"})
        self.assertIn("token=codex ", self._access_record(h))
        self.assertEqual(h._acc["token"], "codex")

    def test_empty_token_records_empty(self):
        """无鉴权头 → ACCESS 行 token= 空串（401 路径同样记录）。"""
        h = _make_handler(None)
        self.assertIn("token= ", self._access_record(h))
        self.assertEqual(h._acc["token"], "")
        self.assertEqual(h._responses[0][0], 401)


if __name__ == "__main__":
    unittest.main()
