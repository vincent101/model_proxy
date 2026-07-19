"""core.reasoning 领域层单测（ladder/capability/codecs/registry），脱网络纯标准库 unittest。

覆盖：
  - ladder：budget↔canonical 边界、canonical_to_budget 反算表。
  - capability：from_config 缺省行为、align() 钳位规则（超界/精确命中/最近邻/并列取高）。
  - codecs：三协议 decode/encode 边界、canonical↔各协议档名映射、
    AnthropicReasoningCodec.interpret_rejection 全部分支。
  - registry：get_codec 单例、apply_fields 语义。
  - 单调性属性测试：align() 对 intent.level 单调不减（含短枚举 cap 穷举验证）。
  - 三路径一致性测试：同一 intent + 同一 capability，经不同协议组合对齐出的 canonical level 相同。

运行：cd tools/model_proxy && python3 -m unittest tests.test_reasoning -v
"""

import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.reasoning.capability import AlignedEffort, ReasoningCapability, align
from core.reasoning.codecs import (
    ANTHROPIC_ADAPTIVE,
    ANTHROPIC_ENABLED,
    AnthropicReasoningCodec,
    CHAT_EFFORT,
    ChatReasoningCodec,
    RESP_EFFORT,
    ResponsesReasoningCodec,
)
from core.reasoning.ladder import CanonicalEffort as CE
from core.reasoning.ladder import ReasoningIntent, budget_to_canonical, canonical_to_budget, name_to_canonical
from core.reasoning.registry import apply_fields, get_codec


# ============================================================================
# ladder
# ============================================================================

class TestLadder(unittest.TestCase):

    def test_budget_to_canonical_boundaries(self):
        self.assertEqual(budget_to_canonical(0), CE.LOW)
        self.assertEqual(budget_to_canonical(1999), CE.LOW)
        self.assertEqual(budget_to_canonical(2000), CE.MEDIUM)
        self.assertEqual(budget_to_canonical(7999), CE.MEDIUM)
        self.assertEqual(budget_to_canonical(8000), CE.HIGH)
        self.assertEqual(budget_to_canonical(31999), CE.HIGH)
        self.assertEqual(budget_to_canonical(32000), CE.XHIGH)
        self.assertEqual(budget_to_canonical(63999), CE.XHIGH)
        self.assertEqual(budget_to_canonical(64000), CE.MAX)
        self.assertEqual(budget_to_canonical(1_000_000), CE.MAX)

    def test_canonical_to_budget_representative_values(self):
        # 代表值必须落在 budget_to_canonical 对应区间*内部*，不能卡在断点上，
        # 否则往返会漂移一档（见 test_budget_roundtrip）。
        self.assertEqual(canonical_to_budget(CE.LOW), 1500)
        self.assertEqual(canonical_to_budget(CE.MEDIUM), 5000)
        self.assertEqual(canonical_to_budget(CE.HIGH), 16000)
        self.assertEqual(canonical_to_budget(CE.XHIGH), 48000)
        self.assertEqual(canonical_to_budget(CE.MAX), 128000)

    def test_canonical_to_budget_monotonic(self):
        levels = [CE.OFF, CE.MINIMAL, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH, CE.MAX]
        budgets = [canonical_to_budget(l) for l in levels]
        self.assertEqual(budgets, sorted(budgets))

    def test_budget_roundtrip(self):
        """canonical_to_budget 反算的代表值喂回 budget_to_canonical 必须往返一致，
        不能因为代表值卡在分档边界而漂移一档（bug 修复回归测试）。"""
        for level in [CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH]:
            budget = canonical_to_budget(level)
            self.assertEqual(
                budget_to_canonical(budget), level,
                f"{level.name} 往返不一致: canonical_to_budget={budget} -> "
                f"budget_to_canonical={budget_to_canonical(budget).name}")

    def test_name_to_canonical(self):
        self.assertEqual(name_to_canonical("low"), CE.LOW)
        self.assertEqual(name_to_canonical("none"), CE.OFF)
        self.assertEqual(name_to_canonical("off"), CE.OFF)
        self.assertEqual(name_to_canonical("max"), CE.MAX)
        self.assertIsNone(name_to_canonical("bogus"))
        self.assertIsNone(name_to_canonical(None))
        self.assertIsNone(name_to_canonical(123))


