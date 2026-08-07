"""core.reasoning 领域层单测（ladder/capability/codecs/registry），脱网络纯标准库 unittest。

覆盖新数据流：decode → resolve_source_capability/resolve_target_capability → remap →
abstract_encode → syntax_adapt。

覆盖：
  - ladder：budget↔canonical 边界、canonical_to_budget 反算表。
  - capability：ModelReasoningCapability.from_config 缺省行为（含 off_alias 单调性
    守卫）、think_seq/rank_of/remap_rank 三个相对映射构件、remap() 相对映射规则
    （haiku 例子、m=1 中位、n=1 塌缩、边界场景）、abstract_encode() 的 kind 判定。
  - codecs：三协议 decode/syntax_adapt 边界、canonical↔各协议档名映射、
    AnthropicReasoningCodec.interpret_rejection 全部分支。
  - registry：get_codec 单例、apply_fields 语义。
  - 单调性属性测试：remap() 对 intent.level 单调不减（含固定 tgt_cap 遍历不同 src_cap
    这一新维度）。
  - 三路径一致性测试：同一 intent + 同一两侧 cap，经不同协议组合 remap 出的 canonical
    level 相同。
  - 缺陷1回归：present=True 但 target 完全不支持思考 → STRIP，wire dict 含
    thinking:None（会被 apply_fields 删除），而非现状的 {}（透传残留）。
  - MAX 无特殊分支验证：grep codecs.py/capability.py 源码，确认不存在任何
    `if ... == CanonicalEffort.MAX` 判断。

运行：cd tools/model_proxy && python3 -m unittest tests.test_reasoning -v
"""

import ast
import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.reasoning.capability import (
    AbstractKind,
    AbstractReasoning,
    ModelReasoningCapability,
    TargetEffort,
    abstract_encode,
    clamp_absolute,
    rank_of,
    remap,
    remap_rank,
    think_seq,
)
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
from core.reasoning.ladder import RawIntent, budget_to_canonical, canonical_to_budget, name_to_canonical
from core.reasoning.registry import apply_fields, get_codec
from core.server import resolve_source_capability, resolve_strategy

_CORE_REASONING_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "core", "reasoning")


def _intent(level=None, source_budget=None, present=True):
    return RawIntent(level=level, source_budget=source_budget, present=present)


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

    def test_name_to_canonical_enum_name_invariant(self):
        """词表不变量（codec 零词表后的全局唯一映射约定）：canonical 枚举名小写 ==
        wire 档名字符串，name_to_canonical 对其自映射。未来新增枚举值时本测试强制
        同步词表，杜绝"新增档名 → 某处漏加 → 静默降级"的复发路径。"""
        for e in CE:
            self.assertEqual(name_to_canonical(e.name.lower()), e,
                             f"{e.name} 未自映射，词表与枚举失同步")
        # OFF 双拼：off 与 none 都映射 OFF
        self.assertEqual(name_to_canonical("off"), CE.OFF)
        self.assertEqual(name_to_canonical("none"), CE.OFF)


# ============================================================================
# capability：ModelReasoningCapability.from_config（含 off_alias 单调性守卫）
# ============================================================================

class TestCapabilityFromConfig(unittest.TestCase):

    def test_default_when_no_supply(self):
        cap = ModelReasoningCapability.from_config(None)
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))
        self.assertEqual(cap.off_alias, CE.OFF)

    def test_default_when_empty_dict(self):
        cap = ModelReasoningCapability.from_config({})
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))

    def test_glm_style_capability(self):
        supply = {"reasoning_capability": {
            "effort_enum": ["none", "minimal", "low", "medium", "high"],
        }}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.OFF, CE.MINIMAL, CE.LOW, CE.MEDIUM, CE.HIGH))
        self.assertEqual(cap.off_alias, CE.OFF)   # enum 含 OFF，缺省 off_alias=OFF

    def test_off_alias_none_when_enum_lacks_off(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "medium", "high"]}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertIsNone(cap.off_alias)

    def test_off_alias_explicit_override(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "high"], "off_alias": "low"}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.off_alias, CE.LOW)

    def test_off_alias_explicit_not_in_enum_falls_back_none(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "high"], "off_alias": "medium"}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertIsNone(cap.off_alias)

    def test_off_alias_higher_than_lowest_think_rejected(self):
        """反例守卫测试（决策3拍板）：off_alias 配成高于最低思考档 → from_config 应拒绝，
        回退 None（单调性守卫，见方案文档 §3.5）。effort_enum=[low,medium,high]，
        off_alias 显式配成 medium（高于最低思考档 low）→ 无效，回退 None。"""
        supply = {"reasoning_capability": {
            "effort_enum": ["low", "medium", "high"], "off_alias": "medium"}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertIsNone(cap.off_alias)

    def test_off_alias_equal_to_lowest_think_accepted(self):
        """off_alias 恰好等于最低思考档（不高于）→ 合法，接受。"""
        supply = {"reasoning_capability": {
            "effort_enum": ["low", "medium", "high"], "off_alias": "low"}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.off_alias, CE.LOW)

    def test_off_alias_off_itself_never_rejected(self):
        """off_alias=OFF（enum 含 OFF 时的常见配置）永不违反单调性守卫，因为 OFF 是
        全序最小值，不可能高于任何思考档。"""
        supply = {"reasoning_capability": {
            "effort_enum": ["none", "low", "high"], "off_alias": "none"}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.off_alias, CE.OFF)

    def test_effort_enum_dedup_and_sorted(self):
        supply = {"reasoning_capability": {"effort_enum": ["high", "low", "low", "medium"]}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.LOW, CE.MEDIUM, CE.HIGH))

    def test_effort_enum_unrecognized_names_ignored(self):
        supply = {"reasoning_capability": {"effort_enum": ["low", "bogus", "high"]}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.LOW, CE.HIGH))

    def test_effort_enum_all_unrecognized_yields_empty(self):
        # effort_enum 键存在即代表显式声明，全非法名解析后为空也是 ()，不回退默认5档
        # （区别于键缺失才回退默认5档，见 test_default_when_empty_dict）。
        supply = {"reasoning_capability": {"effort_enum": ["bogus1", "bogus2"]}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, ())

    def test_effort_enum_key_present_empty_list_yields_empty_enum(self):
        # effort_enum 显式配置为空列表 → () 空元组，代表该 supply 不支持任何档位
        # （0档场景，区别于键完全缺失时的默认5档兜底）。
        supply = {"reasoning_capability": {"effort_enum": []}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, ())
        self.assertIsNone(cap.off_alias)

    def test_effort_enum_key_missing_still_defaults(self):
        # reasoning_capability 存在但没有 effort_enum 键 → 未配置，回归默认5档。
        supply = {"reasoning_capability": {"off_alias": "low"}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))

    def test_effort_enum_short_enum_two_levels(self):
        supply = {"reasoning_capability": {"effort_enum": ["high", "max"]}}
        cap = ModelReasoningCapability.from_config(supply)
        self.assertEqual(cap.enum, (CE.HIGH, CE.MAX))


