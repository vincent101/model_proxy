"""supply 层扩展字段（extra_headers / system_inject / appkey_file）单测。

设计记录：docs/designs/2026-09-20-model_proxy接入mcli的supply层扩展.md
覆盖（§5）：
- inject_system_block 合并矩阵全形态（缺省/空数组/字符串/非空数组/畸形首块）+ 幂等
- validate_supply_ext：extra_headers 禁键/值校验、system_inject 协议限定、
  appkey/appkey_file 互斥与缺一
- resolve_appkey：直取/文件命中/未命中/空值/文件缺失/~ 展开/均缺空串
- 端到端（patch urlopen 驱动 _forward）：extra_headers 注入与最高优先覆盖
  （含转换路径 Content-Type）、content-length 运行时跳过、system_inject 三个
  注入点（PASSTHROUGH / RESPONSES_TO_ANTHROPIC / count_tokens）、appkey_file
  成功与失败 500 不冷却、budget_retry 重试不重复注入
- 配置校验双轨：_validate_config 启动路径 raise（fail-fast）、热重载路径
  warning 不崩（_reload_locked 吞异常，已知降级）

运行：cd tools/model_proxy && python3 -m pytest tests/test_supply_ext.py -q
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
    inject_system_block,
    resolve_appkey,
    validate_supply_ext,
)

INJECT = "x-anthropic-billing-header: cc_version=2.1.196; cc_entrypoint=sdk-cli; cch=00000;"


# ---------------------------------------------------------------------------
# 假上游 / 假配置（沿用 test_budget_retry 模式）
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status=200):
        self.status = status
        self._data = payload if isinstance(payload, (bytes, bytearray)) \
            else json.dumps(payload).encode()
        self._read = False

    def read(self, n=-1):
        if self._read:
            return b""
        self._read = True
        return self._data

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def close(self):
        pass


def _http_error(status, body: bytes):
    return urllib.error.HTTPError("http://up", status, "err", {}, io.BytesIO(body))


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
        return [
            {"errorcode": [401, 403, 429, 500, 502, 503, 504], "cooldown_seconds": 60},
            {"errorcode": ["URLError"], "cooldown_seconds": 60},
        ]


class _FakeCooldown:
    def __init__(self):
        self.cooled = []

    def is_cooling(self, sid):
        return False

    def cooldown(self, sid, secs, reason=""):
        self.cooled.append(sid)


class _FakePref:
    def snapshot(self, model):
        return {}

    def learn(self, model, variant):
        pass


class _FakeSidecar:
    def maybe_reload(self):
        return False

    def get_overrides_for(self, token):
        return {}

    def touch(self, token, session):
        pass


def _supply(sid="s1", protocol="anthropic", **over):
    url = {"anthropic": "http://up/v1/messages",
           "chat": "http://up/v1/chat/completions",
           "responses": "http://up/v1/responses"}[protocol]
    s = {"id": sid, "url": url, "protocol": protocol, "appkey": "k",
         "target_model": "m1",
         "reasoning_capability": {"effort_enum": ["low", "medium", "high", "xhigh", "max"]}}
    s.update(over)
    return s


def _anth_good():
    return {"id": "m2", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "答"}],
            "usage": {"input_tokens": 3, "output_tokens": 100}}


def _anth_truncated():
    # budget_retry 触发条件：stop=max_tokens 且正文缺失（仅 thinking）
    return {"id": "m1", "type": "message", "role": "assistant", "model": "m1",
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "思考占满预算"}],
            "usage": {"input_tokens": 3, "output_tokens": 16000}}


def _chat_good():
    return {"choices": [{"finish_reason": "stop", "message": {"content": "答"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 50}}


def _make_server(supplies, tier_supplies=None, failover="on"):
    supply_map = {s["id"]: s for s in supplies}
    routes_map = {"r1": {"id": "r1",
                         "tiers": {"opus": tier_supplies or [s["id"] for s in supplies]},
                         "failover": failover}}
    strategies = [{"client_token": "tok", "route_id": "r1"}]
    cd = _FakeCooldown()
    ns = SimpleNamespace(
        config_store=_FakeConfig(supply_map, routes_map, strategies),
        cooldown_store=cd, pref_store=_FakePref(), sidecar_store=_FakeSidecar())
    return ns, cd


def _make_handler(server_ns, body: dict, path="/v1/messages", extra_req_headers=None):
    raw = json.dumps(body).encode()
    h = ModelProxyHandler.__new__(ModelProxyHandler)
    h.server = server_ns
    h.path = path
    h.headers = {"Authorization": "Bearer tok", "Content-Length": str(len(raw))}
    h.headers.update(extra_req_headers or {})
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
    with patch("core.server.urllib.request.urlopen",
               side_effect=list(upstream_queue)) as m:
        h._forward("POST")
    return m


def _sent_bodies(mock):
    return [json.loads(c.args[0].data.decode()) for c in mock.call_args_list]


def _write_keyfile(path, lines):
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# inject_system_block：合并矩阵全形态 + 幂等
# ---------------------------------------------------------------------------

class TestInjectSystemBlock(unittest.TestCase):

    def test_missing_system(self):
        body = {"messages": []}
        self.assertTrue(inject_system_block(body, INJECT))
        self.assertEqual(body["system"], [{"type": "text", "text": INJECT}])

    def test_empty_array(self):
        body = {"system": [], "messages": []}
        self.assertTrue(inject_system_block(body, INJECT))
        self.assertEqual(body["system"], [{"type": "text", "text": INJECT}])

    def test_string_system(self):
        body = {"system": "You are helpful", "messages": []}
        self.assertTrue(inject_system_block(body, INJECT))
        self.assertEqual(body["system"], [
            {"type": "text", "text": INJECT},
            {"type": "text", "text": "You are helpful"}])

    def test_nonempty_array_prepend(self):
        orig = [{"type": "text", "text": "sys-a"}]
        body = {"system": orig, "messages": []}
        self.assertTrue(inject_system_block(body, INJECT))
        self.assertEqual(body["system"],
                         [{"type": "text", "text": INJECT},
                          {"type": "text", "text": "sys-a"}])

    def test_first_block_matches_text_skips(self):
        body = {"system": [{"type": "text", "text": INJECT}], "messages": []}
        self.assertFalse(inject_system_block(body, INJECT))
        self.assertEqual(body["system"], [{"type": "text", "text": INJECT}])

    def test_malformed_first_block_non_dict_prepends(self):
        body = {"system": ["not-a-dict"], "messages": []}
        self.assertTrue(inject_system_block(body, INJECT))
        self.assertEqual(body["system"][0], {"type": "text", "text": INJECT})
        self.assertEqual(body["system"][1], "not-a-dict")

    def test_first_block_without_text_key_prepends(self):
        body = {"system": [{"type": "text"}], "messages": []}
        self.assertTrue(inject_system_block(body, INJECT))
        self.assertEqual(body["system"],
                         [{"type": "text", "text": INJECT}, {"type": "text"}])

    def test_double_call_idempotent(self):
        """硬约束：budget_retry/failover 复用同一 body_json，重复调用不叠加。"""
        body = {"messages": []}
        inject_system_block(body, INJECT)
        self.assertFalse(inject_system_block(body, INJECT))
        self.assertEqual(len(body["system"]), 1)

    def test_unknown_system_type_untouched(self):
        body = {"system": 42, "messages": []}
        self.assertFalse(inject_system_block(body, INJECT))
        self.assertEqual(body["system"], 42)


# ---------------------------------------------------------------------------
# validate_supply_ext：配置校验
# ---------------------------------------------------------------------------

class TestValidateSupplyExt(unittest.TestCase):

    def test_baseline_no_new_fields_ok(self):
        """无新字段（现有配置形态）不报错——零回归基线。"""
        validate_supply_ext(_supply())

    def test_extra_headers_ok(self):
        validate_supply_ext(_supply(extra_headers={"X-Working-Dir": "/"}))

    def test_extra_headers_not_dict_raises(self):
        with self.assertRaises(ValueError):
            validate_supply_ext(_supply(extra_headers=["X-Working-Dir"]))

    def test_extra_headers_invalid_values_raise(self):
        for bad in ({"k": ""}, {"k": 1}, {"k": None}):
            with self.assertRaises(ValueError):
                validate_supply_ext(_supply(extra_headers=bad))

    def test_extra_headers_forbidden_keys_raise(self):
        """禁键大小写不敏感：authorization / x-api-key / content-length。"""
        for key in ("authorization", "Authorization", "AUTHORIZATION",
                    "x-api-key", "X-API-Key", "content-length", "CONTENT-LENGTH"):
            with self.assertRaises(ValueError):
                validate_supply_ext(_supply(extra_headers={key: "v"}))

    def test_system_inject_anthropic_ok(self):
        validate_supply_ext(_supply(system_inject=INJECT))

    def test_system_inject_protocol_inferred_from_url_ok(self):
        """protocol 键缺省时从 url 尾缀推断（/v1/messages → anthropic）。"""
        s = {"id": "s1", "url": "http://up/v1/messages", "appkey": "k",
             "system_inject": INJECT}
        validate_supply_ext(s)

    def test_system_inject_non_anthropic_raises(self):
        for protocol in ("chat", "responses"):
            with self.assertRaises(ValueError):
                validate_supply_ext(_supply(protocol=protocol, system_inject=INJECT))

    def test_appkey_and_appkey_file_mutex_raises(self):
        with self.assertRaises(ValueError):
            validate_supply_ext(_supply(appkey_file="/tmp/kf.yaml"))

    def test_neither_appkey_nor_file_raises(self):
        s = _supply()
        del s["appkey"]
        with self.assertRaises(ValueError):
            validate_supply_ext(s)

    def test_appkey_file_only_ok(self):
        s = _supply()
        del s["appkey"]
        s["appkey_file"] = "/tmp/kf.yaml"
        validate_supply_ext(s)


# ---------------------------------------------------------------------------
# resolve_appkey
# ---------------------------------------------------------------------------

class TestResolveAppkey(unittest.TestCase):

    def test_direct_appkey(self):
        self.assertEqual(resolve_appkey(_supply()), "k")

    def test_file_hit(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "kf.yaml"
            _write_keyfile(kf, ["REQUEST_TIMEOUT: 600",
                                "AUTHORIZATION: tk_abc123",
                                "OTHER: v"])
            s = _supply()
            del s["appkey"]
            s["appkey_file"] = str(kf)
            self.assertEqual(resolve_appkey(s), "tk_abc123")

    def test_file_no_authorization_line_raises(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "kf.yaml"
            _write_keyfile(kf, ["REQUEST_TIMEOUT: 600"])
            s = {"id": "s9", "appkey_file": str(kf)}
            with self.assertRaises(ValueError) as ctx:
                resolve_appkey(s)
            self.assertIn("s9", str(ctx.exception))
            self.assertIn("读取失败", str(ctx.exception))

    def test_file_empty_value_raises(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "kf.yaml"
            _write_keyfile(kf, ["AUTHORIZATION:"])
            s = {"id": "s9", "appkey_file": str(kf)}
            with self.assertRaises(ValueError):
                resolve_appkey(s)

    def test_file_missing_raises(self):
        s = {"id": "s9", "appkey_file": "/nonexistent/__no_such__.yaml"}
        with self.assertRaises(ValueError) as ctx:
            resolve_appkey(s)
        self.assertIn("s9", str(ctx.exception))

    def test_tilde_expansion(self):
        """appkey_file 支持 ~ 展开（os.path.expanduser，HOME 指向临时目录）。"""
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {"HOME": d}):
            _write_keyfile(Path(d) / "kf.yaml", ["AUTHORIZATION: tk_home"])
            s = {"id": "s1", "appkey_file": "~/kf.yaml"}
            self.assertEqual(resolve_appkey(s), "tk_home")

    def test_neither_returns_empty_string(self):
        """均缺返回空串（历史行为；必填由配置校验层把关）。"""
        self.assertEqual(resolve_appkey({"id": "s1"}), "")


# ---------------------------------------------------------------------------
# 端到端：extra_headers
# ---------------------------------------------------------------------------

class TestExtraHeadersE2E(unittest.TestCase):

    def test_injected_and_overrides_passthrough(self):
        """注入生效 + 覆盖入站透传值（supply 声明最高优先）。"""
        ns, _ = _make_server([_supply(extra_headers={"X-Working-Dir": "/"})])
        h = _make_handler(ns, {"model": "claude-opus", "max_tokens": 16,
                                "messages": [{"role": "user", "content": "hi"}]},
                          extra_req_headers={"X-Working-Dir": "/client/dir"})
        m = _run(h, [_FakeResp(_anth_good())])
        req = m.call_args.args[0]
        self.assertEqual(req.headers["X-working-dir"], "/")

    def test_overrides_builtin_content_type_on_conversion(self):
        """转换路径（ANTHROPIC_TO_CHAT）内置 Content-Type 被 extra_headers 覆盖。"""
        ns, _ = _make_server([_supply(protocol="chat",
                                      extra_headers={"Content-Type": "application/vnd.custom"})])
        h = _make_handler(ns, {"model": "claude-opus", "max_tokens": 16,
                                "messages": [{"role": "user", "content": "hi"}]})
        m = _run(h, [_FakeResp(_chat_good())])
        req = m.call_args.args[0]
        self.assertEqual(req.headers["Content-type"], "application/vnd.custom")

    def test_content_length_skipped_at_runtime(self):
        """运行时跳过 content-length 键（校验层已禁，此处为双保险）。"""
        ns, _ = _make_server([_supply(extra_headers={"Content-Length": "999",
                                                     "X-Working-Dir": "/"})])
        h = _make_handler(ns, {"model": "claude-opus", "max_tokens": 16,
                                "messages": [{"role": "user", "content": "hi"}]})
        m = _run(h, [_FakeResp(_anth_good())])
        req = m.call_args.args[0]
        self.assertEqual(req.headers["Content-length"], str(len(req.data)))
        self.assertNotEqual(req.headers["Content-length"], "999")

    def test_count_tokens_path(self):
        ns, _ = _make_server([_supply(extra_headers={"X-Working-Dir": "/"})])
        h = _make_handler(ns, {"model": "claude-opus",
                                "messages": [{"role": "user", "content": "hi"}]},
                          path="/v1/messages/count_tokens",
                          extra_req_headers={"X-Working-Dir": "/client/dir"})
        m = _run(h, [_FakeResp({"input_tokens": 3})])
        req = m.call_args.args[0]
        self.assertEqual(req.headers["X-working-dir"], "/")


# ---------------------------------------------------------------------------
# 端到端：system_inject（三个注入点）
# ---------------------------------------------------------------------------

class TestSystemInjectE2E(unittest.TestCase):

    def _anth_body(self, **over):
        body = {"model": "claude-opus", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]}
        body.update(over)
        return body

    def test_passthrough_missing_system(self):
        ns, _ = _make_server([_supply(system_inject=INJECT)])
        h = _make_handler(ns, self._anth_body())
        m = _run(h, [_FakeResp(_anth_good())])
        sent = _sent_bodies(m)[0]
        self.assertEqual(sent["system"], [{"type": "text", "text": INJECT}])

    def test_passthrough_string_system_merged(self):
        ns, _ = _make_server([_supply(system_inject=INJECT)])
        h = _make_handler(ns, self._anth_body(system="You are helpful"))
        m = _run(h, [_FakeResp(_anth_good())])
        sent = _sent_bodies(m)[0]
        self.assertEqual(sent["system"], [
            {"type": "text", "text": INJECT},
            {"type": "text", "text": "You are helpful"}])

    def test_reverse_conversion_inject(self):
        """RESPONSES_TO_ANTHROPIC：转换后 body 同样前置注入。"""
        ns, _ = _make_server([_supply(system_inject=INJECT)])
        h = _make_handler(ns, {"model": "claude-opus", "input": "hi"},
                          path="/v1/responses")
        m = _run(h, [_FakeResp(_anth_good())])
        sent = _sent_bodies(m)[0]
        self.assertEqual(sent["system"][0], {"type": "text", "text": INJECT})

    def test_count_tokens_inject(self):
        ns, _ = _make_server([_supply(system_inject=INJECT)])
        h = _make_handler(ns, {"model": "claude-opus",
                                "messages": [{"role": "user", "content": "hi"}]},
                          path="/v1/messages/count_tokens")
        m = _run(h, [_FakeResp({"input_tokens": 3})])
        sent = _sent_bodies(m)[0]
        self.assertEqual(sent["system"], [{"type": "text", "text": INJECT}])

    def test_budget_retry_no_duplicate_injection(self):
        """硬约束：budget_retry 复用同一 body_json，重试轮不叠加计费块。"""
        ns, _ = _make_server([_supply(system_inject=INJECT)])
        h = _make_handler(ns, self._anth_body(max_tokens=16000))
        m = _run(h, [_FakeResp(_anth_truncated()), _FakeResp(_anth_good())])
        bodies = _sent_bodies(m)
        self.assertEqual(m.call_count, 2)
        for b in bodies:
            self.assertEqual(b["system"], [{"type": "text", "text": INJECT}])

    def test_non_anthropic_target_not_injected(self):
        """anthropic→chat 出站不注入（运行时条件即协议限定）。"""
        ns, _ = _make_server([_supply(protocol="chat", system_inject=INJECT)])
        h = _make_handler(ns, self._anth_body())
        m = _run(h, [_FakeResp(_chat_good())])
        sent = _sent_bodies(m)[0]
        self.assertNotIn("system", sent)


# ---------------------------------------------------------------------------
# 端到端：appkey_file
# ---------------------------------------------------------------------------

class TestAppkeyFileE2E(unittest.TestCase):

    def test_success_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "kf.yaml"
            _write_keyfile(kf, ["AUTHORIZATION: tk123"])
            s = _supply()
            del s["appkey"]
            s["appkey_file"] = str(kf)
            ns, _ = _make_server([s])
            h = _make_handler(ns, {"model": "claude-opus", "max_tokens": 16,
                                    "messages": [{"role": "user", "content": "hi"}]})
            m = _run(h, [_FakeResp(_anth_good())])
            req = m.call_args.args[0]
            self.assertEqual(req.headers["Authorization"], "Bearer tk123")
            self.assertEqual(req.headers["X-api-key"], "tk123")

    def test_missing_file_500_no_cooldown(self):
        """文件缺失：出站前 500、不触发 cooldown、错误体指向配置。"""
        s = _supply()
        del s["appkey"]
        s["appkey_file"] = "/nonexistent/__no_such__.yaml"
        ns, cd = _make_server([s])  # failover=on + cooldown rules 含 500/502
        h = _make_handler(ns, {"model": "claude-opus", "max_tokens": 16,
                                "messages": [{"role": "user", "content": "hi"}]})
        m = _run(h, [])
        m.assert_not_called()          # 未出站
        self.assertEqual(cd.cooled, [])  # 不进 cooldown
        status, body = h._responses[0]
        self.assertEqual(status, 500)
        msg = json.loads(body)["error"]["message"]
        self.assertIn("s1", msg)
        self.assertIn("appkey_file 读取失败", msg)

    def test_count_tokens_missing_file_500(self):
        s = _supply()
        del s["appkey"]
        s["appkey_file"] = "/nonexistent/__no_such__.yaml"
        ns, cd = _make_server([s])
        h = _make_handler(ns, {"model": "claude-opus",
                                "messages": [{"role": "user", "content": "hi"}]},
                          path="/v1/messages/count_tokens")
        m = _run(h, [])
        m.assert_not_called()
        self.assertEqual(cd.cooled, [])
        self.assertEqual(h._responses[0][0], 500)


# ---------------------------------------------------------------------------
# 配置校验双轨：_validate_config（启动 raise / 热重载吞）
# ---------------------------------------------------------------------------

class TestConfigValidationDualTrack(unittest.TestCase):

    @staticmethod
    def _cfg(supply):
        return {"supplies": [supply], "routes": [], "strategies": []}

    def test_startup_path_raise(self):
        """启动路径 fail-fast：非法配置（appkey+appkey_file 互斥）加载即抛。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps(self._cfg(_supply(appkey_file="/tmp/kf.yaml"))),
                         encoding="utf-8")
            with self.assertRaises(ValueError):
                ConfigStore(p)

    def test_startup_raise_on_each_new_rule(self):
        """三条新校验（禁键/协议限定/互斥缺一）在启动路径都能触发。"""
        cases = [
            _supply(extra_headers={"authorization": "x"}),
            _supply(protocol="chat", system_inject=INJECT),
        ]
        no_appkey = _supply()
        del no_appkey["appkey"]
        cases.append(no_appkey)
        for supply in cases:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "cfg.json"
                p.write_text(json.dumps(self._cfg(supply)), encoding="utf-8")
                with self.assertRaises(ValueError):
                    ConfigStore(p)

    def test_hot_reload_swallows_and_loads(self):
        """热重载路径（_reload_locked 吞校验异常）：不崩，非法配置仍生效（已知降级）。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps(self._cfg(_supply())), encoding="utf-8")
            cs = ConfigStore(p)
            # 改写为非法配置（互斥）后强制热重载：不抛异常，新配置生效
            p.write_text(json.dumps(self._cfg(_supply(appkey_file="/tmp/kf.yaml"))),
                         encoding="utf-8")
            cs.reload()
            self.assertEqual(cs.get_supply_map()["s1"].get("appkey_file"), "/tmp/kf.yaml")

    def test_legal_new_fields_load(self):
        """三新字段齐配的合法配置正常加载（mcli 形态）。"""
        supply = {"id": "catpaw", "url": "https://mcli.sankuai.com/v1/messages",
                  "protocol": "anthropic",
                  "appkey_file": "~/.config/mcopilot-cli/.config.yaml",
                  "target_model": "glm-5.3",
                  "extra_headers": {"X-Working-Dir": "/"},
                  "system_inject": INJECT}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps(self._cfg(supply)), encoding="utf-8")
            cs = ConfigStore(p)
            self.assertEqual(cs.get_supply_map()["catpaw"]["extra_headers"],
                             {"X-Working-Dir": "/"})


if __name__ == "__main__":
    unittest.main()
