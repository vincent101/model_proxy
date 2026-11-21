"""catpaw-cli provider：内置常量 + 与 legacy 路径的等价性。

C1 退出门（设计记录 §4.3）：同一 supply 在「全内联的 legacy」与
「protocol: catpaw-cli 全内置」下，出站 body/headers/状态/事件/冷却逐项一致。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.providers import catpaw_cli as C  # noqa: E402
from core.providers import http_compat as H  # noqa: E402
from core import server as S  # noqa: E402
from core.thinking_guard import ThinkingStripStore  # noqa: E402


class TestBuiltinConstants(unittest.TestCase):
    def test_endpoint(self):
        self.assertEqual(C.ENDPOINT, "https://mcli.sankuai.com/v1/messages")

    def test_credential_is_keeper_managed_file(self):
        """与 keeper 的 MCLI_CONFIG 必须是同一个文件（keeper 代管、proxy 只读）。"""
        self.assertEqual(C.CREDENTIAL_FILE, "~/.config/mcopilot-cli/.config.yaml")

    def test_extra_headers(self):
        self.assertEqual(C.EXTRA_HEADERS, {"X-Working-Dir": "/"})

    def test_system_inject_shape(self):
        self.assertTrue(C.SYSTEM_INJECT.startswith("x-anthropic-billing-header:"))
        self.assertIn("cc_version=", C.SYSTEM_INJECT)

    def test_wire_protocol(self):
        self.assertEqual(C.WIRE_PROTOCOL, "anthropic")


class TestValidate(unittest.TestCase):
    def test_minimal_ok(self):
        C.validate_supply({"id": "x", "protocol": "catpaw-cli", "target_model": "m"})

    def test_forbidden_fields_rejected(self):
        for field, val in [("url", "http://a"), ("appkey", "k"),
                           ("appkey_file", "/f"), ("system_inject", "s"),
                           ("credential_file", "/c"), ("bridge_lib", "/l")]:
            with self.assertRaises(C.ConfigError, msg=field):
                C.validate_supply({"id": "x", "protocol": "catpaw-cli",
                                   "target_model": "m", field: val})

    def test_target_model_required(self):
        with self.assertRaises(C.ConfigError):
            C.target_model_of({"id": "x"})


class TestMaterialize(unittest.TestCase):
    def test_fills_builtin_fields(self):
        out = C.materialize({"id": "x", "protocol": "catpaw-cli", "target_model": "m"})
        self.assertEqual(out["url"], C.ENDPOINT)
        self.assertEqual(out["appkey_file"], C.CREDENTIAL_FILE)
        self.assertEqual(out["extra_headers"], C.EXTRA_HEADERS)
        self.assertEqual(out["system_inject"], C.SYSTEM_INJECT)
        self.assertEqual(out["protocol"], "anthropic", "应转为线协议供下游使用")

    def test_does_not_mutate_input(self):
        src = {"id": "x", "protocol": "catpaw-cli", "target_model": "m"}
        C.materialize(src)
        self.assertEqual(src["protocol"], "catpaw-cli")
        self.assertNotIn("url", src)

    def test_preserves_extra_keys(self):
        src = {"id": "x", "protocol": "catpaw-cli", "target_model": "m",
               "reasoning_capability": {"effort_enum": ["low"]}}
        out = C.materialize(src)
        self.assertEqual(out["reasoning_capability"], {"effort_enum": ["low"]})


class TestHeaderEquivalence(unittest.TestCase):
    """CLI 物化后的出站 header 必须与手工全内联的 legacy supply 完全相同。"""

    def test_headers_identical(self):
        tmp = tempfile.mkdtemp()
        cred = os.path.join(tmp, "cfg.yaml")
        with open(cred, "w") as f:
            f.write("AUTHORIZATION: tok-abc\n")

        legacy = {
            "id": "inline", "url": C.ENDPOINT, "protocol": "anthropic",
            "appkey_file": cred, "target_model": "m",
            "extra_headers": dict(C.EXTRA_HEADERS),
            "system_inject": C.SYSTEM_INJECT,
        }
        cli = C.materialize({"id": "builtin", "protocol": "catpaw-cli",
                             "target_model": "m"})
        # 用同一份「实际凭据文件」对比：把 cli 的路径指向临时文件
        cli["appkey_file"] = cred

        client_headers = {"Content-Type": "application/json", "User-Agent": "cc/1.0",
                          "Authorization": "Bearer client", "Host": "h"}
        a = H.build_standard_headers(client_headers, H.resolve_appkey(legacy), 100,
                                     converted=True,
                                     extra_headers=legacy.get("extra_headers"))
        b = H.build_standard_headers(client_headers, H.resolve_appkey(cli), 100,
                                     converted=True,
                                     extra_headers=cli.get("extra_headers"))
        self.assertEqual(a, b)

    def test_system_inject_idempotent_same(self):
        legacy_body = {"system": [{"type": "text", "text": "orig"}]}
        cli_body = {"system": [{"type": "text", "text": "orig"}]}
        H.inject_system_block(legacy_body, C.SYSTEM_INJECT)
        H.inject_system_block(cli_body, C.SYSTEM_INJECT)
        self.assertEqual(legacy_body, cli_body)


class TestDetectTarget(unittest.TestCase):
    def test_cli_maps_to_anthropic(self):
        self.assertEqual(S.detect_target({"id": "x", "protocol": "catpaw-cli",
                                          "target_model": "m"}), "anthropic")

    def test_legacy_unaffected(self):
        self.assertEqual(S.detect_target({"id": "a", "url": "https://a/v1/responses"}),
                         "responses")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# C1 端到端对拍：同一 supply 的全内联 legacy vs 全内置 catpaw-cli
# ---------------------------------------------------------------------------
class _Cfg:
    def __init__(self, sup, tier):
        self._sm = {sup["id"]: sup}
        self._rm = {"r1": {"id": "r1", "tiers": {"opus": tier}, "failover": "off"}}

    def maybe_reload(self): return False
    def get_strategies(self): return [{"client_token": "tok", "route_id": "r1"}]
    def get_routes_map(self): return self._rm
    def get_supply_map(self): return self._sm
    def get_upstream_timeout(self): return 60
    def get_budget_retry(self): return {"enabled": False, "max_retries": 0}
    def get_body_error_rules(self): return []
    def get_cooldown_rules(self): return []


class _CD:
    def __init__(self): self.hits = []
    def snapshot(self, s): return False
    def is_cooling(self, s): return False
    def cooldown(self, s, sec, r): self.hits.append((s, sec, r))


def _run_forward(supply):
    """驱动 _forward，捕获实际出站请求。"""
    from unittest.mock import patch
    body = {"model": "claude-opus", "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]}
    raw = json.dumps(body).encode()
    ns = SimpleNamespace(
        config_store=_Cfg(supply, [supply["id"]]), cooldown_store=_CD(),
        pref_store=SimpleNamespace(snapshot=lambda m: {}),
        sidecar_store=SimpleNamespace(maybe_reload=lambda: False,
                                      get_overrides_for=lambda t: {},
                                      touch=lambda t, s: None),
        thinking_strip_store=ThinkingStripStore())
    h = S.ModelProxyHandler.__new__(S.ModelProxyHandler)
    h.server, h.path = ns, "/v1/messages"
    h.headers = {"Authorization": "Bearer tok", "Content-Type": "application/json",
                 "Content-Length": str(len(raw))}
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    h._acc = {k: v for k, v in {
        "status": 0, "source": "", "route": "", "tier": "", "supply": "",
        "failover": 0, "attempts": 0, "token": "", "usage_in": 0, "usage_out": 0,
        "strategy": "", "session": "", "route_failover": 0, "builtin": "",
        "budget_retried": "", "budget_truncated": 0, "stop_reason": "",
        "final_error": "", "attempt_errors": [], "body_error": "",
        "thinking_strip": 0}.items()}
    h._responses = []
    h._write_buffered_response = lambda st, hd, b: (
        h._acc.__setitem__("status", st), h._responses.append((st, b)))
    for attr in ("send_response", "send_header", "end_headers"):
        setattr(h, attr, lambda *a, **k: None)

    captured = {}

    class _Resp:
        status = 200
        def getheaders(self): return [("Content-Type", "application/json")]
        def read(self, n=-1):
            return json.dumps({"id": "m", "type": "message", "role": "assistant",
                               "content": [{"type": "text", "text": "ok"}],
                               "stop_reason": "end_turn",
                               "usage": {"input_tokens": 5, "output_tokens": 1}}).encode()
        def close(self): pass

    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data
        return _Resp()

    with patch("core.server.urllib.request.urlopen", side_effect=fake):
        h._forward("POST")
    return captured, h._acc


class TestCliEqualsLegacy(unittest.TestCase):
    """C1 退出门：出站 URL / header / body / 状态 / usage 逐项一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cred = os.path.join(self.tmp, "cfg.yaml")
        with open(self.cred, "w") as f:
            f.write("AUTHORIZATION: tok-from-keeper\n")

    def _legacy_supply(self):
        return {"id": "inline", "url": C.ENDPOINT, "protocol": "anthropic",
                "appkey_file": self.cred, "target_model": "claude-opus-5.5",
                "extra_headers": dict(C.EXTRA_HEADERS),
                "system_inject": C.SYSTEM_INJECT}

    def _cli_supply(self):
        sup = C.materialize({"id": "builtin", "protocol": "catpaw-cli",
                             "target_model": "claude-opus-5.5"})
        sup["appkey_file"] = self.cred   # 指向临时文件，不碰真实凭据
        return sup

    def test_equivalent(self):
        a_req, a_acc = _run_forward(self._legacy_supply())
        b_req, b_acc = _run_forward(self._cli_supply())
        self.assertEqual(a_req["url"], b_req["url"], "URL")
        self.assertEqual({k.lower(): v for k, v in a_req["headers"].items()},
                         {k.lower(): v for k, v in b_req["headers"].items()}, "headers")
        self.assertEqual(json.loads(a_req["body"]), json.loads(b_req["body"]), "body")
        self.assertEqual(a_acc["status"], b_acc["status"], "status")
        self.assertEqual((a_acc["usage_in"], a_acc["usage_out"]),
                         (b_acc["usage_in"], b_acc["usage_out"]), "usage")

    def test_cli_uses_builtin_endpoint(self):
        req, _ = _run_forward(self._cli_supply())
        self.assertEqual(req["url"], C.ENDPOINT)

    def test_cli_injects_billing_header(self):
        req, _ = _run_forward(self._cli_supply())
        body = json.loads(req["body"])
        texts = [b.get("text", "") for b in body.get("system", []) if isinstance(b, dict)]
        self.assertTrue(any(C.SYSTEM_INJECT in t for t in texts),
                        "system_inject 必须已注入")