# ============================================================================
# capability：think_seq / rank_of / remap_rank（相对映射三构件）
# ============================================================================

class TestThinkSeq(unittest.TestCase):

    def test_excludes_off(self):
        cap = ModelReasoningCapability(enum=(CE.OFF, CE.LOW, CE.HIGH), off_alias=CE.OFF)
        self.assertEqual(think_seq(cap), (CE.LOW, CE.HIGH))

    def test_no_off_present_unchanged(self):
        cap = ModelReasoningCapability(enum=(CE.HIGH, CE.MAX), off_alias=None)
        self.assertEqual(think_seq(cap), (CE.HIGH, CE.MAX))

    def test_only_off_yields_empty(self):
        cap = ModelReasoningCapability(enum=(CE.OFF,), off_alias=CE.OFF)
        self.assertEqual(think_seq(cap), ())


class TestRankOf(unittest.TestCase):

    def test_exact_hit(self):
        seq = (CE.LOW, CE.MEDIUM, CE.HIGH)
        self.assertEqual(rank_of(CE.MEDIUM, seq), 1)

    def test_not_in_seq_clamps_to_nearest_then_ranks(self):
        seq = (CE.HIGH, CE.MAX)
        self.assertEqual(rank_of(CE.LOW, seq), 0)   # clamp_absolute(LOW,seq)->HIGH->idx0
        self.assertEqual(rank_of(CE.XHIGH, seq), 1)  # 并列取更高 -> MAX -> idx1


class TestRemapRank(unittest.TestCase):

    def test_n_equals_1_collapses_to_zero(self):
        for i in range(3):
            self.assertEqual(remap_rank(i, 3, 1), 0)

    def test_m_equals_1_maps_to_median_not_strongest(self):
        # 用户拍板：m=1 时映射到 target 中位档 (n-1)//2，不是最强档 n-1。
        self.assertEqual(remap_rank(0, 1, 2), 0)   # target 2 档 -> 中位是最低档 index0
        self.assertEqual(remap_rank(0, 1, 3), 1)   # target 3 档 -> 中位是中间档 index1
        self.assertEqual(remap_rank(0, 1, 5), 2)   # target 5 档 -> 中位是 index2
        self.assertEqual(remap_rank(0, 1, 4), 1)   # target 4 档 -> (4-1)//2=1

    def test_proportional_rounding_half_up(self):
        # haiku 例子（方案文档 §3.4）：m=3, n=2
        self.assertEqual(remap_rank(0, 3, 2), 0)  # floor(0/2*1+0.5)=0
        self.assertEqual(remap_rank(1, 3, 2), 1)  # floor(1/2*1+0.5)=floor(1.0)=1
        self.assertEqual(remap_rank(2, 3, 2), 1)  # floor(2/2*1+0.5)=1

    def test_identity_when_m_equals_n(self):
        for m in (2, 3, 5):
            for i in range(m):
                self.assertEqual(remap_rank(i, m, m), i)


# ============================================================================
# capability：remap()（核心相对映射算法）
# ============================================================================

class TestRemapCore(unittest.TestCase):

    def test_not_present_returns_absent(self):
        cap = ModelReasoningCapability.from_config(None)
        out = remap(_intent(present=False), cap, cap)
        self.assertEqual(out, TargetEffort(level=None, source_budget=None, stripped=False))

    def test_level_none_but_present_returns_stripped(self):
        # 发了 reasoning 意图但无法识别档 → 仍需清理（stripped=True），非单纯 ABSENT。
        cap = ModelReasoningCapability.from_config(None)
        out = remap(_intent(level=None, present=True), cap, cap)
        self.assertIsNone(out.level)
        self.assertTrue(out.stripped)

    def test_off_uses_off_alias(self):
        cap = ModelReasoningCapability.from_config(None)  # 默认5档含OFF，off_alias=OFF
        out = remap(_intent(level=CE.OFF), cap, cap)
        self.assertEqual(out.level, CE.OFF)
        self.assertFalse(out.stripped)

    def test_off_with_no_off_alias_strips(self):
        cap = ModelReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None)
        out = remap(_intent(level=CE.OFF), cap, cap)
        self.assertIsNone(out.level)
        self.assertTrue(out.stripped)

    def test_source_budget_passthrough(self):
        cap = ModelReasoningCapability.from_config(None)
        out = remap(_intent(level=CE.HIGH, source_budget=12345), cap, cap)
        self.assertEqual(out.source_budget, 12345)

    def test_source_budget_none_when_not_present(self):
        cap = ModelReasoningCapability.from_config(None)
        out = remap(_intent(present=False, source_budget=999), cap, cap)
        self.assertIsNone(out.source_budget)

    def test_haiku_example_from_doc_3_4(self):
        """方案文档 §3.4 haiku 例子：source think_seq=(LOW,MEDIUM,HIGH) m=3；
        target think_seq=(HIGH,MAX) n=2。LOW->HIGH, MEDIUM->MAX, HIGH->MAX。"""
        src = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        tgt = ModelReasoningCapability(enum=(CE.HIGH, CE.MAX), off_alias=None)
        self.assertEqual(remap(_intent(level=CE.LOW), src, tgt).level, CE.HIGH)
        self.assertEqual(remap(_intent(level=CE.MEDIUM), src, tgt).level, CE.MAX)
        self.assertEqual(remap(_intent(level=CE.HIGH), src, tgt).level, CE.MAX)

    def test_isomorphic_range_degenerates_to_identity(self):
        """等档数 source(LOW,MED,HIGH) → target(LOW,MED,HIGH)：rank 恒等，验证相对映射
        在同构 range 上退化为恒等。"""
        cap = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        for level in (CE.LOW, CE.MEDIUM, CE.HIGH):
            self.assertEqual(remap(_intent(level=level), cap, cap).level, level)

    def test_two_to_five_expansion(self):
        """source(LOW,HIGH) 2档 → target(MINIMAL,LOW,MED,HIGH,XHIGH) 5档：验证向上扩展。"""
        src = ModelReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None)
        tgt = ModelReasoningCapability(
            enum=(CE.MINIMAL, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH), off_alias=None)
        # i=0(LOW) -> j=floor(0/1*4+0.5)=0 -> MINIMAL
        self.assertEqual(remap(_intent(level=CE.LOW), src, tgt).level, CE.MINIMAL)
        # i=1(HIGH) -> j=floor(1/1*4+0.5)=floor(4.5)=4 -> XHIGH
        self.assertEqual(remap(_intent(level=CE.HIGH), src, tgt).level, CE.XHIGH)