# ============================================================================
# capability
# ============================================================================

class TestCapabilityFromConfig(unittest.TestCase):

    def test_default_when_no_supply(self):
        cap = ReasoningCapability.from_config(None)
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))
        self.assertEqual(cap.off_alias, CE.OFF)

    def test_default_when_empty_dict(self):
        cap = ReasoningCapability.from_config({})
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))

    def test_glm_style_capability(self):
        supply = {"reasoning_capability": {
            "effort_enum": ["none", "minimal", "low", "medium", "high"],
        }}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.OFF, CE.MINIMAL, CE.LOW, CE.MEDIUM, CE.HIGH))
        self.assertEqual(cap.off_alias, CE.OFF)   # enum 含 OFF，缺省 off_alias=OFF

    def test_off_alias_none_when_enum_lacks_off(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "medium", "high"]}}
        cap = ReasoningCapability.from_config(supply)
        self.assertIsNone(cap.off_alias)

    def test_off_alias_explicit_override(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "high"], "off_alias": "low"}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.off_alias, CE.LOW)

    def test_off_alias_explicit_not_in_enum_falls_back_none(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "high"], "off_alias": "medium"}}
        cap = ReasoningCapability.from_config(supply)
        self.assertIsNone(cap.off_alias)

    def test_effort_enum_dedup_and_sorted(self):
        supply = {"reasoning_capability": {"effort_enum": ["high", "low", "low", "medium"]}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.LOW, CE.MEDIUM, CE.HIGH))

    def test_effort_enum_unrecognized_names_ignored(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "bogus", "high"]}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.LOW, CE.HIGH))

    def test_effort_enum_all_unrecognized_yields_empty(self):
        # effort_enum 键存在即代表显式声明，全非法名解析后为空也是 ()，不回退默认5档
        # （区别于键缺失才回退默认5档，见 test_default_when_empty_dict）。
        supply = {"reasoning_capability": {"effort_enum": ["bogus1", "bogus2"]}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, ())

    def test_effort_enum_key_present_empty_list_yields_empty_enum(self):
        # effort_enum 显式配置为空列表 → () 空元组，代表该 supply 不支持任何档位
        # （0档场景，区别于键完全缺失时的默认5档兜底）。
        supply = {"reasoning_capability": {"effort_enum": []}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, ())
        self.assertIsNone(cap.off_alias)

    def test_effort_enum_key_missing_still_defaults(self):
        # reasoning_capability 存在但没有 effort_enum 键 → 未配置，回归默认5档。
        supply = {"reasoning_capability": {"off_alias": "low"}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))

    def test_effort_enum_short_enum_two_levels(self):
        supply = {"reasoning_capability": {"effort_enum": ["high", "max"]}}
        cap = ReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.HIGH, CE.MAX))


