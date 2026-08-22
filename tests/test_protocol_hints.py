import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.protocol_hints import (
    build_protocol_conversion_hints,
    operation_compatible,
    protocol_conversion_kind,
)


class TestProtocolHints(unittest.TestCase):
    def test_conversion_kind_enum(self):
        self.assertEqual(protocol_conversion_kind("anthropic", "anthropic"), "passthrough")
        self.assertEqual(protocol_conversion_kind("anthropic", "responses"), "a2r")
        self.assertEqual(protocol_conversion_kind("responses", "anthropic"), "r2a")
        self.assertEqual(protocol_conversion_kind("anthropic", "chat"), "a2chat")
        self.assertEqual(protocol_conversion_kind("chat", "anthropic"), "chat2a")
        self.assertEqual(protocol_conversion_kind("responses", "chat"), "r2chat")
        self.assertEqual(protocol_conversion_kind("chat", "responses"), "chat2r")

    def test_build_full_preview(self):
        cfg = {
            "supplies": [{"id": "s1", "protocol": "responses", "url": "https://x/v1/responses"}],
            "routes": [{"id": "r1", "tiers": {"opus": ["s1"]}}],
        }
        hints = build_protocol_conversion_hints(cfg)
        self.assertEqual(len(hints), 3)
        self.assertIn({
            "route": "r1", "tier": "opus", "supply": "s1", "source": "anthropic",
            "target_protocol": "responses", "is_conversion": True, "kind": "a2r",
        }, hints)

    def test_count_tokens_only_anthropic(self):
        self.assertTrue(operation_compatible("count_tokens", "anthropic"))
        self.assertFalse(operation_compatible("count_tokens", "responses"))
        self.assertTrue(operation_compatible("messages", "responses"))


if __name__ == "__main__":
    unittest.main()