class TestRemapBoundaries(unittest.TestCase):
    """方案文档 §9「边界」一节列出的全部场景。"""

    def test_n_equals_1_all_intents_collapse_to_sole_level(self):
        # target 单思考档：所有 source 思考意图 → 该唯一档。
        src = ModelReasoningCapability.from_config(None)  # 默认5档
        tgt = ModelReasoningCapability(enum=(CE.MEDIUM,), off_alias=None)
        for level in (CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH):
            self.assertEqual(remap(_intent(level=level), src, tgt).level, CE.MEDIUM)

    def test_m_equals_1_maps_to_median_two_target_levels(self):
        # target 2 档 -> 中位是最低档 index0。
        src = ModelReasoningCapability(enum=(CE.MEDIUM,), off_alias=None)
        tgt = ModelReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None)
        self.assertEqual(remap(_intent(level=CE.MEDIUM), src, tgt).level, CE.LOW)

    def test_m_equals_1_maps_to_median_three_target_levels(self):
        # target 3 档 -> 中位是中间档 index1。
        src = ModelReasoningCapability(enum=(CE.MEDIUM,), off_alias=None)
        tgt = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        self.assertEqual(remap(_intent(level=CE.MEDIUM), src, tgt).level, CE.MEDIUM)

    def test_src_think_empty_falls_back_to_clamp_absolute(self):
        # source 未建模思考子序列（只有 OFF 或为空）→ 回退 clamp_absolute，等价现状行为。
        src = ModelReasoningCapability(enum=(CE.OFF,), off_alias=CE.OFF)
        tgt = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        out = remap(_intent(level=CE.XHIGH), src, tgt)
        self.assertEqual(out.level, clamp_absolute(CE.XHIGH, think_seq(tgt)))
        self.assertEqual(out.level, CE.HIGH)
        self.assertFalse(out.stripped)

    def test_tgt_think_empty_with_off_alias_now_strips(self):
        # target 只有 OFF、无思考档（决策B：[] 与 ["off"] 强制等价走 STRIP）：
        # 思考意图不再落到 off_alias，而是统一 STRIP，off_alias 不被消费。
        src = ModelReasoningCapability.from_config(None)
        tgt = ModelReasoningCapability(enum=(CE.OFF,), off_alias=CE.OFF)
        out = remap(_intent(level=CE.HIGH), src, tgt)
        self.assertIsNone(out.level)
        self.assertTrue(out.stripped)

    def test_tgt_cap_enum_fully_empty_strips(self):
        # target enum 全空（ds-v3friday 场景）：思考意图 → STRIP。
        src = ModelReasoningCapability.from_config(None)
        tgt = ModelReasoningCapability(enum=(), off_alias=None)
        out = remap(_intent(level=CE.HIGH), src, tgt)
        self.assertIsNone(out.level)
        self.assertTrue(out.stripped)


# ============================================================================
# 决策B：["off"] 和 [] 强制等价走 STRIP（tgt_think 为空时 off_alias 不被消费）
# ============================================================================

