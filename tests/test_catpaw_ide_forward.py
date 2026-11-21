"""catpaw-ide 走 ModelProxyHandler._forward 全链路（脱网络）。

上游是 Go 桥 + pocfake 假 transport：验证 supply.protocol 分派、凭据只读、
Anthropic 流式/非流式写回；凭据/桥故障不进 cooldown；上游非 2xx 走
body_error_rules→冷却→failover（无匹配规则时回落状态码）。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.server import ModelProxyHandler  # noqa: E402
from core.thinking_guard import ThinkingStripStore  # noqa: E402

GO_DIR = ROOT / "core" / "providers" / "catpaw_ide" / "go"


class _Config:
    def __init__(self, supplies, tier, body_error_rules=None,
                 cooldown_rules=None):
        self._sm = {s["id"]: s for s in supplies}
        self._rm = {"r1": {"id": "r1", "tiers": {"opus": tier}, "failover": "on"}}
        self._be = list(body_error_rules or [])
        self._cd = list(cooldown_rules or [{"errorcode": [500, 502],
                                            "cooldown_seconds": 60}])

    def maybe_reload(self):
        return False

    def get_strategies(self):
        return [{"client_token": "tok", "route_id": "r1"}]

    def get_routes_map(self):
        return self._rm

    def get_supply_map(self):
        return self._sm

    def get_upstream_timeout(self):
        return 60

    def get_budget_retry(self):
        return {"enabled": False, "max_retries": 0}

    def get_body_error_rules(self):
        return self._be

    def get_cooldown_rules(self):
        return self._cd


class _Cooldown:
    def __init__(self):
        self.hits = []

    def snapshot(self, sid):
        return False

    def is_cooling(self, sid):
        return False

    def cooldown(self, sid, secs, reason):
        self.hits.append((sid, secs, reason))


class _Pref:
    def snapshot(self, m):
        return {}

    def learn(self, m, v):
        pass


class _Sidecar:
    def maybe_reload(self):
        return False

    def get_overrides_for(self, t):
        return {}

    def touch(self, t, s):
        pass


def _handler(supplies, tier, body, cooldown, body_error_rules=None,
             cooldown_rules=None):
    ns = SimpleNamespace(
        config_store=_Config(supplies, tier, body_error_rules, cooldown_rules),
        cooldown_store=cooldown,
        pref_store=_Pref(), sidecar_store=_Sidecar(),
        thinking_strip_store=ThinkingStripStore())
    raw = json.dumps(body).encode()
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.server = ns
    h.path = "/v1/messages"
    h.headers = {"Authorization": "Bearer tok", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h._acc = {k: v for k, v in {
        "status": 0, "source": "", "route": "", "tier": "", "supply": "",
        "failover": 0, "attempts": 0, "token": "", "usage_in": 0,
        "usage_out": 0, "strategy": "", "session": "", "route_failover": 0,
        "builtin": "", "budget_retried": "", "budget_truncated": 0,
        "stop_reason": "", "final_error": "", "attempt_errors": [],
        "body_error": "", "thinking_strip": 0}.items()}
    h._responses = []
    h._write_buffered_response = lambda s, hd, b: (
        h._acc.__setitem__("status", s), h._responses.append((s, b)))
    h.send_response = lambda s: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h._written = []

    def _sse(chunk):
        h._written.append(chunk)

    h._write_sse_chunk = _sse
    return h


def _cred(o):
    p = o / "cred.json"
    p.write_text(json.dumps({
        "accessToken": "at", "refreshToken": "rt", "attestation": "a",
        "mis": "wangfan24", "expire": int(time.time()) + 86400,
        "issuedAt": int(time.time())}))
    return str(p)


class TestIdeForward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("go") is None:
            raise unittest.SkipTest("go 不可用")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.lib = cls.tmp / "lib.dylib"
        subprocess.run(["go", "build", "-tags", "pocfake", "-buildmode",
                        "c-shared", "-o", str(cls.lib), "./cmd/native"],
                       cwd=str(GO_DIR), check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _ide_supply(self):
        return {"id": "ide1", "protocol": "catpaw-ide",
                "credential_file": _cred(self.tmp),
                "bridge_lib": str(self.lib), "target_model": "gpt-6.2-sol"}

    def test_stream_writes_anthropic_sse(self):
        cd = _Cooldown()
        h = _handler([self._ide_supply()], ["ide1"], {
            "model": "claude-opus", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, cd)
        h._forward("POST")
        text = b"".join(h._written).decode()
        for want in ("event: message_start", "event: content_block_delta",
                     "event: message_delta", "event: message_stop",
                     '"stop_reason":"end_turn"'):
            self.assertIn(want, text)
        self.assertEqual(h._acc["status"], 200)
        self.assertEqual(h._acc["supply"], "ide1")
        self.assertEqual(cd.hits, [], "正常请求不该有 cooldown")

    def test_non_stream_returns_anthropic_json(self):
        cd = _Cooldown()
        h = _handler([self._ide_supply()], ["ide1"], {
            "model": "claude-opus", "max_tokens": 64, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}, cd)
        h._forward("POST")
        status, body = h._responses[-1]
        self.assertEqual(status, 200)
        msg = json.loads(body)
        self.assertEqual(msg["content"][0]["text"], "Hello CatPaw")
        self.assertEqual(cd.hits, [])

    def test_missing_credential_500_without_cooldown(self):
        cd = _Cooldown()
        sup = self._ide_supply()
        sup["credential_file"] = str(self.tmp / "absent.json")
        h = _handler([sup], ["ide1"], {
            "model": "claude-opus", "max_tokens": 8, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, cd)
        h._forward("POST")
        status, body = h._responses[-1]
        self.assertEqual(status, 500)
        self.assertIn("凭据文件不存在", body.decode())
        self.assertEqual(cd.hits, [], "凭据问题不得进 cooldown（会误伤 failover）")
        self.assertEqual(h._acc["failover"], 0)

    def test_expired_credential_500(self):
        cd = _Cooldown()
        p = self.tmp / "expired.json"
        p.write_text(json.dumps({
            "accessToken": "at", "refreshToken": "rt", "attestation": "a",
            "mis": "m", "expire": int(time.time()) - 60, "issuedAt": 0}))
        sup = self._ide_supply()
        sup["credential_file"] = str(p)
        h = _handler([sup], ["ide1"], {
            "model": "claude-opus", "max_tokens": 8, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, cd)
        h._forward("POST")
        status, body = h._responses[-1]
        self.assertEqual(status, 500)
        self.assertIn("过期", body.decode())
        self.assertEqual(cd.hits, [])

    def test_upstream_model_missing_cooldowns_with_configured_code(self):
        """IDE 目录无此模型（本地 invalid 400）→ body_error_rules 映射成
        长冷却 errorcode 并 failover 到下一个供给，而不是直接 500。"""
        cd = _Cooldown()
        bad = self._ide_supply()
        bad["target_model"] = "gpt-6-missing"
        good = self._ide_supply()
        good["id"] = "ide2"
        good["target_model"] = "gpt-6.2-sol"
        h = _handler([bad, good], ["ide1", "ide2"], {
            "model": "claude-opus", "max_tokens": 8, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}, cd,
            body_error_rules=[{
                "pattern": "当前账号目录中没有该模型", "match": "contains",
                "errorcode": "upstream_model_unavailable", "max_text_len": 1000}],
            cooldown_rules=[{"errorcode": ["upstream_model_unavailable"],
                             "cooldown_seconds": 21600}])
        h._forward("POST")
        status, body = h._responses[-1]
        self.assertEqual(status, 200, body.decode())
        self.assertEqual(h._acc["supply"], "ide2")
        self.assertEqual(h._acc["failover"], 1)
        self.assertEqual(h._acc["body_error"], "upstream_model_unavailable")
        self.assertEqual(cd.hits,
                         [("ide1", 21600, "upstream_model_unavailable")])

    def test_upstream_error_without_rule_falls_back_to_status_cooldown(self):
        """无匹配 body 规则时保持原行为：按 400 状态码冷却后 failover。"""
        cd = _Cooldown()
        bad = self._ide_supply()
        bad["target_model"] = "gpt-6-missing"
        good = self._ide_supply()
        good["id"] = "ide2"
        good["target_model"] = "gpt-6.2-sol"
        h = _handler([bad, good], ["ide1", "ide2"], {
            "model": "claude-opus", "max_tokens": 8, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}, cd,
            cooldown_rules=[{"errorcode": [400], "cooldown_seconds": 5}])
        h._forward("POST")
        status, body = h._responses[-1]
        self.assertEqual(status, 200, body.decode())
        self.assertEqual(h._acc["supply"], "ide2")
        self.assertEqual(cd.hits, [("ide1", 5, "http_400")])

    def test_count_tokens_short_circuits_locally(self):
        cd = _Cooldown()
        h = _handler([self._ide_supply()], ["ide1"], {
            "model": "claude-opus",
            "messages": [{"role": "user", "content": "hi"}]}, cd)
        h.path = "/v1/messages/count_tokens"
        h._forward("POST")
        status, body = h._responses[-1]
        self.assertEqual(status, 404)
        self.assertIn("catpaw-ide", body.decode())


if __name__ == "__main__":
    unittest.main()
