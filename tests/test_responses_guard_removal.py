"""Responses guard removal: synthetic upstream only, no real HTTP or configuration.

2026-11-11 更新：出站由「item_ → fc_ 前缀改写」收敛为「剥离无法兑现的 input item id」
（core/item_id_normalize.sanitize_responses_input_ids）。本文件守卫的不变量：
- 400 不做 ID guard 重试（attempts / failover / cooldown 行为不变）；
- 不注入 store、不改 call_id、不动 encrypted_content / arguments / output；
- 出站 body = target_model 替换 + 有负载 item 的 id 剥离（引用形态豁免），其余一字不改。
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from core import translate
from tests.test_thinking_strip_retry import _make_handler, _make_server


_MISSING = object()
_ID_ERROR = json.dumps({"error": {
    "param": "input[0].id",
    "message": "Expected an ID that begins with 'fc'.",
}}).encode()
_STORE_ERROR = json.dumps({"error": {
    "message": "Items are not persisted when `store` is set to false.",
}}).encode()
_GOOD = {"id": "synthetic_response", "status": "completed", "output": [],
         "usage": {"total_tokens": 5}}


def _body(store):
    body = {
        "model": "claude-opus",
        "input": [
            {"type": "function_call", "id": "item_first", "call_id": "odd_first",
             "name": "f", "arguments": '{"x": 1}'},
            {"type": "function_call_output", "call_id": "odd_first", "output": "ok"},
            {"type": "function_call", "id": "item_second", "call_id": "odd_second",
             "name": "g", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "odd_second", "output": "ok2"},
            {"type": "custom_tool_call", "id": "item_custom", "call_id": "odd_custom",
             "name": "c", "input": "text"},
            {"type": "reasoning", "id": "rs_synthetic", "summary": []},
        ],
    }
    if store is not _MISSING:
        body["store"] = store
    return body


class _MemoryResponse(io.BytesIO):
    status = 200

    def __init__(self):
        super().__init__(json.dumps(_GOOD).encode())

    def getheaders(self):
        return [("Content-Type", "application/json")]


def _error(body):
    return urllib.error.HTTPError("http://synthetic", 400, "err", {}, io.BytesIO(body))


def _supply(sid):
    return {"id": sid, "url": f"http://{sid}.invalid/v1/responses",
            "protocol": "responses", "appkey": "synthetic", "target_model": "m1"}


class TestResponsesGuardRemoval(unittest.TestCase):
    def _run_case(self, body, failover, error=None):
        supplies = [_supply("s1"), _supply("s2")]
        server, cooldown, _ = _make_server(supplies, ["s1", "s2"], failover=failover)
        handler = _make_handler(server, body, path="/v1/responses")
        queue = ([] if error is None else [_error(error)]) + [
            _MemoryResponse(), _MemoryResponse()]
        with patch("socket.socket", side_effect=AssertionError("socket forbidden")), \
             patch("core.server.urllib.request.urlopen", side_effect=queue) as upstream, \
             patch("core.server.log.warning") as warnings, \
             patch("core.server.access_log.info") as access, \
             patch("core.server._record_unconfigured"):
            handler._forward_logged("POST")
        self.assertEqual(access.call_count, 1)
        args = access.call_args.args
        line = args[0] % args[1:]  # also verifies placeholder/argument alignment
        warning_text = "\n".join(str(call) for call in warnings.call_args_list)
        sent = [json.loads(call.args[0].data) for call in upstream.call_args_list]
        urls = [call.args[0].full_url for call in upstream.call_args_list]
        return handler, cooldown, sent, urls, line, warning_text

    def _assert_forwarded(self, original, sent):
        """出站契约：target_model 替换 + 有负载 item 的 id 剥离（引用形态豁免）。

        显式断言「源有 id 的 outgoing 必无 id」，其余字段逐项深等——不复制剥离规则，
        避免测试镜像实现导致漂移。
        """
        src_items = original.get("input") or []
        for outgoing in sent:
            self.assertEqual(outgoing.get("model"), "m1")  # existing target_model substitution
            out_items = outgoing.get("input") or []
            self.assertEqual(len(out_items), len(src_items))
            for src, out in zip(src_items, out_items):
                if not isinstance(src, dict) or not isinstance(out, dict):
                    self.assertEqual(out, src)
                    continue
                had_id = "id" in src
                is_ref = src.get("type") == "item_reference"
                if had_id and not is_ref:
                    self.assertNotIn("id", out,
                                     f"payload item id must be stripped: {out}")
                if had_id and is_ref:
                    self.assertEqual(out.get("id"), src.get("id"),
                                     "reference-shaped id must be kept")
                # 其余字段（call_id/arguments/output/summary/status/content/...）深等
                src_rest = {k: v for k, v in src.items() if k != "id"}
                out_rest = {k: v for k, v in out.items() if k != "id"}
                self.assertEqual(out_rest, src_rest)

    def _assert_observation(self, handler, line, warnings, attempts, status):
        for name in ("responses_id_rewrite", "responses_store_retry"):
            self.assertNotIn(name, handler._acc)
            self.assertNotIn(name, line)
        for name in ("responses_id_retry", "responses_store_retry"):
            self.assertNotIn(name, warnings)
        self.assertEqual(handler._acc["attempts"], attempts)
        self.assertEqual(handler._acc["status"], status)
        self.assertIn(f"attempts={attempts}", line)
        self.assertIn(f"status={status}", line)
        for field in ("source=responses", "operation=responses", "route=r1",
                      "tier=opus", f"supply={handler._acc['supply']}",
                      "target_protocol=responses", "conversion_kind=passthrough",
                      f"failover={handler._acc['failover']}", "thinking_strip=0",
                      "response_committed=0", "stream_integrity= ",
                      "terminal_status= ", "terminal_reason= ", "first_event_ms="):
            self.assertIn(field, line)

    def test_id_rejection_failover_off_no_retry(self):
        """400 不触发 ID guard 重试（历史上已撤除）；出站仅做前缀规范化，不改其他。"""
        body = _body(False)
        h, cd, sent, urls, line, warnings = self._run_case(body, "off", _ID_ERROR)
        self._assert_forwarded(body, sent)
        self.assertEqual(urls, ["http://s1.invalid/v1/responses"])
        self.assertEqual(cd.calls, [])
        self._assert_observation(h, line, warnings, 1, 400)

    def test_store_rejection_failover_off_no_retry_or_injection(self):
        for store in (False, True, None, _MISSING):
            with self.subTest(store=store):
                body = _body(store)
                h, cd, sent, urls, line, warnings = self._run_case(body, "off", _STORE_ERROR)
                self._assert_forwarded(body, sent)
                self.assertEqual(urls, ["http://s1.invalid/v1/responses"])
                self.assertEqual(cd.calls, [])
                self._assert_observation(h, line, warnings, 1, 400)

    def test_400_failover_switches_supply_without_guard_retry(self):
        for error in (_ID_ERROR, _STORE_ERROR):
            for store in (False, True, None, _MISSING):
                with self.subTest(error=error, store=store):
                    body = _body(store)
                    h, cd, sent, urls, line, warnings = self._run_case(body, "on", error)
                    self._assert_forwarded(body, sent)
                    self.assertEqual(urls, ["http://s1.invalid/v1/responses",
                                            "http://s2.invalid/v1/responses"])
                    self.assertEqual(cd.calls, [("s1", 60, "http_400")])
                    self.assertEqual(h._acc["failover"], 1)
                    self._assert_observation(h, line, warnings, 2, 200)

    def test_success_passthrough_store_and_ids(self):
        for store in (False, True, None, _MISSING):
            with self.subTest(store=store):
                body = _body(store)
                h, cd, sent, urls, line, warnings = self._run_case(body, "off")
                self._assert_forwarded(body, sent)
                self.assertEqual(urls, ["http://s1.invalid/v1/responses"])
                self.assertEqual(cd.calls, [])
                self._assert_observation(h, line, warnings, 1, 200)

    def test_failover_second_round_body_also_stripped(self):
        """failover 换 supply 后，第二轮出站 body 同样无 payload id（幂等复用 body_json）。"""
        body = _body(False)
        h, cd, sent, urls, line, warnings = self._run_case(body, "on", _ID_ERROR)
        self.assertEqual(len(sent), 2)
        self._assert_forwarded(body, sent)  # 两轮都满足剥离契约

    def test_reference_shaped_item_id_kept(self):
        """item_reference 形态豁免：id 保留（剥离会变空 item），并发 warning 计数。"""
        body = {
            "model": "claude-opus",
            "input": [
                {"type": "message", "id": "msg_keep_payload", "role": "user",
                 "content": [{"type": "input_text", "text": "q"}]},
                {"type": "item_reference", "id": "fc_ref_keep"},
            ],
        }
        h, cd, sent, urls, line, warnings = self._run_case(body, "off")
        self.assertEqual(len(sent), 1)
        outgoing = sent[0]
        self.assertEqual(outgoing["model"], "m1")
        items = outgoing["input"]
        # 有负载的 message id 被剥离
        self.assertNotIn("id", items[0])
        self.assertEqual(items[0]["content"], [{"type": "input_text", "text": "q"}])
        # 引用形态 id 保留
        self.assertEqual(items[1]["id"], "fc_ref_keep")
        # 观测：kept reference 走 warning
        self.assertIn("reference-shaped", warnings)

    def test_protocol_conversion_preserves_tool_use_and_result_pair(self):
        anthropic = {"model": "claude-opus", "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "odd_conversion_id", "name": "f", "input": {"x": 1}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "odd_conversion_id", "content": "ok"}]},
        ]}
        responses, _ = translate.anthropic_to_responses_request(anthropic)
        calls = [item for item in responses["input"] if item["type"] == "function_call"]
        outputs = [item for item in responses["input"] if item["type"] == "function_call_output"]
        self.assertEqual(calls[0]["call_id"], "odd_conversion_id")
        self.assertEqual(outputs[0]["call_id"], "odd_conversion_id")
        roundtrip = translate.responses_to_anthropic_request(responses)
        blocks = [block for msg in roundtrip["messages"] for block in msg["content"]]
        tool = next(block for block in blocks if block["type"] == "tool_use")
        result = next(block for block in blocks if block["type"] == "tool_result")
        self.assertEqual(tool["id"], "odd_conversion_id")
        self.assertEqual(result["tool_use_id"], "odd_conversion_id")
        self.assertEqual(tool["input"], {"x": 1})
        self.assertEqual(result["content"], "ok")