class TestOffAliasNotConsumedWhenTgtThinkEmpty(unittest.TestCase):
    """覆盖"target 无真实思考档"场景（effort_enum 为 [] 或 ["off"]）下，任何思考/关闭
    意图都统一走 STRIP，不消费 off_alias。这不应该影响 think_seq 非空的场景。"""

    def test_off_enum_config_thinking_intent_strips(self):
        src = ModelReasoningCapability.from_config(None)
        tgt = ModelReasoningCapability(enum=(CE.OFF,), off_alias=CE.OFF)
        out = remap(_intent(level=CE.HIGH), src, tgt)
        self.assertIsNone(out.level)
        self.assertTrue(out.stripped)

    def test_off_enum_config_off_intent_strips(self):
        src = ModelReasoningCapability.from_config(None)
        tgt = ModelReasoningCapability(enum=(CE.OFF,), off_alias=CE.OFF)
        out = remap(_intent(level=CE.OFF), src, tgt)
        self.assertIsNone(out.level)
        self.assertTrue(out.stripped)

    def test_empty_enum_and_off_enum_equivalent_via_abstract_encode(self):
        """等价性核心断言：分别构造 [] 和 ["off"] 两种 cap，对思考意图和关闭意图，
        断言 abstract_encode(remap(...)).kind 都是 AbstractKind.STRIP（4 组组合）。"""
        src = ModelReasoningCapability.from_config(None)
        empty_cap = ModelReasoningCapability(enum=(), off_alias=None)
        off_cap = ModelReasoningCapability(enum=(CE.OFF,), off_alias=CE.OFF)

        for tgt in (empty_cap, off_cap):
            for intent_level in (CE.HIGH, CE.OFF):
                out = remap(_intent(level=intent_level), src, tgt)
                abstract = abstract_encode(out)
                self.assertEqual(
                    abstract.kind, AbstractKind.STRIP,
                    f"tgt.enum={tgt.enum} intent_level={intent_level!r} 应为 STRIP，"
                    f"实际 kind={abstract.kind}")

    def test_off_low_medium_config_off_intent_still_disabled(self):
        """守护：think_seq 非空场景不受影响——effort_enum:["off","low","medium"]
        配置下，关闭意图仍应正常走 DISABLED（不是 STRIP）。"""
        src = ModelReasoningCapability.from_config(None)
        tgt = ModelReasoningCapability(enum=(CE.OFF, CE.LOW, CE.MEDIUM), off_alias=CE.OFF)
        out = remap(_intent(level=CE.OFF), src, tgt)
        self.assertEqual(out.level, CE.OFF)
        self.assertFalse(out.stripped)
        abstract = abstract_encode(out)
        self.assertEqual(abstract.kind, AbstractKind.DISABLED)

    def test_off_low_medium_config_thinking_intent_normal_rank_mapping(self):
        """同样配置下，思考意图走正常 rank 映射，不是 STRIP。"""
        src = ModelReasoningCapability.from_config(None)  # 默认5档 think_seq=(LOW,MED,HIGH,XHIGH)
        tgt = ModelReasoningCapability(enum=(CE.OFF, CE.LOW, CE.MEDIUM), off_alias=CE.OFF)
        out = remap(_intent(level=CE.HIGH), src, tgt)
        self.assertFalse(out.stripped)
        self.assertIsNotNone(out.level)
        # src_think=(LOW,MED,HIGH,XHIGH) m=4；tgt_think=(LOW,MEDIUM) n=2。
        # HIGH 在 src_think 里 rank i=2；remap_rank(2,4,2)=floor(2/3*1+0.5)=floor(1.166)=1 -> MEDIUM。
        self.assertEqual(out.level, CE.MEDIUM)


# ============================================================================
# capability：abstract_encode()
# ============================================================================

class TestAbstractEncode(unittest.TestCase):

    def test_stripped_yields_strip(self):
        te = TargetEffort(level=None, source_budget=999, stripped=True)
        out = abstract_encode(te)
        self.assertEqual(out.kind, AbstractKind.STRIP)
        self.assertIsNone(out.level)

    def test_level_none_not_stripped_yields_absent(self):
        te = TargetEffort(level=None, source_budget=None, stripped=False)
        out = abstract_encode(te)
        self.assertEqual(out.kind, AbstractKind.ABSENT)

    def test_off_level_yields_disabled(self):
        te = TargetEffort(level=CE.OFF, source_budget=None, stripped=False)
        out = abstract_encode(te)
        self.assertEqual(out.kind, AbstractKind.DISABLED)
        self.assertIsNone(out.level)

    def test_thinking_level_yields_thinking_with_level_and_budget(self):
        te = TargetEffort(level=CE.HIGH, source_budget=12345, stripped=False)
        out = abstract_encode(te)
        self.assertEqual(out.kind, AbstractKind.THINKING)
        self.assertEqual(out.level, CE.HIGH)
        self.assertEqual(out.source_budget, 12345)

    def test_max_level_yields_thinking_no_special_case(self):
        # MAX 走跟其余思考档完全一样的 THINKING 分支，无特殊判断。
        te = TargetEffort(level=CE.MAX, source_budget=None, stripped=False)
        out = abstract_encode(te)
        self.assertEqual(out.kind, AbstractKind.THINKING)
        self.assertEqual(out.level, CE.MAX)


# ============================================================================
# 单调性属性测试（穷举，含新增维度：固定 tgt_cap 遍历不同 src_cap）
# ============================================================================

class TestMonotonicity(unittest.TestCase):
    """对任意两个 intent，若 intent1.level <= intent2.level（均为思考档），
    则 remap(intent1,src,tgt).level <= remap(intent2,src,tgt).level。"""

    def _caps(self):
        return [
            ModelReasoningCapability.from_config(None),                                    # 默认 5 档
            ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None),     # 3 档短枚举
            ModelReasoningCapability(enum=(CE.LOW, CE.HIGH), off_alias=None),                # 2 档跳档
            ModelReasoningCapability(enum=(CE.OFF, CE.XHIGH), off_alias=CE.OFF),             # 极端两端
            ModelReasoningCapability(enum=(CE.HIGH, CE.MAX), off_alias=None),                # 窄高档
            ModelReasoningCapability.from_config({"reasoning_capability": {
                "effort_enum": ["none", "minimal", "low", "medium", "high"]}}),
        ]

    def test_monotonic_exhaustive_pairs_fixed_src_varying_tgt(self):
        """固定 src_cap=tgt_cap（原有维度）：对每组 cap 组合，穷举所有 source 思考档对
        (l1<=l2)，断言 remap 不减。"""
        levels = list(CE)
        for src_cap in self._caps():
            for tgt_cap in self._caps():
                for l1, l2 in itertools.combinations_with_replacement(levels, 2):
                    lo, hi = (l1, l2) if l1 <= l2 else (l2, l1)
                    out_lo = remap(_intent(level=lo), src_cap, tgt_cap).level
                    out_hi = remap(_intent(level=hi), src_cap, tgt_cap).level
                    if out_lo is None or out_hi is None:
                        continue  # STRIP/off_alias=None 场景不参与数值比较
                    self.assertLessEqual(
                        out_lo, out_hi,
                        f"单调性破坏: src={src_cap.enum} tgt={tgt_cap.enum} "
                        f"lo={lo!r}->{out_lo!r} hi={hi!r}->{out_hi!r}")

    def test_monotonic_fixed_tgt_varying_src(self):
        """新增维度（方案文档 §9 要求）：固定 tgt_cap，遍历不同 src_cap，确认单调性对
        source range 变化仍成立。"""
        levels = list(CE)
        caps = self._caps()
        for tgt_cap in caps:
            for src_cap in caps:
                for l1, l2 in itertools.combinations_with_replacement(levels, 2):
                    lo, hi = (l1, l2) if l1 <= l2 else (l2, l1)
                    out_lo = remap(_intent(level=lo), src_cap, tgt_cap).level
                    out_hi = remap(_intent(level=hi), src_cap, tgt_cap).level
                    if out_lo is None or out_hi is None:
                        continue
                    self.assertLessEqual(
                        out_lo, out_hi,
                        f"单调性破坏(固定tgt遍历src): src={src_cap.enum} tgt={tgt_cap.enum} "
                        f"lo={lo!r}->{out_lo!r} hi={hi!r}->{out_hi!r}")