class TestAlign(unittest.TestCase):

    def setUp(self):
        self.default_cap = ReasoningCapability.from_config(None)

    def _intent(self, level=None, source_budget=None, explicit_off=False, present=True):
        return ReasoningIntent(level=level, source_budget=source_budget,
                               explicit_off=explicit_off, present=present)

    def test_not_present_returns_none(self):
        out = align(self._intent(present=False), self.default_cap)
        self.assertEqual(out, AlignedEffort(level=None, source_budget=None))

    def test_explicit_off_uses_off_alias(self):
        out = align(self._intent(explicit_off=True), self.default_cap)
        self.assertEqual(out.level, CE.OFF)

    def test_explicit_off_with_no_off_alias(self):
        cap = ReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None)
        out = align(self._intent(explicit_off=True), cap)
        self.assertIsNone(out.level)

    def test_max_clamped_to_enum_highest(self):
        cap = ReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        out = align(self._intent(level=CE.MAX), cap)
        self.assertEqual(out.level, CE.HIGH)

    def test_exact_match_passthrough(self):
        cap = ReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        out = align(self._intent(level=CE.MEDIUM), cap)
        self.assertEqual(out.level, CE.MEDIUM)

    def test_clamp_above_highest(self):
        cap = ReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        out = align(self._intent(level=CE.XHIGH), cap)
        self.assertEqual(out.level, CE.HIGH)

    def test_clamp_below_lowest(self):
        cap = ReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        out = align(self._intent(level=CE.OFF), cap)
        self.assertEqual(out.level, CE.LOW)

    def test_nearest_neighbor_non_tie(self):
        # enum=[LOW, XHIGH]，intent=MEDIUM：距 LOW 距离 1，距 XHIGH 距离 3 → LOW
        cap = ReasoningCapability(enum=(CE.LOW, CE.XHIGH), off_alias=None)
        out = align(self._intent(level=CE.MEDIUM), cap)
        self.assertEqual(out.level, CE.LOW)

    def test_nearest_neighbor_tie_prefers_higher(self):
        # enum=[OFF, XHIGH]，intent=MEDIUM(3)：距 OFF(0) 距离3，距 XHIGH(5) 距离2 → 非并列，验证独立并列场景
        # 构造真正并列：enum=[LOW(2), HIGH(4)]，intent=MEDIUM(3)：距 LOW 距离1，距 HIGH 距离1 → 并列取更高档 HIGH
        cap = ReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None)
        out = align(self._intent(level=CE.MEDIUM), cap)
        self.assertEqual(out.level, CE.HIGH)

    def test_source_budget_passthrough(self):
        out = align(self._intent(level=CE.HIGH, source_budget=12345), self.default_cap)
        self.assertEqual(out.source_budget, 12345)

    def test_source_budget_none_when_not_present(self):
        out = align(self._intent(present=False, source_budget=999), self.default_cap)
        self.assertIsNone(out.source_budget)

    def test_empty_enum_level_present_returns_none(self):
        cap = ReasoningCapability.from_config({"reasoning_capability": {"effort_enum": []}})
        out = align(self._intent(level=CE.HIGH), cap)
        self.assertIsNone(out.level)

    def test_empty_enum_explicit_off_returns_none(self):
        cap = ReasoningCapability.from_config({"reasoning_capability": {"effort_enum": []}})
        out = align(self._intent(explicit_off=True), cap)
        self.assertIsNone(out.level)

    def test_empty_enum_level_none_returns_none(self):
        cap = ReasoningCapability.from_config({"reasoning_capability": {"effort_enum": []}})
        out = align(self._intent(level=None), cap)
        self.assertIsNone(out.level)

    def test_empty_enum_source_budget_still_passed_through(self):
        cap = ReasoningCapability.from_config({"reasoning_capability": {"effort_enum": []}})
        out = align(self._intent(level=CE.HIGH, source_budget=999), cap)
        self.assertIsNone(out.level)
        self.assertEqual(out.source_budget, 999)

    def test_short_enum_two_levels_monotonic_clamp(self):
        cap = ReasoningCapability.from_config(
            {"reasoning_capability": {"effort_enum": ["high", "max"]}})
        self.assertEqual(align(self._intent(level=CE.OFF), cap).level, CE.HIGH)
        self.assertEqual(align(self._intent(level=CE.LOW), cap).level, CE.HIGH)
        self.assertEqual(align(self._intent(level=CE.HIGH), cap).level, CE.HIGH)
        # XHIGH(5) 与 HIGH(4)/MAX(6) 距离并列(各1) → 取更高档 MAX
        self.assertEqual(align(self._intent(level=CE.XHIGH), cap).level, CE.MAX)
        self.assertEqual(align(self._intent(level=CE.MAX), cap).level, CE.MAX)


