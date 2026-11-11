"""Responses 出站 input item id 剥离（sanitize_responses_input_ids）单测。

不变量：passthrough responses 出站不携带无法兑现的 item id；引用形态豁免。
2026-11-11 取代原「item_ → fc_ 前缀改写」实现。
"""
import json
import unittest

from core.item_id_normalize import sanitize_responses_input_ids


def _body(items):
    return {"model": "claude-sonnet", "input": items}


class TestStripByType(unittest.TestCase):
    def test_all_payload_types_stripped_regardless_of_prefix(self):
        """message/function_call/function_call_output/reasoning 各前缀一律剥离。"""
        body = _body([
            {"type": "message", "id": "msg_a", "role": "user",
             "content": [{"type": "input_text", "text": "q"}]},
            {"type": "function_call", "id": "fc_b", "call_id": "c1",
             "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "id": "fco_c", "call_id": "c1",
             "output": "r"},
            {"type": "reasoning", "id": "rs_d", "summary": []},
        ])
        stripped, refs = sanitize_responses_input_ids(body)
        self.assertEqual((stripped, refs), (4, 0))
        for item in body["input"]:
            self.assertNotIn("id", item)

    def test_legacy_item_prefix_also_stripped(self):
        """旧 item_ 前缀同属「有负载」→ 剥离（规则不看前缀）。"""
        body = _body([{"type": "function_call", "id": "item_deadbeef",
                       "call_id": "c", "name": "t", "arguments": "{}"}])
        self.assertEqual(sanitize_responses_input_ids(body), (1, 0))
        self.assertNotIn("id", body["input"][0])

    def test_unknown_types_with_payload_stripped(self):
        """未知/未来类型：有负载即剥离（不是类型白名单）。"""
        body = _body([
            {"type": "local_shell_call", "id": "lsh_1", "cmd": "ls"},
            {"type": "web_search_call", "id": "ws_1", "query": "q"},
            {"type": "mcp_call", "id": "mcp_1", "name": "n"},
        ])
        self.assertEqual(sanitize_responses_input_ids(body), (3, 0))


class TestReferenceExemption(unittest.TestCase):
    def test_item_reference_kept(self):
        body = _body([{"type": "item_reference", "id": "fc_x"}])
        self.assertEqual(sanitize_responses_input_ids(body), (0, 1))
        self.assertEqual(body["input"][0]["id"], "fc_x")

    def test_bare_reference_without_type_kept(self):
        body = _body([{"id": "msg_x"}])
        self.assertEqual(sanitize_responses_input_ids(body), (0, 1))
        self.assertEqual(body["input"][0]["id"], "msg_x")

    def test_reasoning_with_only_id_and_status_kept(self):
        """去掉 {id,type,status} 后无键 → 视为裸引用，保留。"""
        body = _body([{"type": "reasoning", "id": "rs_x", "status": "completed"}])
        self.assertEqual(sanitize_responses_input_ids(body), (0, 1))

    def test_reasoning_with_empty_payload_list_stripped(self):
        """键存在即算负载（即使值为空）→ 剥离。"""
        body = _body([{"type": "reasoning", "id": "rs_x", "summary": []}])
        self.assertEqual(sanitize_responses_input_ids(body), (1, 0))


class TestNonIdFieldsUntouched(unittest.TestCase):
    def test_preserves_all_other_fields(self):
        original = {
            "type": "function_call", "id": "fc_b", "call_id": "c1",
            "name": "tool", "arguments": '{"a": 1}', "status": "completed",
        }
        body = _body([dict(original)])
        sanitize_responses_input_ids(body)
        expected = {k: v for k, v in original.items() if k != "id"}
        self.assertEqual(body["input"][0], expected)

    def test_encrypted_content_preserved(self):
        body = _body([{"type": "reasoning", "id": "rs_x",
                       "encrypted_content": "gAAA...", "summary": []}])
        sanitize_responses_input_ids(body)
        self.assertEqual(body["input"][0]["encrypted_content"], "gAAA...")
        self.assertNotIn("id", body["input"][0])


class TestEdgeCases(unittest.TestCase):
    def test_no_id_untouched(self):
        body = _body([{"type": "function_call", "call_id": "c",
                       "name": "t", "arguments": "{}"}])
        self.assertEqual(sanitize_responses_input_ids(body), (0, 0))

    def test_non_dict_and_missing_input(self):
        self.assertEqual(sanitize_responses_input_ids(None), (0, 0))
        self.assertEqual(sanitize_responses_input_ids({}), (0, 0))
        self.assertEqual(sanitize_responses_input_ids({"input": "string"}), (0, 0))
        self.assertEqual(sanitize_responses_input_ids({"input": [None, 42, "x"]}), (0, 0))

    def test_idempotent(self):
        body = _body([
            {"type": "function_call", "id": "fc_a", "call_id": "c",
             "name": "t", "arguments": "{}"},
            {"type": "item_reference", "id": "fc_b"},
        ])
        self.assertEqual(sanitize_responses_input_ids(body), (1, 1))
        # 第二次：id 已剥离的跳过，引用仍保留 → (0, 1)
        self.assertEqual(sanitize_responses_input_ids(body), (0, 1))

    def test_mixed_counts(self):
        body = _body([
            {"type": "message", "id": "msg_a", "role": "user",
             "content": [{"type": "input_text", "text": "q"}]},
            {"type": "item_reference", "id": "fc_ref"},
            {"type": "reasoning", "id": "rs_c", "summary": []},
        ])
        self.assertEqual(sanitize_responses_input_ids(body), (2, 1))


if __name__ == "__main__":
    unittest.main()