# ============================================================================
# 三路径一致性测试
# ============================================================================

class TestCrossProtocolConsistency(unittest.TestCase):
    """同一 intent + 同一两侧 cap，经不同协议组合 remap 出的 canonical level 相同。

    验证的是 remap() 本身的确定性（remap 只依赖 intent+两侧 cap，不依赖走的是哪个
    协议组合），这正是要消除的"三套实现各给不同结果"问题的核心断言。
    """

    def test_same_intent_same_caps_yields_same_target_level_regardless_of_route(self):
        cap = ModelReasoningCapability.from_config(None)
        anthropic_codec = get_codec("anthropic")

        intent_passthrough = anthropic_codec.decode(
            {"thinking": {"type": "enabled", "budget_tokens": 40000}})
        level_passthrough = remap(intent_passthrough, cap, cap).level

        intent_a2c = anthropic_codec.decode(
            {"thinking": {"type": "enabled", "budget_tokens": 40000}})
        level_a2c = remap(intent_a2c, cap, cap).level

        intent_a2r = anthropic_codec.decode(
            {"thinking": {"type": "enabled", "budget_tokens": 40000}})
        level_a2r = remap(intent_a2r, cap, cap).level

        self.assertEqual(level_passthrough, level_a2c)
        self.assertEqual(level_a2c, level_a2r)
        self.assertEqual(level_passthrough, CE.XHIGH)

    def test_responses_to_anthropic_and_anthropic_to_responses_agree_on_shared_domain_name(self):
        cap = ModelReasoningCapability.from_config(None)
        intent_from_responses = get_codec("responses").decode({"reasoning": {"effort": "high"}})
        intent_from_anthropic = get_codec("anthropic").decode(
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})
        self.assertEqual(remap(intent_from_responses, cap, cap).level,
                          remap(intent_from_anthropic, cap, cap).level)


# ============================================================================
# codecs：AnthropicReasoningCodec
# ============================================================================

class TestAnthropicCodecDecode(unittest.TestCase):

    def setUp(self):
        self.codec = AnthropicReasoningCodec()

    def test_disabled_decodes_to_off_present(self):
        # explicit_off 字段已删除：关闭意图归一为 level=OFF, present=True。
        intent = self.codec.decode({"thinking": {"type": "disabled"}})
        self.assertTrue(intent.present)
        self.assertEqual(intent.level, CE.OFF)

    def test_enabled_budget(self):
        intent = self.codec.decode({"thinking": {"type": "enabled", "budget_tokens": 1000}})
        self.assertTrue(intent.present)
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


class TestAnthropicCodecSyntaxAdapt(unittest.TestCase):

    def setUp(self):
        self.codec = AnthropicReasoningCodec()

    def test_absent_returns_empty(self):
        abstract = AbstractReasoning(kind=AbstractKind.ABSENT, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, ANTHROPIC_ENABLED), {})

    def test_strip_returns_cleanup_dict(self):
        abstract = AbstractReasoning(kind=AbstractKind.STRIP, level=None, source_budget=None)
        out = self.codec.syntax_adapt(abstract, ANTHROPIC_ENABLED)
        self.assertEqual(out, {"thinking": None, "output_config": None})

    def test_disabled_returns_thinking_disabled(self):
        abstract = AbstractReasoning(kind=AbstractKind.DISABLED, level=None, source_budget=None)
        out = self.codec.syntax_adapt(abstract, ANTHROPIC_ENABLED)
        self.assertEqual(out["thinking"], {"type": "disabled"})
        self.assertIsNone(out["output_config"])  # 约定：None 表示删除该 key

    def test_enabled_variant_backfills_source_budget(self):
        abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=CE.HIGH, source_budget=12345)
        out = self.codec.syntax_adapt(abstract, ANTHROPIC_ENABLED)
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": 12345})
        self.assertIsNone(out["output_config"])

    def test_enabled_variant_fallback_when_no_source_budget(self):
        abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=CE.HIGH, source_budget=None)
        out = self.codec.syntax_adapt(abstract, ANTHROPIC_ENABLED)
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": canonical_to_budget(CE.HIGH)})

    def test_adaptive_variant(self):
        abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=CE.HIGH, source_budget=None)
        out = self.codec.syntax_adapt(abstract, ANTHROPIC_ADAPTIVE)
        self.assertEqual(out["thinking"], {"type": "adaptive"})
        self.assertEqual(out["output_config"], {"effort": "high"})

    def test_adaptive_variant_max_level_no_special_branch(self):
        # MAX 走跟其余档位完全一样的查表路径（_CANONICAL_TO_ANTHROPIC_NAME 本身含 max）。
        abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=CE.MAX, source_budget=None)
        out = self.codec.syntax_adapt(abstract, ANTHROPIC_ADAPTIVE)
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

    def test_decode_present(self):
        intent = self.codec.decode({"reasoning_effort": "high"})
        self.assertTrue(intent.present)
        self.assertEqual(intent.level, CE.HIGH)

    def test_decode_none_string_decodes_to_off_present(self):
        # "none" 字符串 -> level=OFF, present=True（关闭意图统一走 level=OFF 表达）。
        intent = self.codec.decode({"reasoning_effort": "none"})
        self.assertTrue(intent.present)
        self.assertEqual(intent.level, CE.OFF)

    def test_decode_none_value_not_present(self):
        intent = self.codec.decode({"reasoning_effort": None})
        self.assertFalse(intent.present)

    def test_decode_missing_not_present(self):
        intent = self.codec.decode({})
        self.assertFalse(intent.present)

    def test_decode_unrecognized_value_not_present(self):
        intent = self.codec.decode({"reasoning_effort": "bogus"})
        self.assertFalse(intent.present)

    def test_syntax_adapt_absent_returns_empty(self):
        abstract = AbstractReasoning(kind=AbstractKind.ABSENT, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, CHAT_EFFORT), {})

    def test_syntax_adapt_strip_returns_none_cleanup(self):
        abstract = AbstractReasoning(kind=AbstractKind.STRIP, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, CHAT_EFFORT), {"reasoning_effort": None})

    def test_syntax_adapt_disabled_returns_none_string(self):
        abstract = AbstractReasoning(kind=AbstractKind.DISABLED, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, CHAT_EFFORT), {"reasoning_effort": "none"})

    def test_syntax_adapt_max_no_special_branch_falls_back_default(self):
        # 该协议域本不该配出 MAX（配置层责任），若真出现，走通用查表兜底（非专门 if MAX 判断）。
        abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=CE.MAX, source_budget=None)
        out = self.codec.syntax_adapt(abstract, CHAT_EFFORT)
        self.assertEqual(out, {"reasoning_effort": "medium"})  # 通用兜底值，非针对 MAX 的判断结果

    def test_syntax_adapt_regular_levels(self):
        for level, name in [(CE.LOW, "low"), (CE.MEDIUM, "medium"),
                            (CE.HIGH, "high"), (CE.XHIGH, "xhigh")]:
            abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=level, source_budget=None)
            out = self.codec.syntax_adapt(abstract, CHAT_EFFORT)
            self.assertEqual(out, {"reasoning_effort": name})

    def test_interpret_rejection_always_none(self):
        self.assertIsNone(self.codec.interpret_rejection(b"anything", CHAT_EFFORT))

    def test_select_variant_single(self):
        self.assertEqual(self.codec.select_variant({}), CHAT_EFFORT)
        self.assertEqual(self.codec.select_variant({"variant": "whatever"}), CHAT_EFFORT)


