"""responses_id_guard 纯函数单测（脱网络）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.responses_id_guard import (  # noqa: E402
    parse_invalid_item_id_rejection,
    rewrite_invalid_item_id,
)


_BAD_ID = b'''{
  "error": {
    "message": "Invalid 'input[4].id': 'item_974dabae5c9e316b3f24f6b4040c3f98'. Expected an ID that begins with 'fc'.",
    "type": "invalid_request_error",
    "param": "input[4].id",
    "code": "invalid_value"
  }
}'''


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


class TestRewriteInvalidItemId(unittest.TestCase):
    def test_rewrites_item_id_stably(self):
        body = {"input": [
            {"type": "message", "role": "user"},
            {"type": "function_call", "id": "item_abc", "call_id": "call_1"},
        ]}
        rejection = parse_invalid_item_id_rejection(_BAD_ID.replace(b"input[4]", b"input[1]"))
        self.assertTrue(rewrite_invalid_item_id(body, rejection))
        self.assertTrue(body["input"][1]["id"].startswith("fc_"))
        self.assertNotEqual(body["input"][1]["id"], "item_abc")
        self.assertEqual(body["input"][1]["call_id"], "call_1")

    def test_rewrite_is_deterministic(self):
        body = {"input": [{"id": "item_abc"}]}
        first = dict(body["input"][0])
        rejection = parse_invalid_item_id_rejection(_BAD_ID.replace(b"input[4]", b"input[0]"))
        rewrite_invalid_item_id(body, rejection)
        new_id = body["input"][0]["id"]
        body2 = {"input": [first]}
        rewrite_invalid_item_id(body2, rejection)
        self.assertEqual(body2["input"][0]["id"], new_id)

    def test_out_of_range_index_noop(self):
        body = {"input": []}
        rejection = parse_invalid_item_id_rejection(_BAD_ID)
        self.assertFalse(rewrite_invalid_item_id(body, rejection))

    def test_already_prefixed_noop(self):
        body = {"input": [{"id": "fc_existing"}]}
        rejection = parse_invalid_item_id_rejection(_BAD_ID.replace(b"input[4]", b"input[0]"))
        self.assertFalse(rewrite_invalid_item_id(body, rejection))

    def test_non_dict_item_noop(self):
        body = {"input": ["text"]}
        rejection = parse_invalid_item_id_rejection(_BAD_ID.replace(b"input[4]", b"input[0]"))
        self.assertFalse(rewrite_invalid_item_id(body, rejection))

    def test_non_list_input_noop(self):
        body = {"input": "probe"}
        rejection = parse_invalid_item_id_rejection(_BAD_ID.replace(b"input[4]", b"input[0]"))
        self.assertFalse(rewrite_invalid_item_id(body, rejection))


if __name__ == "__main__":
    unittest.main()