# ============================================================================
# 单调性属性测试（architect 特别强调，穷举验证防倒挂）
# ============================================================================

class TestMonotonicity(unittest.TestCase):
    """对任意两个 intent，若 intent1.level <= intent2.level，
    则 align(intent1,cap).level <= align(intent2,cap).level（用几组不同 cap 验证）。"""

    def _caps(self):
        return [
            ReasoningCapability.from_config(None),                                    # 默认 5 档
            ReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None),     # 3 档短枚举
            ReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None),                # 2 档跳档
            ReasoningCapability(enum=(CE.OFF, CE.XHIGH), off_alias=CE.OFF),             # 极端两端
            ReasoningCapability.from_config({"reasoning_capability": {
                "effort_enum": ["none", "minimal", "low", "medium", "high"]}}),
        ]

    def test_monotonic_exhaustive_pairs(self):
        levels = list(CE)
        for cap in self._caps():
            for l1, l2 in itertools.combinations_with_replacement(levels, 2):
                lo, hi = (l1, l2) if l1 <= l2 else (l2, l1)
                intent_lo = ReasoningIntent(level=lo, source_budget=None, explicit_off=False, present=True)
                intent_hi = ReasoningIntent(level=hi, source_budget=None, explicit_off=False, present=True)
                out_lo = align(intent_lo, cap).level
                out_hi = align(intent_hi, cap).level
                self.assertLessEqual(
                    out_lo, out_hi,
                    f"单调性破坏: cap.enum={cap.enum} lo={lo!r}->{out_lo!r} hi={hi!r}->{out_hi!r}")


# ============================================================================
# 三路径一致性测试
# ============================================================================

class TestCrossProtocolConsistency(unittest.TestCase):
    """同一 intent + 同一 capability，经不同协议组合对齐出的 canonical level 相同。

    验证的是 align() 本身的确定性（align 只依赖 intent+cap，不依赖走的是哪个协议
    组合），这正是要消除的"三套实现各给不同结果"问题的核心断言。
    """

    def test_same_intent_same_cap_yields_same_aligned_level_regardless_of_route(self):
        cap = ReasoningCapability.from_config(None)
        anthropic_codec = get_codec("anthropic")
        chat_codec = get_codec("chat")
        responses_codec = get_codec("responses")

        # PASSTHROUGH（anthropic→anthropic）：decode 用 anthropic
        intent_passthrough = anthropic_codec.decode(
            {"thinking": {"type": "enabled", "budget_tokens": 40000}})
        level_passthrough = align(intent_passthrough, cap).level

        # ANTHROPIC_TO_CHAT：decode 用 anthropic（source 相同，body 相同）
        intent_a2c = anthropic_codec.decode(
            {"thinking": {"type": "enabled", "budget_tokens": 40000}})
        level_a2c = align(intent_a2c, cap).level

        # ANTHROPIC_TO_RESPONSES：decode 用 anthropic
        intent_a2r = anthropic_codec.decode(
            {"thinking": {"type": "enabled", "budget_tokens": 40000}})
        level_a2r = align(intent_a2r, cap).level

        self.assertEqual(level_passthrough, level_a2c)
        self.assertEqual(level_a2c, level_a2r)
        self.assertEqual(level_passthrough, CE.XHIGH)

    def test_responses_to_anthropic_and_anthropic_to_responses_agree_on_shared_domain_name(self):
        # responses "high" decode 出的 canonical，与 anthropic adaptive "high" decode 出的 canonical 一致
        cap = ReasoningCapability.from_config(None)
        intent_from_responses = get_codec("responses").decode({"reasoning": {"effort": "high"}})
        intent_from_anthropic = get_codec("anthropic").decode(
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})
        self.assertEqual(align(intent_from_responses, cap).level, align(intent_from_anthropic, cap).level)