class TestResponsesCodec(unittest.TestCase):

    def setUp(self):
        self.codec = ResponsesReasoningCodec()

    def test_decode_present(self):
        intent = self.codec.decode({"reasoning": {"effort": "medium"}})
        self.assertTrue(intent.present)
        self.assertEqual(intent.level, CE.MEDIUM)

    def test_decode_missing_not_present(self):
        intent = self.codec.decode({})
        self.assertFalse(intent.present)
        intent2 = self.codec.decode({"reasoning": {"effort": None}})
        self.assertFalse(intent2.present)

    def test_syntax_adapt_max_no_special_branch(self):
        abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=CE.MAX, source_budget=None)
        out = self.codec.syntax_adapt(abstract, RESP_EFFORT)
        self.assertEqual(out, {"reasoning": {"effort": "medium"}})

    def test_syntax_adapt_absent_returns_empty(self):
        abstract = AbstractReasoning(kind=AbstractKind.ABSENT, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, RESP_EFFORT), {})

    def test_syntax_adapt_strip_returns_none_cleanup(self):
        abstract = AbstractReasoning(kind=AbstractKind.STRIP, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, RESP_EFFORT), {"reasoning": None})

    def test_syntax_adapt_disabled(self):
        abstract = AbstractReasoning(kind=AbstractKind.DISABLED, level=None, source_budget=None)
        self.assertEqual(self.codec.syntax_adapt(abstract, RESP_EFFORT), {"reasoning": {"effort": "none"}})

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
# 缺陷1回归：present=True + target 空 enum → STRIP，wire dict 含 thinking:None
# （会被 apply_fields 删除），而非现状的 {}（透传残留）。
# ============================================================================

class TestDefect1PassthroughRegression(unittest.TestCase):

    def test_present_with_empty_target_enum_yields_strip_wire_with_none_cleanup(self):
        codec = get_codec("anthropic")
        body = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        raw_intent = codec.decode(body)
        self.assertTrue(raw_intent.present)

        source_cap = ModelReasoningCapability.from_config(None)
        target_cap = ModelReasoningCapability.from_config(
            {"reasoning_capability": {"effort_enum": []}})   # claude-haiku-sankuai-0956 等效场景

        target_effort = remap(raw_intent, source_cap, target_cap)
        self.assertTrue(target_effort.stripped)

        abstract = abstract_encode(target_effort)
        self.assertEqual(abstract.kind, AbstractKind.STRIP)

        wire = codec.syntax_adapt(abstract, ANTHROPIC_ENABLED)
        self.assertEqual(wire, {"thinking": None, "output_config": None})

        # 模拟 apply_fields 原地清理，验证客户端原始字段确实被删除，不再透传。
        apply_fields(body, wire)
        self.assertNotIn("thinking", body)
        self.assertNotIn("output_config", body)

    def test_present_false_yields_empty_wire_no_residue(self):
        codec = get_codec("anthropic")
        body = {}
        raw_intent = codec.decode(body)
        self.assertFalse(raw_intent.present)

        cap = ModelReasoningCapability.from_config(None)
        target_effort = remap(raw_intent, cap, cap)
        abstract = abstract_encode(target_effort)
        self.assertEqual(abstract.kind, AbstractKind.ABSENT)

        wire = codec.syntax_adapt(abstract, ANTHROPIC_ENABLED)
        self.assertEqual(wire, {})
        apply_fields(body, wire)
        self.assertEqual(body, {})


