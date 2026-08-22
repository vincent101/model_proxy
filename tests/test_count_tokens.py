import io
import json
import os
import sys
import unittest
import urllib.error
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import ModelProxyHandler, build_target_url, detect_operation, detect_source


class FakeResponse:
    status = 200
    def __init__(self, body):
        self.body = body
    def read(self): return self.body
    def getheaders(self): return [("Content-Type", "application/json")]
    def close(self): pass


class Config:
    def __init__(self, supplies, failover="on"):
        self.supplies = {s["id"]: s for s in supplies}
        self.route = {"id": "r1", "tiers": {"opus": [s["id"] for s in supplies]}, "failover": failover}
    def maybe_reload(self): pass
    def get_strategies(self): return [{"client_token": "tok", "route_id": "r1"}]
    def get_routes_map(self): return {"r1": self.route}
    def get_supply_map(self): return self.supplies
    def get_upstream_timeout(self): return 10
    def get_cooldown_rules(self): return getattr(self, "rules", [])


class Cooldown:
    def __init__(self): self.calls = []
    def is_cooling(self, sid): return False
    def cooldown(self, *args): self.calls.append(args)


class Sidecar:
    def maybe_reload(self): pass
    def get_overrides_for(self, token): return {}
    def touch(self, *args): pass


class TestCountTokens(unittest.TestCase):
    def test_detection_and_url(self):
        path = "/V1/Messages/Count_Tokens/?beta=x&foo=1"
        self.assertEqual(detect_operation(path), "count_tokens")
        self.assertEqual(detect_source(path, {"messages": []}), "anthropic")
        self.assertEqual(build_target_url("https://x/v1/messages/", path, "count_tokens"),
                         "https://x/v1/messages/count_tokens?foo=1")
        with self.assertRaises(ValueError):
            build_target_url("https://x/v1/messages/count_tokens", path, "count_tokens")

    def _handler(self, supplies, body=None, failover="on"):
        body = body or {"model": "claude-opus", "messages": [{"role": "user", "content": "$route x"}], "stream": True}
        raw = json.dumps(body).encode()
        cd = Cooldown()
        h = ModelProxyHandler.__new__(ModelProxyHandler)
        h.server = SimpleNamespace(config_store=Config(supplies, failover), cooldown_store=cd,
                                   pref_store=SimpleNamespace(), sidecar_store=Sidecar())
        h.path = "/v1/messages/count_tokens"
        h.headers = {"Authorization": "Bearer tok", "Content-Length": str(len(raw))}
        h.rfile = io.BytesIO(raw)
        h._acc = {"status": 0, "source": "", "operation": "", "route": "", "tier": "", "supply": "",
                  "target_protocol": "", "conversion_kind": "", "failover": 0, "attempts": 0,
                  "token": "", "usage_in": 0, "usage_out": 0, "strategy": "", "session": "",
                  "route_failover": 0, "builtin": "", "budget_retried": "", "budget_truncated": 0,
                  "stop_reason": "", "final_error": "", "attempt_errors": [], "nudge_rewritten": ""}
        h.responses = []
        h._write_buffered_response = lambda status, headers, data: (h._acc.update(status=status), h.responses.append((status, headers, data)))
        return h, cd

    def test_skips_incompatible_and_returns_buffered_valid_body(self):
        supplies = [
            {"id": "bad", "protocol": "responses", "url": "https://x/v1/responses", "appkey": "k"},
            {"id": "good", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k", "target_model": "m"},
        ]
        h, cd = self._handler(supplies)
        with patch("core.server.urllib.request.urlopen", return_value=FakeResponse(b'{"input_tokens":3}')) as call:
            h._forward("POST")
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(h._acc["attempts"], 1)
        self.assertEqual(h._acc["supply"], "good")
        self.assertEqual(cd.calls, [])
        sent = json.loads(call.call_args.args[0].data)
        self.assertNotIn("stream", sent)
        self.assertEqual(sent["model"], "m")

    @staticmethod
    def _http_error(status, body, content_type="application/json"):
        headers = Message()
        headers["Content-Type"] = content_type
        return urllib.error.HTTPError("https://up", status, "err", headers, io.BytesIO(body))

    def test_valid_anthropic_error_preserved(self):
        supply = {"id": "s", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k"}
        body = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow"}}'
        h, _ = self._handler([supply], failover="off")
        with patch("core.server.urllib.request.urlopen",
                   side_effect=self._http_error(429, body, "application/problem+json")):
            h._forward("POST")
        status, headers, actual = h.responses[0]
        self.assertEqual((status, actual), (429, body))
        self.assertIn(("Content-Type", "application/problem+json"), headers)

    def test_invalid_and_empty_error_wrapped(self):
        supply = {"id": "s", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k"}
        for raw in (b"", b'{"error":"bad"}'):
            h, _ = self._handler([supply], failover="off")
            with patch("core.server.urllib.request.urlopen",
                       side_effect=self._http_error(500, raw)):
                h._forward("POST")
            parsed = json.loads(h.responses[0][2])
            self.assertEqual(h.responses[0][0], 500)
            self.assertEqual(parsed["type"], "error")
            self.assertEqual(parsed["error"]["type"], "api_error")

    def test_http_error_failover_exhaustion_returns_503(self):
        supplies = [
            {"id": "s1", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k"},
            {"id": "s2", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k"},
        ]
        h, cd = self._handler(supplies)
        h.server.config_store.rules = [{"errorcode": [429], "cooldown_seconds": 60}]
        errors = [self._http_error(429, b"{}"), self._http_error(429, b"{}")]
        with patch("core.server.urllib.request.urlopen", side_effect=errors):
            h._forward("POST")
        self.assertEqual(h.responses[0][0], 503)
        self.assertEqual(h._acc["attempts"], 2)
        self.assertEqual(len(cd.calls), 2)

    def test_failover_on_all_incompatible_exhausted_501(self):
        supplies = [
            {"id": "r", "protocol": "responses", "url": "https://x/v1/responses", "appkey": "k"},
            {"id": "c", "protocol": "chat", "url": "https://x/chat/completions", "appkey": "k"},
        ]
        h, cd = self._handler(supplies, failover="on")
        with patch("core.server.urllib.request.urlopen") as call:
            h._forward("POST")
        self.assertEqual(h.responses[0][0], 501)
        self.assertEqual(h._acc["attempts"], 0)
        call.assert_not_called()
        self.assertEqual(cd.calls, [])

    def test_count_tokens_does_not_rewrite_nudge(self):
        supply = {"id": "s", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k"}
        nudge = "[Your previous response had no visible output. Please continue and produce a user-visible response.]"
        body = {"model": "claude-opus", "messages": [{"role": "user", "content": nudge}]}
        h, _ = self._handler([supply], body=body)
        with patch("core.server.urllib.request.urlopen", return_value=FakeResponse(b'{"input_tokens":3}')) as call:
            h._forward("POST")
        self.assertEqual(h._acc["nudge_rewritten"], "")
        self.assertEqual(json.loads(call.call_args.args[0].data)["messages"][0]["content"], nudge)

    def test_invalid_success_values_are_502(self):
        supply = {"id": "s", "protocol": "anthropic", "url": "https://x/v1/messages", "appkey": "k"}
        for value in (-1, 1.5, "1", True, None):
            h, _ = self._handler([supply])
            body = json.dumps({"input_tokens": value}).encode() if value is not None else b"{}"
            with patch("core.server.urllib.request.urlopen", return_value=FakeResponse(body)):
                h._forward("POST")
            self.assertEqual(h.responses[0][0], 502, value)

    def test_failover_off_incompatible_is_501_without_attempt(self):
        supply = {"id": "bad", "protocol": "responses", "url": "https://x/v1/responses", "appkey": "k"}
        h, cd = self._handler([supply], failover="off")
        with patch("core.server.urllib.request.urlopen") as call:
            h._forward("POST")
        self.assertEqual(h.responses[0][0], 501)
        self.assertEqual(h._acc["attempts"], 0)
        self.assertEqual(h._acc["supply"], "")
        call.assert_not_called()
        self.assertEqual(cd.calls, [])


if __name__ == "__main__": unittest.main()
