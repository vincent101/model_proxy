"""responses_store_guard 纯函数单测（脱网络）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.responses_store_guard import (  # noqa: E402
    enable_store_for_retry,
    parse_store_disabled_rejection,
)


_STORE_400 = b'''{
  "error": {
    "message": "Item with id 'rs_611faf3efce7ac49394edc5f1f3a0a26' not found. Items are not persisted when `store` is set to false. Try again with `store` set to true, or remove this item from your input.",
    "type": "invalid_request_error",
    "param": "input",
    "code": null
  }
}'''


class TestParseStoreDisabledRejection(unittest.TestCase):
    def test_real_wire_shape(self):
        self.assertTrue(parse_store_disabled_rejection(_STORE_400))

    def test_case_insensitive(self):
        body = _STORE_400.replace(b"`store` is set to false", b"`STORE` IS SET TO FALSE")
        self.assertTrue(parse_store_disabled_rejection(body))

    def test_wrapped_ext_error(self):
        body = b'{"ext":{"error":{"message":"Items are not persisted when `store` is set to false."}}}'
        self.assertTrue(parse_store_disabled_rejection(body))

    def test_other_error_not_matched(self):
        body = b'{"error":{"message":"Invalid \'input[4].id\'"}}'
        self.assertFalse(parse_store_disabled_rejection(body))

    def test_invalid_json_not_matched(self):
        self.assertFalse(parse_store_disabled_rejection(b"{bad"))

    def test_empty_body_not_matched(self):
        self.assertFalse(parse_store_disabled_rejection(b""))


class TestEnableStoreForRetry(unittest.TestCase):
    def test_false_becomes_true(self):
        body = {"store": False, "input": []}
        self.assertTrue(enable_store_for_retry(body))
        self.assertIs(body["store"], True)

    def test_true_unchanged(self):
        body = {"store": True}
        self.assertFalse(enable_store_for_retry(body))
        self.assertIs(body["store"], True)

    def test_missing_store_unchanged(self):
        body = {"input": []}
        self.assertFalse(enable_store_for_retry(body))
        self.assertNotIn("store", body)

    def test_none_store_unchanged(self):
        # None 是字段存在但显式空值；不属于本方案的 False 场景，不写 true。
        body = {"store": None}
        self.assertFalse(enable_store_for_retry(body))
        self.assertIsNone(body["store"])

    def test_non_dict_body_unchanged(self):
        self.assertFalse(enable_store_for_retry(None))
        self.assertFalse(enable_store_for_retry("x"))
        self.assertFalse(enable_store_for_retry([]))


if __name__ == "__main__":
    unittest.main()