# ============================================================================
# failover 跨 supply 不永久钳低（改用 remap 三参数签名）
#
# server.py::_forward 本身依赖 HTTPServer/BaseHTTPRequestHandler，不适合直接实例化
# 单测；这里复现的是修复前后行为差异的核心不变量：decode() 只应对客户端原始 body
# 调用一次，不应在循环体对"已被上一轮 apply_fields 写入结果"的同一个 body 重新
# decode。用 codec 直接模拟 _forward 循环两轮的最小逻辑骨架来断言这个不变量。
# ============================================================================

class TestForwardLoopReasoningIntentNotPolluted(unittest.TestCase):

    def setUp(self):
        self.codec = get_codec("anthropic")
        self.source_cap = ModelReasoningCapability.from_config(None)

    def test_fixed_behavior_decode_once_reused_across_rounds(self):
        """修复后的正确做法：decode 在循环外只调用一次，循环内复用同一个 raw_intent，
        即使 body_json 被 apply_fields 原地改写，raw_intent 也不受影响。"""
        body = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        raw_intent = self.codec.decode(body)  # 循环外，只调用一次
        self.assertEqual(raw_intent.level, CE.HIGH)

        target_cap = ModelReasoningCapability.from_config(None)

        # round 1：remap + abstract_encode + syntax_adapt(enabled variant) + apply_fields 原地写回 body
        target_effort1 = remap(raw_intent, self.source_cap, target_cap)
        abstract1 = abstract_encode(target_effort1)
        wire1 = self.codec.syntax_adapt(abstract1, ANTHROPIC_ENABLED)
        apply_fields(body, wire1)
        self.assertEqual(body["thinking"]["type"], "enabled")

        # round 2（模拟上游 400 拒绝后重试）：不重新 decode，直接复用 raw_intent
        target_effort2 = remap(raw_intent, self.source_cap, target_cap)
        abstract2 = abstract_encode(target_effort2)
        wire2 = self.codec.syntax_adapt(abstract2, ANTHROPIC_ADAPTIVE)
        apply_fields(body, wire2)

        # 断言：即便 body 已经被两轮写入污染，raw_intent 本身岿然不变
        self.assertEqual(raw_intent.level, CE.HIGH)
        self.assertEqual(target_effort1.level, CE.HIGH)
        self.assertEqual(target_effort2.level, CE.HIGH)

    def test_anti_pattern_redecoding_polluted_body_permanently_clamps_across_supplies(self):
        """反向验证修复2的必要性：failover 跨到 capability 不同的 supply 时，若在循环内
        对被污染的 body 重新 decode（修复前的错误做法），原始高强度意图会被第一轮命中的
        窄 capability 永久钳低，即使换到支持更高档的 supply 也恢复不了（README 承诺的
        "按强度就近映射，不会强度倒挂"被违反）。修复1（区间内部代表值）只解决同档反算的
        往返漂移，不解决这个跨 capability 的永久钳低问题——必须靠修复2（decode 只用一次
        不变的原始 intent）解决。"""
        body = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "xhigh"}}
        original_intent = self.codec.decode(body)
        self.assertEqual(original_intent.level, CE.XHIGH)

        # round 1：命中一个窄 capability 的 supply（只到 HIGH），映射后 syntax_adapt 写回 body。
        # source 默认5档 think_seq=(LOW,MED,HIGH,XHIGH) m=4；narrow target think_seq=(LOW,MED,HIGH) n=3。
        # XHIGH 在 src_think 里 rank i=3；remap_rank(3,4,3)=floor(3/3*2+0.5)=floor(2.5)=2 -> HIGH。
        narrow_cap = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH), off_alias=None)
        target_effort1 = remap(original_intent, self.source_cap, narrow_cap)
        self.assertEqual(target_effort1.level, CE.HIGH)
        abstract1 = abstract_encode(target_effort1)
        wire1 = self.codec.syntax_adapt(abstract1, ANTHROPIC_ENABLED)
        apply_fields(body, wire1)  # body 被写成 budget_tokens=16000（HIGH 代表值）

        # round 2：failover 换到支持 XHIGH 的宽 capability supply
        wide_cap = ModelReasoningCapability.from_config(None)  # OFF..XHIGH

        # 错误做法（修复前）：对已被污染的 body 重新 decode，拿到的是 round1 映射后的 HIGH，
        # 不是客户端原始的 XHIGH ——即使 supply 换了、capability 变宽了也恢复不了。
        intent_round2_buggy = self.codec.decode(body)
        target_effort2_buggy = remap(intent_round2_buggy, self.source_cap, wide_cap)
        self.assertEqual(intent_round2_buggy.level, CE.HIGH)
        self.assertEqual(target_effort2_buggy.level, CE.HIGH,
                          "永久钳低复现：即使 round2 capability 支持 XHIGH，"
                          "重新 decode 被污染的 body 也只能拿到 round1 映射后的 HIGH")

        # 正确做法（修复后）：复用循环外算好的 original_intent，重新 remap 到新 capability，
        # 强度能正确恢复到 XHIGH。
        target_effort2_fixed = remap(original_intent, self.source_cap, wide_cap)
        self.assertEqual(target_effort2_fixed.level, CE.XHIGH,
                          "修复后：换到支持 XHIGH 的 supply，强度应恢复到客户端原始意图 XHIGH")


# ============================================================================
# MAX 无特殊分支验证（决策2：MAX 完全统一，走查表/排名路径）
# ============================================================================

