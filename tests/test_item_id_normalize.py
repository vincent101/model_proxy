"""Responses input function_call item id 前缀规范化（item_ → fc_）单测。

对应 2026-11-10 修复：存量 codex 历史中的 item_ id 回放到 responses 上游被拒
（"Expected an ID that begins with 'fc'"）。
"""
import unittest

from core.item_id_normalize import normalize_responses_item_ids


def _body(items):
    return {"model": "claude-sonnet", "input": items}


class TestNormalizeResponsesItemIds(unittest.TestCase):
    def test_rewrites_legacy_function_call_id(self):
        body = _body([
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "q"}]},
            {"type": "function_call", "id": "item_4df75218fc9fa76dc7ae9e09a1f778b0",
             "call_id": "call_abc", "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_abc", "output": "r"},
        ])
        n = normalize_responses_item_ids(body)
        self.assertEqual(n, 1)
        self.assertEqual(body["input"][1]["id"],
                         "fc_4df75218fc9fa76dc7ae9e09a1f778b0")
        # call_id 不动（配对靠它）
        self.assertEqual(body["input"][1]["call_id"], "call_abc")
        self.assertEqual(body["input"][2]["call_id"], "call_abc")

    def test_already_canonical_untouched(self):
        body = _body([{"type": "function_call", "id": "fc_abc123",
                       "call_id": "c", "name": "t", "arguments": "{}"}])
        self.assertEqual(normalize_responses_item_ids(body), 0)
        self.assertEqual(body["input"][0]["id"], "fc_abc123")

    def test_missing_id_untouched(self):
        body = _body([{"type": "function_call", "call_id": "c",
                       "name": "t", "arguments": "{}"}])
        self.assertEqual(normalize_responses_item_ids(body), 0)
        self.assertNotIn("id", body["input"][0])

    def test_other_types_untouched(self):
        """message / reasoning / function_call_output 的 id 不改（不扩大改写面）。"""
        body = _body([
            {"type": "message", "id": "item_should_stay", "role": "user",
             "content": [{"type": "input_text", "text": "q"}]},
            {"type": "reasoning", "id": "item_rs_stay", "summary": []},
            {"type": "function_call_output", "id": "item_fco_stay",
             "call_id": "c", "output": "r"},
        ])
        self.assertEqual(normalize_responses_item_ids(body), 0)
        self.assertEqual(body["input"][0]["id"], "item_should_stay")
        self.assertEqual(body["input"][1]["id"], "item_rs_stay")
        self.assertEqual(body["input"][2]["id"], "item_fco_stay")

    def test_multiple_items_and_idempotent(self):
        body = _body([
            {"type": "function_call", "id": "item_aaa", "call_id": "c1",
             "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": "item_bbb", "call_id": "c2",
             "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": "fc_ccc", "call_id": "c3",
             "name": "t", "arguments": "{}"},
        ])
        self.assertEqual(normalize_responses_item_ids(body), 2)
        self.assertEqual([i["id"] for i in body["input"]],
                         ["fc_aaa", "fc_bbb", "fc_ccc"])
        # 幂等
        self.assertEqual(normalize_responses_item_ids(body), 0)

    def test_custom_tool_call_also_rewritten(self):
        body = _body([{"type": "custom_tool_call", "id": "item_zzz", "call_id": "c"}])
        self.assertEqual(normalize_responses_item_ids(body), 1)
        self.assertEqual(body["input"][0]["id"], "fc_zzz")

    def test_non_dict_and_missing_input_safe(self):
        self.assertEqual(normalize_responses_item_ids(None), 0)
        self.assertEqual(normalize_responses_item_ids({}), 0)
        self.assertEqual(normalize_responses_item_ids({"input": "not-a-list"}), 0)
        self.assertEqual(normalize_responses_item_ids({"input": [None, 42, "x"]}), 0)

    def test_string_input_untouched(self):
        body = {"model": "m", "input": "plain string input"}
        self.assertEqual(normalize_responses_item_ids(body), 0)
        self.assertEqual(body["input"], "plain string input")


if __name__ == "__main__":
    unittest.main()