# ============================================================================
# codecs：AnthropicReasoningCodec
# ============================================================================

class TestAnthropicCodecDecode(unittest.TestCase):

    def setUp(self):
        self.codec = AnthropicReasoningCodec()

    def test_disabled(self):
        intent = self.codec.decode({"thinking": {"type": "disabled"}})
        self.assertTrue(intent.present)
        self.assertTrue(intent.explicit_off)
        self.assertIsNone(intent.level)

    def test_enabled_budget(self):
        intent = self.codec.decode({"thinking": {"type": "enabled", "budget_tokens": 1000}})
        self.assertTrue(intent.present)
        self.assertFalse(intent.explicit_off)
        self.assertEqual(intent.level, CE.LOW)
        self.assertEqual(intent.source_budget, 1000)

    def test_enabled_default_budget_when_missing(self):
        intent = self.codec.decode({"thinking": {"type": "enabled"}})
        self.assertEqual(intent.source_budget, 10000)

    def test_adaptive_with_effort(self):
        intent = self.codec.decode({"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})
        self.assertEqual(intent.level, CE.HIGH)
        self.assertIsNone(intent.source_budget)

    def test_adaptive_max(self):
        intent = self.codec.decode({"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}})
        self.assertEqual(intent.level, CE.MAX)

    def test_adaptive_without_effort_defaults_medium(self):
        intent = self.codec.decode({"thinking": {"type": "adaptive"}})
        self.assertEqual(intent.level, CE.MEDIUM)

    def test_bare_output_config_not_present(self):
        intent = self.codec.decode({"output_config": {"effort": "high"}})
        self.assertFalse(intent.present)

    def test_missing_thinking_not_present(self):
        intent = self.codec.decode({})
        self.assertFalse(intent.present)
        intent2 = self.codec.decode({"thinking": None})
        self.assertFalse(intent2.present)


class TestAnthropicCodecEncode(unittest.TestCase):

    def setUp(self):
        self.codec = AnthropicReasoningCodec()
        self.cap = ReasoningCapability.from_config(None)

    def test_none_level_returns_empty(self):
        self.assertEqual(self.codec.encode(AlignedEffort(None, None), self.cap, ANTHROPIC_ENABLED), {})

    def test_off_level_returns_disabled(self):
        out = self.codec.encode(AlignedEffort(CE.OFF, None), self.cap, ANTHROPIC_ENABLED)
        self.assertEqual(out["thinking"], {"type": "disabled"})
        self.assertIsNone(out["output_config"])  # 约定：None 表示删除该 key

    def test_enabled_variant_backfills_source_budget(self):
        out = self.codec.encode(AlignedEffort(CE.HIGH, 12345), self.cap, ANTHROPIC_ENABLED)
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": 12345})
        self.assertIsNone(out["output_config"])

    def test_enabled_variant_fallback_when_no_source_budget(self):
        out = self.codec.encode(AlignedEffort(CE.HIGH, None), self.cap, ANTHROPIC_ENABLED)
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": canonical_to_budget(CE.HIGH)})

    def test_adaptive_variant(self):
        out = self.codec.encode(AlignedEffort(CE.HIGH, None), self.cap, ANTHROPIC_ADAPTIVE)
        self.assertEqual(out["thinking"], {"type": "adaptive"})
        self.assertEqual(out["output_config"], {"effort": "high"})

    def test_adaptive_variant_max_level(self):
        out = self.codec.encode(AlignedEffort(CE.MAX, None), self.cap, ANTHROPIC_ADAPTIVE)
        self.assertEqual(out["output_config"], {"effort": "max"})


class TestAnthropicInterpretRejection(unittest.TestCase):
    """覆盖 AnthropicReasoningCodec.interpret_rejection 全部规则分支
    （原 server.py::_parse_thinking_error 场景搬入）。"""

    def setUp(self):
        self.codec = AnthropicReasoningCodec()

    def test_enabled_not_supported_switches_to_adaptive(self):
        body = b'{"error":{"message":"thinking.type.enabled is not supported"}}'
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ENABLED), ANTHROPIC_ADAPTIVE)

    def test_budget_tokens_not_supported_switches_to_adaptive(self):
        body = b'{"error":{"message":"budget_tokens is not supported for this model"}}'
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ENABLED), ANTHROPIC_ADAPTIVE)

    def test_adaptive_not_supported_switches_to_enabled(self):
        body = b'{"error":{"message":"thinking.type.adaptive is not supported"}}'
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ADAPTIVE), ANTHROPIC_ENABLED)

    def test_output_config_not_permitted_switches_to_enabled(self):
        body = b'{"error":{"message":"output_config is not permitted for this model"}}'
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ADAPTIVE), ANTHROPIC_ENABLED)

    def test_output_config_not_allowed_switches_to_enabled(self):
        body = b'{"error":{"message":"output_config not allowed here"}}'
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ADAPTIVE), ANTHROPIC_ENABLED)

    def test_glm_chinese_generic_error_switches_to_enabled(self):
        body = "参数有误，请检查请求体".encode("utf-8")
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ADAPTIVE), ANTHROPIC_ENABLED)

    def test_invalid_parameter_english_switches_to_enabled(self):
        body = b'{"error":{"message":"Invalid parameter: xyz"}}'
        self.assertEqual(self.codec.interpret_rejection(body, ANTHROPIC_ADAPTIVE), ANTHROPIC_ENABLED)

    def test_unrecognized_error_returns_none(self):
        body = b'{"error":{"message":"rate limit exceeded"}}'
        self.assertIsNone(self.codec.interpret_rejection(body, ANTHROPIC_ENABLED))

    def test_undecodable_bytes_returns_none_gracefully(self):
        # errors="replace" 保证不抛异常，只是可能匹配不到规则
        body = b"\xff\xfe\x00garbage"
        result = self.codec.interpret_rejection(body, ANTHROPIC_ENABLED)
        self.assertIn(result, (None, ANTHROPIC_ADAPTIVE, ANTHROPIC_ENABLED))


