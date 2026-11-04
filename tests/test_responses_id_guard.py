"""responses_id_guard 纯函数单测（脱网络）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.responses_id_guard import (  # noqa: E402
    parse_invalid_item_id_rejection,
    rewrite_invalid_item_ids,
)


_BAD_ID = b'''{
  "error": {
    "message": "Invalid 'input[4].id': 'item_974dabae5c9e316b3f24f6b4040c3f98'. Expected an ID that begins with 'fc'.",
    "type": "invalid_request_error",
    "param": "input[4].id",
    "code": "invalid_value"
  }
}'''


def _rejection(index: int):
    body = _BAD_ID.replace(b"input[4]", f"input[{index}]".encode())
    return parse_invalid_item_id_rejection(body)


class TestParseInvalidItemIdRejection(unittest.TestCase):
    def test_real_wire_shape(self):
        rejection = parse_invalid_item_id_rejection(_BAD_ID)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.index, 4)
        self.assertEqual(rejection.expected_prefix, "fc")

    def test_wrapped_ext_error_shape(self):
        body = b'{"ext":{"error":{"message":"Expected an ID that begins with \'fc\'.","param":"input[2].id"}}}'
        rejection = parse_invalid_item_id_rejection(body)
        self.assertIsNotNone(rejection)
        self.assertEqual((rejection.index, rejection.expected_prefix), (2, "fc"))

    def test_non_item_param_not_matched(self):
        body = b'{"error":{"message":"Expected an ID that begins with \'fc\'.","param":"tools[0].id"}}'
        self.assertIsNone(parse_invalid_item_id_rejection(body))

    def test_missing_expected_marker_not_matched(self):
        body = b'{"error":{"message":"bad id","param":"input[4].id"}}'
        self.assertIsNone(parse_invalid_item_id_rejection(body))

    def test_invalid_json_not_matched(self):
        self.assertIsNone(parse_invalid_item_id_rejection(b"{bad"))


class TestRewriteInvalidItemIds(unittest.TestCase):
    def test_rewrites_all_matching_function_call_ids(self):
        body = {"input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "function_call", "id": "item_1", "call_id": "call_1"},
            {"type": "message", "role": "assistant", "content": []},
            {"type": "function_call", "id": "item_2", "call_id": "call_2"},
            {"type": "function_call", "id": "fc_good", "call_id": "call_3"},
        ]}
        result = rewrite_invalid_item_ids(body, _rejection(1))
        self.assertEqual(result.id_count, 2)
        self.assertTrue(body["input"][1]["id"].startswith("fc_"))
        self.assertTrue(body["input"][3]["id"].startswith("fc_"))
        self.assertEqual(body["input"][4]["id"], "fc_good")

    def test_rewrites_nonstandard_call_ids_pairwise(self):
        body = {"input": [
            {"type": "function_call", "id": "item_1", "call_id": "item_call_1"},
            {"type": "function_call_output", "call_id": "item_call_1", "output": "ok"},
        ]}
        result = rewrite_invalid_item_ids(body, _rejection(0))
        self.assertEqual(result.call_id_count, 2)
        self.assertEqual(body["input"][0]["call_id"], body["input"][1]["call_id"])
        self.assertTrue(body["input"][0]["call_id"].startswith("call_"))

    def test_keeps_existing_call_prefix(self):
        body = {"input": [
            {"type": "function_call", "id": "item_1", "call_id": "call_1"},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ]}
        # 只重写 id；call_id 已符合 call_ 前缀，不改。
        result = rewrite_invalid_item_ids(body, _rejection(0))
        self.assertEqual(result.call_id_count, 0)
        self.assertEqual(body["input"][0]["call_id"], "call_1")
        self.assertEqual(body["input"][1]["call_id"], "call_1")

    def test_rewrite_is_deterministic(self):
        body = {"input": [{"type": "function_call", "id": "item_abc", "call_id": "call_1"}]}
        rewrite_invalid_item_ids(body, _rejection(0))
        new_id = body["input"][0]["id"]

        body2 = {"input": [{"type": "function_call", "id": "item_abc", "call_id": "call_1"}]}
        rewrite_invalid_item_ids(body2, _rejection(0))
        self.assertEqual(body2["input"][0]["id"], new_id)

    def test_out_of_range_index_noop(self):
        body = {"input": []}
        self.assertEqual(rewrite_invalid_item_ids(body, _rejection(4)).fields, 0)

    def test_non_dict_item_noop(self):
        body = {"input": ["text"]}
        self.assertEqual(rewrite_invalid_item_ids(body, _rejection(0)).fields, 0)

    def test_non_list_input_noop(self):
        body = {"input": "probe"}
        self.assertEqual(rewrite_invalid_item_ids(body, _rejection(0)).fields, 0)

    def test_message_item_not_rewritten(self):
        body = {"input": [
            {"type": "message", "id": "item_msg", "role": "user", "content": "hi"},
        ]}
        result = rewrite_invalid_item_ids(body, _rejection(0))
        self.assertEqual(result.fields, 0)
        self.assertEqual(body["input"][0]["id"], "item_msg")

    def test_already_prefixed_noop(self):
        body = {"input": [{"type": "function_call", "id": "fc_existing", "call_id": "call_1"}]}
        result = rewrite_invalid_item_ids(body, _rejection(0))
        self.assertEqual(result.id_count, 0)
        self.assertEqual(body["input"][0]["id"], "fc_existing")


if __name__ == "__main__":
    unittest.main()
