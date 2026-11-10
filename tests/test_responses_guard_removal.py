"""Responses guard removal: synthetic upstream only, no real HTTP or configuration.

2026-11-10 更新：新增「出站无条件规范化 item_ → fc_ 前缀」（core/item_id_normalize）。
本文件守卫的不变量随之收敛为：
- 400 不做 ID guard 重试（attempts / failover / cooldown 行为不变）；
- 不注入 store、不改 call_id、不动 reasoning/message 的 id；
- 出站 body 除 target_model 替换 + 旧前缀规范化外，其余字段一字不改。
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

    @staticmethod
    def _expect_forwarded(original):
        """期望的出站 body：target_model 替换 + 旧 item_ id 规范化（其他不动）。"""
        expected = json.loads(json.dumps(original))  # deep copy
        expected["model"] = "m1"
        for item in expected.get("input") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("function_call", "custom_tool_call"):
                iid = item.get("id")
                if isinstance(iid, str) and iid.startswith("item_"):
                    item["id"] = "fc_" + iid[len("item_"):]
        return expected

    def _assert_forwarded(self, original, sent):
        expected = self._expect_forwarded(original)
        for outgoing in sent:
            self.assertEqual(outgoing, expected)

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