# ============================================================================
# codecs：Chat / Responses（单变体）
# ============================================================================

class TestChatCodec(unittest.TestCase):

    def setUp(self):
        self.codec = ChatReasoningCodec()
        self.cap = ReasoningCapability.from_config(None)

    def test_decode_present(self):
        intent = self.codec.decode({"reasoning_effort": "high"})
        self.assertTrue(intent.present)
        self.assertEqual(intent.level, CE.HIGH)

    def test_decode_none_effort_not_present(self):
        intent = self.codec.decode({"reasoning_effort": None})
        self.assertFalse(intent.present)

    def test_decode_missing_not_present(self):
        intent = self.codec.decode({})
        self.assertFalse(intent.present)

    def test_decode_unrecognized_value_not_present(self):
        intent = self.codec.decode({"reasoning_effort": "bogus"})
        self.assertFalse(intent.present)

    def test_encode_none_level_empty(self):
        self.assertEqual(self.codec.encode(AlignedEffort(None, None), self.cap, CHAT_EFFORT), {})

    def test_encode_max_clamped_to_xhigh(self):
        out = self.codec.encode(AlignedEffort(CE.MAX, None), self.cap, CHAT_EFFORT)
        self.assertEqual(out, {"reasoning_effort": "xhigh"})

    def test_encode_off_to_none(self):
        out = self.codec.encode(AlignedEffort(CE.OFF, None), self.cap, CHAT_EFFORT)
        self.assertEqual(out, {"reasoning_effort": "none"})

    def test_encode_regular_levels(self):
        for level, name in [(CE.LOW, "low"), (CE.MEDIUM, "medium"),
                            (CE.HIGH, "high"), (CE.XHIGH, "xhigh")]:
            out = self.codec.encode(AlignedEffort(level, None), self.cap, CHAT_EFFORT)
            self.assertEqual(out, {"reasoning_effort": name})

    def test_interpret_rejection_always_none(self):
        self.assertIsNone(self.codec.interpret_rejection(b"anything", CHAT_EFFORT))

    def test_select_variant_single(self):
        self.assertEqual(self.codec.select_variant({}), CHAT_EFFORT)
        self.assertEqual(self.codec.select_variant({"variant": "whatever"}), CHAT_EFFORT)