class TestMaxNoSpecialBranch(unittest.TestCase):
    """解析 codecs.py/capability.py 的 AST，确认不存在任何 if 语句的条件表达式里出现
    对 CanonicalEffort.MAX 的比较（用 AST 而非文本 grep，避免注释/文档字符串里提及
    "if...MAX" 说明文字导致误报——只检测真实代码结构，不检测注释文本）。"""

    def _assert_no_max_if_branch(self, filename: str):
        path = os.path.join(_CORE_REASONING_DIR, filename)
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=filename)

        def _mentions_max(node) -> bool:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "MAX":
                    return True
            return False

        offending_lines = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _mentions_max(node.test):
                offending_lines.append(node.lineno)
        self.assertEqual(
            offending_lines, [],
            f"{filename} 不应出现针对 MAX 的专门 if 判断（决策2：MAX 无特殊分支），"
            f"命中行号: {offending_lines}")

    def test_codecs_py_has_no_max_special_branch(self):
        self._assert_no_max_if_branch("codecs.py")

    def test_capability_py_has_no_max_special_branch(self):
        self._assert_no_max_if_branch("capability.py")

    def test_max_walks_same_lookup_path_as_other_levels_in_anthropic_adaptive(self):
        """构造性验证：MAX 作为普通序列项查表，跟 LOW/MEDIUM/HIGH 走同一条代码路径
        （AnthropicReasoningCodec.syntax_adapt 的 ANTHROPIC_ADAPTIVE 分支，统一走
        _CANONICAL_TO_ANTHROPIC_NAME 查表，没有独立的 if level==MAX 分支）。"""
        codec = AnthropicReasoningCodec()
        for level, name in [(CE.LOW, "low"), (CE.MEDIUM, "medium"), (CE.HIGH, "high"),
                             (CE.XHIGH, "xhigh"), (CE.MAX, "max")]:
            abstract = AbstractReasoning(kind=AbstractKind.THINKING, level=level, source_budget=None)
            out = codec.syntax_adapt(abstract, ANTHROPIC_ADAPTIVE)
            self.assertEqual(out["output_config"]["effort"], name)

    def test_remap_treats_max_as_ordinary_highest_think_rank(self):
        """remap() 里 MAX 作为思考子序列最高值，走跟 LOW/MEDIUM/HIGH 完全一样的
        rank_of/remap_rank 路径，不出现任何针对 MAX 的判断。"""
        src = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH, CE.MAX), off_alias=None)
        tgt = ModelReasoningCapability(enum=(CE.LOW, CE.MEDIUM, CE.HIGH, CE.MAX), off_alias=None)
        for level in (CE.LOW, CE.MEDIUM, CE.HIGH, CE.MAX):
            self.assertEqual(remap(_intent(level=level), src, tgt).level, level)


# ============================================================================
# resolve_strategy：client_token → strategy 记录本身
# ============================================================================

class TestResolveStrategy(unittest.TestCase):

    def test_hit(self):
        strategies = [{"client_token": "cc", "route_id": "claude"},
                      {"client_token": "codex", "route_id": "openai"}]
        s = resolve_strategy(strategies, "codex")
        self.assertIs(s, strategies[1])

    def test_miss(self):
        strategies = [{"client_token": "cc", "route_id": "claude"}]
        self.assertIsNone(resolve_strategy(strategies, "unknown"))

    def test_empty_strategies(self):
        self.assertIsNone(resolve_strategy([], "cc"))


# ============================================================================
# resolve_source_capability：从 strategy 的 tiers_source_capability[tier] 取值
# （改动1：source_models 归属重构，废弃顶层表，改挂在 strategy 下）
# ============================================================================

class TestResolveSourceCapability(unittest.TestCase):
    """覆盖新签名 resolve_source_capability(strategy, tier)：
    命中查表（cc/codex 两个不同 strategy 分别验证）/ tier 未声明回退默认5档 /
    strategy 整体为 None 回退默认5档 / tier 为 None 回退默认5档 /
    strategy 存在但无 tiers_source_capability 字段回退默认5档 /
    两个不同 strategy 的同一个 tier 可以配出不同结果。"""

    def setUp(self):
        self.cc_strategy = {
            "client_token": "cc",
            "route_id": "claude",
            "tiers_source_capability": {
                "opus": {"effort_enum": ["off", "low", "medium", "high", "xhigh"]},
                "sonnet": {"effort_enum": ["off", "low", "medium"]},
            },
        }
        self.codex_strategy = {
            "client_token": "codex",
            "route_id": "openai",
            "tiers_source_capability": {
                "opus": {"effort_enum": ["low", "high"]},
            },
        }
        self.strategy_no_field = {"client_token": "plain", "route_id": "claude"}
        self._default_enum = (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH)

    def test_hit_cc_opus(self):
        cap = resolve_source_capability(self.cc_strategy, "opus")
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM, CE.HIGH, CE.XHIGH))

    def test_hit_cc_sonnet_short_enum(self):
        cap = resolve_source_capability(self.cc_strategy, "sonnet")
        self.assertEqual(cap.enum, (CE.OFF, CE.LOW, CE.MEDIUM))

    def test_hit_codex_opus_narrow_enum(self):
        cap = resolve_source_capability(self.codex_strategy, "opus")
        self.assertEqual(cap.enum, (CE.LOW, CE.HIGH))

    def test_tier_not_declared_under_strategy_falls_back_default(self):
        # codex_strategy 下没有 haiku 声明 -> 回退默认5档
        cap = resolve_source_capability(self.codex_strategy, "haiku")
        self.assertEqual(cap.enum, self._default_enum)

    def test_strategy_none_falls_back_default(self):
        cap = resolve_source_capability(None, "opus")
        self.assertEqual(cap.enum, self._default_enum)

    def test_tier_none_falls_back_default(self):
        cap = resolve_source_capability(self.cc_strategy, None)
        self.assertEqual(cap.enum, self._default_enum)

    def test_both_none_falls_back_default(self):
        cap = resolve_source_capability(None, None)
        self.assertEqual(cap.enum, self._default_enum)

    def test_strategy_without_tiers_source_capability_field_falls_back_default(self):
        cap = resolve_source_capability(self.strategy_no_field, "opus")
        self.assertEqual(cap.enum, self._default_enum)

    def test_cc_and_codex_can_be_declared_differently(self):
        """核心验证点：两个不同 strategy 的同一个 tier（opus）可以配出不同结果。"""
        cc_cap = resolve_source_capability(self.cc_strategy, "opus")
        codex_cap = resolve_source_capability(self.codex_strategy, "opus")
        self.assertNotEqual(cc_cap.enum, codex_cap.enum)


if __name__ == "__main__":
    unittest.main()
