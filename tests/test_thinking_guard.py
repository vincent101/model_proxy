"""thinking_guard 纯函数单测（脱网络）。

覆盖（设计记录 docs/designs/2026-10-28-thinking空签名剥离与supply级自适应.md
§验证方式 1-5，推演·具象1/具象2 逐条断言）：
- 剥离判据四态：空签名删 / 缺键删 / 非空签名留 / redacted_thinking 留
- 幂等：同 body 连续两次，第二次返回 0 且 body 不变
- 非法输入：body_json None、messages 缺失、content 字符串、messages 非列表 → 0 且不抛
- 分类器：反引号体命中 / 无反引号命中 / Field-required 不命中 / 空 bytes 不命中
- ThinkingStripStore：learn→True；TTL 过期（patch time.time +48h+1s）→ False；未 learn → False

运行：cd tools/model_proxy && python3 -m unittest tests.test_thinking_guard -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.thinking_guard import (  # noqa: E402
    CountTokensUnsupportedStore,
    ThinkingStripStore,
    ToolStrictStore,
    is_strict_mode_rejection,
    is_invalid_signature_rejection,
    strip_empty_signature_thinking,
)


def _body_with(blocks):
    """assistant 历史消息 content = blocks + 一条 user 消息。"""
    return {"model": "claude-sonnet", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": list(blocks)},
        {"role": "user", "content": "go on"},
    ]}


class TestStripEmptySignatureThinking(unittest.TestCase):

    def test_empty_signature_block_removed(self):
        """具象1：signature="" → 命中删除（mcli 产块形态）。"""
        body = _body_with([
            {"type": "thinking", "thinking": "…", "signature": ""},
            {"type": "text", "text": "答"},
        ])
        self.assertEqual(strip_empty_signature_thinking(body), 1)
        self.assertEqual(body["messages"][1]["content"],
                         [{"type": "text", "text": "答"}])

    def test_missing_signature_key_removed(self):
        """具象1：缺 signature 键 → 命中删除。"""
        body = _body_with([
            {"type": "thinking", "thinking": "…"},
            {"type": "text", "text": "答"},
        ])
        self.assertEqual(strip_empty_signature_thinking(body), 1)
        self.assertEqual(len(body["messages"][1]["content"]), 1)

    def test_non_empty_signature_kept(self):
        """具象1：signature 非空（aigc 自签块）→ 保留。"""
        body = _body_with([
            {"type": "thinking", "thinking": "…", "signature": "EqQBCabc123"},
            {"type": "text", "text": "答"},
        ])
        self.assertEqual(strip_empty_signature_thinking(body), 0)
        self.assertEqual(len(body["messages"][1]["content"]), 2)

    def test_redacted_thinking_kept(self):
        """具象1：redacted_thinking 不命中 type 判据 → 保留（风险 R4）。"""
        body = _body_with([
            {"type": "redacted_thinking", "data": "…"},
            {"type": "text", "text": "答"},
        ])
        self.assertEqual(strip_empty_signature_thinking(body), 0)
        self.assertEqual(len(body["messages"][1]["content"]), 2)

    def test_multiple_messages_and_blocks(self):
        body = _body_with([
            {"type": "thinking", "thinking": "a", "signature": ""},
            {"type": "thinking", "thinking": "b"},          # 缺键
            {"type": "thinking", "thinking": "c", "signature": "sig"},   # 保留
            {"type": "text", "text": "答"},
        ])
        body["messages"].insert(2, {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "d", "signature": ""}]})
        self.assertEqual(strip_empty_signature_thinking(body), 3)
        self.assertEqual(body["messages"][1]["content"],
                         [{"type": "thinking", "thinking": "c", "signature": "sig"},
                          {"type": "text", "text": "答"}])
        self.assertEqual(body["messages"][2]["content"], [])

    def test_user_tool_blocks_untouched(self):
        """user 消息 / tool_use / text 块不在判据内，零改动。"""
        body = {"messages": [
            {"role": "user", "content": [
                {"type": "thinking", "thinking": "x", "signature": ""}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "f", "input": {}}]},
        ]}
        self.assertEqual(strip_empty_signature_thinking(body), 0)

    def test_idempotent(self):
        """幂等：第二次调用返回 0 且 body 不变。"""
        body = _body_with([
            {"type": "thinking", "thinking": "…", "signature": ""},
            {"type": "text", "text": "答"},
        ])
        self.assertEqual(strip_empty_signature_thinking(body), 1)
        snapshot = repr(body)
        self.assertEqual(strip_empty_signature_thinking(body), 0)
        self.assertEqual(repr(body), snapshot)

    def test_invalid_inputs_safe(self):
        """非法输入全部安全跳过返回 0 且不抛。"""
        self.assertEqual(strip_empty_signature_thinking(None), 0)
        self.assertEqual(strip_empty_signature_thinking({}), 0)                    # messages 缺失
        self.assertEqual(strip_empty_signature_thinking({"messages": "x"}), 0)     # 非列表
        self.assertEqual(strip_empty_signature_thinking(
            {"messages": [{"role": "assistant", "content": "字符串"}]}), 0)
        self.assertEqual(strip_empty_signature_thinking(
            {"messages": [{"role": "assistant", "content": None}]}), 0)
        self.assertEqual(strip_empty_signature_thinking(
            {"messages": ["not-a-dict"]}), 0)


class TestIsInvalidSignatureRejection(unittest.TestCase):

    _BACKTICK = (b'{"error":{"type":"invalid_request_error","message":'
                 b'"Invalid `signature` in `thinking` block: signature VerboseB"}')

    def test_backtick_form_hits(self):
        """具象2：带反引号报文 → 命中。"""
        self.assertTrue(is_invalid_signature_rejection(self._BACKTICK))

    def test_plain_form_hits(self):
        """具象2：无反引号报文 → 命中。"""
        plain = self._BACKTICK.replace(b"`", b"")
        self.assertTrue(is_invalid_signature_rejection(plain))

    def test_case_insensitive(self):
        body = self._BACKTICK.replace(b"Invalid", b"INVALID")
        self.assertTrue(is_invalid_signature_rejection(body))

    def test_field_required_not_hit(self):
        """具象2：schema 层 Field required 报文 → 不命中（fail-open）。"""
        body = (b'{"error":{"message":'
                b'"messages.0.content.1: Field required: signature"}}')
        self.assertFalse(is_invalid_signature_rejection(body))

    def test_empty_bytes_not_hit(self):
        self.assertFalse(is_invalid_signature_rejection(b""))

    def test_garbage_bytes_not_hit(self):
        self.assertFalse(is_invalid_signature_rejection(b"\xff\xfe\x00garbage"))


class TestThinkingStripStore(unittest.TestCase):

    def test_learn_then_snapshot_true(self):
        store = ThinkingStripStore()
        self.assertFalse(store.snapshot("s1"))
        store.learn("s1")
        self.assertTrue(store.snapshot("s1"))
        self.assertFalse(store.snapshot("s2"))   # 其他 supply 不受影响

    def test_ttl_expiry(self):
        """TTL 48h：patch time.time +48h+1s → entry 失效。"""
        store = ThinkingStripStore()
        store.learn("s1")
        with patch("core.thinking_guard.time.time",
                   return_value=store._cache["s1"]["at"] + 48 * 3600 + 1):
            self.assertFalse(store.snapshot("s1"))
        # 边界：恰好 48h 内仍有效
        with patch("core.thinking_guard.time.time",
                   return_value=store._cache["s1"]["at"] + 48 * 3600 - 1):
            self.assertTrue(store.snapshot("s1"))

    def test_relearn_refreshes_ttl(self):
        store = ThinkingStripStore()
        store.learn("s1")
        with patch("core.thinking_guard.time.time",
                   return_value=store._cache["s1"]["at"] + 48 * 3600 + 1):
            self.assertFalse(store.snapshot("s1"))
            store.learn("s1")   # 过期后 re-learn → 自愈
        self.assertTrue(store.snapshot("s1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestToolStrictStore(unittest.TestCase):
    """supply 级工具 strict 策略存储（2026-11-06 Agent isolation 填齐问题）。"""

    def test_default_is_false(self):
        store = ToolStrictStore()
        self.assertFalse(store.snapshot("any-supply"))

    def test_learn_true_then_snapshot(self):
        store = ToolStrictStore()
        store.learn("sol", True)
        self.assertTrue(store.snapshot("sol"))
        self.assertFalse(store.snapshot("other"))  # supply 隔离

    def test_learn_false_resets(self):
        store = ToolStrictStore()
        store.learn("sol", True)
        store.learn("sol", False)
        self.assertFalse(store.snapshot("sol"))

    def test_expiry(self):
        store = ToolStrictStore()
        store.learn("sol", True)
        # 模拟过期
        with store._lock:
            store._cache["sol"]["at"] = 0
        self.assertFalse(store.snapshot("sol"))


class TestStrictModeRejection(unittest.TestCase):
    def test_strict_keyword(self):
        self.assertTrue(is_strict_mode_rejection(
            b'{"error":{"message":"strict mode requires additionalProperties"}}'))

    def test_additionalproperties_and_required(self):
        self.assertTrue(is_strict_mode_rejection(
            b'{"error":{"message":"additionalProperties is required to be false; '
            b'all fields must be in required"}}'))

    def test_normal_schema_error_no_match(self):
        self.assertFalse(is_strict_mode_rejection(
            b'{"error":{"message":"field x missing type"}}'))

    def test_empty_body(self):
        self.assertFalse(is_strict_mode_rejection(b""))


class TestCountTokensUnsupportedStore(unittest.TestCase):
    """supply 级 count_tokens 不支持标记（mcli 返 404，learn 后本地短接）。"""

    def test_default_false(self):
        store = CountTokensUnsupportedStore()
        self.assertFalse(store.snapshot("any"))

    def test_learn_then_snapshot(self):
        store = CountTokensUnsupportedStore()
        store.learn("mcli-a")
        self.assertTrue(store.snapshot("mcli-a"))
        self.assertFalse(store.snapshot("mcli-b"))  # supply 隔离

    def test_expiry(self):
        store = CountTokensUnsupportedStore()
        store.learn("mcli-a")
        with store._lock:
            store._cache["mcli-a"]["at"] = 0
        self.assertFalse(store.snapshot("mcli-a"))