class TestResponsesCodec(unittest.TestCase):

    def setUp(self):
        self.codec = ResponsesReasoningCodec()
        self.cap = ReasoningCapability.from_config(None)

    def test_decode_present(self):
        intent = self.codec.decode({"reasoning": {"effort": "medium"}})
        self.assertTrue(intent.present)
        self.assertEqual(intent.level, CE.MEDIUM)

    def test_decode_missing_not_present(self):
        intent = self.codec.decode({})
        self.assertFalse(intent.present)
        intent2 = self.codec.decode({"reasoning": {"effort": None}})
        self.assertFalse(intent2.present)

    def test_encode_max_clamped_to_xhigh(self):
        out = self.codec.encode(AlignedEffort(CE.MAX, None), self.cap, RESP_EFFORT)
        self.assertEqual(out, {"reasoning": {"effort": "xhigh"}})

    def test_encode_none_level_empty(self):
        self.assertEqual(self.codec.encode(AlignedEffort(None, None), self.cap, RESP_EFFORT), {})

    def test_interpret_rejection_always_none(self):
        self.assertIsNone(self.codec.interpret_rejection(b"anything", RESP_EFFORT))


# ============================================================================
# registry
# ============================================================================

class TestRegistry(unittest.TestCase):

    def test_get_codec_returns_singletons(self):
        self.assertIs(get_codec("anthropic"), get_codec("anthropic"))
        self.assertIs(get_codec("chat"), get_codec("chat"))
        self.assertIs(get_codec("responses"), get_codec("responses"))

    def test_get_codec_types(self):
        self.assertIsInstance(get_codec("anthropic"), AnthropicReasoningCodec)
        self.assertIsInstance(get_codec("chat"), ChatReasoningCodec)
        self.assertIsInstance(get_codec("responses"), ResponsesReasoningCodec)

    def test_get_codec_unknown_raises(self):
        with self.assertRaises(KeyError):
            get_codec("bogus")

    def test_apply_fields_sets_and_deletes(self):
        body = {"output_config": {"effort": "high"}, "keep": 1}
        apply_fields(body, {"thinking": {"type": "disabled"}, "output_config": None})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("output_config", body)
        self.assertEqual(body["keep"], 1)

    def test_apply_fields_empty_noop(self):
        body = {"a": 1}
        apply_fields(body, {})
        apply_fields(body, None)
        self.assertEqual(body, {"a": 1})


# ============================================================================
# _forward 多轮循环内 reasoning_intent 不被污染（bug 修复回归测试）
#
# server.py::_forward 本身依赖 HTTPServer/BaseHTTPRequestHandler，不适合直接实例化
# 单测；这里复现的是修复前后行为差异的核心不变量：decode() 只应对客户端原始 body
# 调用一次，不应在循环体对"已被上一轮 apply_fields 写入结果"的同一个 body 重新
# decode。用 codec 直接模拟 _forward 循环两轮的最小逻辑骨架来断言这个不变量。
# ============================================================================

class TestForwardLoopReasoningIntentNotPolluted(unittest.TestCase):

    def setUp(self):
        self.codec = get_codec("anthropic")
        self.cap = ReasoningCapability.from_config(None)

    def test_fixed_behavior_decode_once_reused_across_rounds(self):
        """修复后的正确做法：decode 在循环外只调用一次，循环内复用同一个 intent，
        即使 body_json 被 apply_fields 原地改写，intent 也不受影响。"""
        body = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        reasoning_intent = self.codec.decode(body)  # 循环外，只调用一次
        self.assertEqual(reasoning_intent.level, CE.HIGH)

        # round 1：align + encode(enabled variant) + apply_fields 原地写回 body
        aligned1 = align(reasoning_intent, self.cap)
        fields1 = self.codec.encode(aligned1, self.cap, ANTHROPIC_ENABLED)
        apply_fields(body, fields1)
        self.assertEqual(body["thinking"]["type"], "enabled")

        # round 2（模拟上游 400 拒绝后重试）：不重新 decode，直接复用 reasoning_intent
        aligned2 = align(reasoning_intent, self.cap)
        fields2 = self.codec.encode(aligned2, self.cap, ANTHROPIC_ADAPTIVE)
        apply_fields(body, fields2)

        # 断言：即便 body 已经被两轮写入污染，reasoning_intent 本身岿然不变
        self.assertEqual(reasoning_intent.level, CE.HIGH)
        self.assertEqual(aligned1.level, CE.HIGH)
        self.assertEqual(aligned2.level, CE.HIGH)

    def test_anti_pattern_redecoding_polluted_body_permanently_clamps_across_supplies(self):
        """反向验证修复2的必要性：failover 跨到 capability 不同的 supply 时，若在循环内
        对被污染的 body 重新 decode（修复前的错误做法），原始高强度意图会被第一轮命中的
        窄 capability 永久钳低，即使换到支持更高档的 supply 也恢复不了（README 承诺的
        "按强度就近钳位，不会强度倒挂"被违反）。修复1（区间内部代表值）只解决同档反算的
        往返漂移，不解决这个跨 capability 的永久钳低问题——必须靠修复2（decode 只用一次
        不变的原始 intent）解决。"""
        body = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "xhigh"}}
        original_intent = self.codec.decode(body)
        self.assertEqual(original_intent.level, CE.XHIGH)

        # round 1：命中一个窄 capability 的 supply（只到 HIGH），钳低后 encode 写回 body
        narrow_cap = ReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        aligned1 = align(original_intent, narrow_cap)
        self.assertEqual(aligned1.level, CE.HIGH)  # 被钳到 HIGH
        fields1 = self.codec.encode(aligned1, narrow_cap, ANTHROPIC_ENABLED)
        apply_fields(body, fields1)  # body 被写成 budget_tokens=16000（HIGH 代表值）

        # round 2：failover 换到支持 XHIGH 的宽 capability supply
        wide_cap = ReasoningCapability.from_config(None)  # OFF..XHIGH

        # 错误做法（修复前）：对已被污染的 body 重新 decode，拿到的是 round1 钳低后的 HIGH，
        # 不是客户端原始的 XHIGH ——即使 supply 换了、capability 变宽了也恢复不了。
        intent_round2_buggy = self.codec.decode(body)
        aligned2_buggy = align(intent_round2_buggy, wide_cap)
        self.assertEqual(intent_round2_buggy.level, CE.HIGH)
        self.assertEqual(aligned2_buggy.level, CE.HIGH,
                          "永久钳低复现：即使 round2 capability 支持 XHIGH，"
                          "重新 decode 被污染的 body 也只能拿到 round1 钳低后的 HIGH")

        # 正确做法（修复后）：复用循环外算好的 original_intent，重新 align 到新 capability，
        # 强度能正确恢复到 XHIGH。
        aligned2_fixed = align(original_intent, wide_cap)
        self.assertEqual(aligned2_fixed.level, CE.XHIGH,
                          "修复后：换到支持 XHIGH 的 supply，强度应恢复到客户端原始意图 XHIGH")


if __name__ == "__main__":
    unittest.main()
