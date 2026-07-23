"""core.reasoning.capability — source/target 能力描述 + remap()（相对排名映射）+ abstract_encode()。

依赖 ladder，不依赖 codecs/registry/server/translate。

新数据流（decode → resolve_source_capability/resolve_target_capability → remap →
abstract_encode → syntax_adapt）的核心算法层：
- ModelReasoningCapability：source/target 共用同一能力描述类型（原 ReasoningCapability）。
- think_seq/rank_of/remap_rank：相对排名映射的三个纯函数构件。
- clamp_absolute：原 _clamp_to_enum 改名，仅作 source 未建模思考子序列时的兜底钳位用。
- remap()：唯一的强度换算点，签名 (intent, src_cap, tgt_cap) -> TargetEffort。
- abstract_encode()：TargetEffort -> AbstractReasoning，OFF→DISABLED 的唯一判断点
  （与 remap() 内部的 OFF 吸收态 clause 一起，是全代码库仅有的两处 level==OFF 判断）。

MAX 无任何特殊分支：作为思考子序列的最高值，走跟 LOW/MEDIUM/HIGH 完全一样的
查表/排名路径，全模块不出现任何 `if ... == CanonicalEffort.MAX` 判断。
"""

import math
from dataclasses import dataclass
from enum import Enum

from .ladder import CanonicalEffort, RawIntent, name_to_canonical

# 默认 5 档（等价旧版硬编码行为）：OFF/LOW/MEDIUM/HIGH/XHIGH，未配置 effort_enum 时的兜底。
_DEFAULT_ENUM = (
    CanonicalEffort.OFF,
    CanonicalEffort.LOW,
    CanonicalEffort.MEDIUM,
    CanonicalEffort.HIGH,
    CanonicalEffort.XHIGH,
)


def think_seq(cap: "ModelReasoningCapability") -> tuple:
    """cap.enum 排除 OFF 后的思考子序列（升序，OFF 不参与排名计算）。"""
    return tuple(e for e in cap.enum if e != CanonicalEffort.OFF)


@dataclass(frozen=True)
class ModelReasoningCapability:
    """一个表面模型（source）或一个 supply（target）真实支持的 reasoning 档位能力描述。

    原名 ReasoningCapability；source/target 共用同一类型（决策1）。
    """
    enum: tuple             # 有序去重 CanonicalEffort 元组（低→高），含/不含 OFF
    off_alias: "CanonicalEffort | None"  # "关闭"落点；缺省 = enum 含 OFF 则 OFF 否则 None

    @classmethod
    def from_config(cls, supply: "dict | None") -> "ModelReasoningCapability":
        """解析 supply（或 source_models 单条 entry 包一层 reasoning_capability 后）的
        reasoning_capability 字段，缺省用默认 5 档。

        新 schema：
            {"effort_enum": ["none","low","medium","high"], "off_alias": "none"}
        effort_enum 里的字符串档名（协议无关规范名，见 ladder.name_to_canonical）转换成
        CanonicalEffort 元组，按值升序排列、去重。未识别的档名忽略（不报错，容错优先）。
        MAX 档不设专属别名，统一走 remap() 里的相对排名路径，或 clamp_absolute 兜底钳到
        enum 最高档，不在本方法/任何转换函数里对 MAX 做专门判断。

        新增校验（决策3拍板，单调性约束见 remap 模块头注释 §3.5）：off_alias 不得高于
        enum 中最低思考档（think_seq 首元素），否则视为无效配置，回退 None——否则"关闭
        思考"会被映射得比"低强度思考"更强，破坏 remap() 的单调性承诺。
        """
        supply = supply or {}
        rc = supply.get("reasoning_capability") or {}

        if "effort_enum" in rc:
            raw_enum = rc.get("effort_enum")
            parsed = []
            for name in (raw_enum or []):
                level = name_to_canonical(name)
                if level is not None and level not in parsed:
                    parsed.append(level)
            enum = tuple(sorted(parsed))  # 空列表/全非法名 → () 空元组，不回退默认档
        else:
            enum = _DEFAULT_ENUM

        if "off_alias" in rc:
            off_alias = name_to_canonical(rc.get("off_alias"))
            if off_alias is not None and off_alias not in enum:
                off_alias = None
        else:
            off_alias = CanonicalEffort.OFF if CanonicalEffort.OFF in enum else None

        # off_alias 单调性守卫：不得高于 enum 中最低思考档。
        think = tuple(e for e in enum if e != CanonicalEffort.OFF)
        if off_alias is not None and think and off_alias > think[0]:
            off_alias = None

        return cls(enum=enum, off_alias=off_alias)


@dataclass(frozen=True)
class TargetEffort:
    """remap() 的产出：已映射到某 target capability 上的强度，供 abstract_encode 使用。

    原名 AlignedEffort，新增 stripped 位（决策消除缺陷1：present 但 target 完全不支持
    思考时的显式清理信号）。
    """
    level: "CanonicalEffort | None"   # None 表示不塞 reasoning 字段（ABSENT）或需清理（STRIP，见 stripped）
    source_budget: "int | None"        # 透传自 RawIntent，供无损回填
    stripped: bool                    # True = present 但 target 完全不支持思考，需主动清理原始字段


class AbstractKind(Enum):
    """abstract_encode() 产出的协议无关抽象种类。"""
    THINKING = "thinking"
    DISABLED = "disabled"             # OFF 的唯一落点，统一收在 abstract_encode 这一处
    STRIP = "strip"
    ABSENT = "absent"


@dataclass(frozen=True)
class AbstractReasoning:
    """abstract_encode() 的产出：协议无关中间物，供各 codec.syntax_adapt 消费。"""
    kind: AbstractKind
    level: "CanonicalEffort | None"   # 仅 kind=THINKING 时有意义
    source_budget: "int | None"


def clamp_absolute(level: CanonicalEffort, enum: tuple) -> "CanonicalEffort | None":
    """把 level 按 canonical 序数就近钳到 enum 里的一档（原 _clamp_to_enum 改名）。

    区别于新的相对 remap；仅作 source 未建模思考子序列时的兜底钳位用，以及
    rank_of() 内部"intent.level 不在 seq 中时先钳到最近档再取 rank"这一步。

    - enum 为空 → None（该 supply 不支持任何 effort 档位）
    - 超过 enum 最高档 → enum 最高档
    - 低于 enum 最低档 → enum 最低档
    - 精确命中 → 原样返回
    - 落在范围内但未精确命中 → 取序数最近邻；并列取更高档（偏保守保留思考质量）
    """
    if not enum:
        return None
    lo, hi = enum[0], enum[-1]
    if level <= lo:
        return lo
    if level >= hi:
        return hi
    if level in enum:
        return level
    best = None
    best_dist = None
    for cand in enum:
        dist = abs(int(cand) - int(level))
        if best is None or dist < best_dist or (dist == best_dist and cand > best):
            best = cand
            best_dist = dist
    return best


def rank_of(level: CanonicalEffort, seq: tuple) -> int:
    """level 在思考子序列 seq 里的 rank（下标）。

    若 level 精确命中 seq 中某档，直接返回其下标；否则先用 clamp_absolute 绝对钳到
    seq 里最近的一档，再取该档下标。要求 seq 非空（调用方需先判空）。
    """
    if level in seq:
        return seq.index(level)
    clamped = clamp_absolute(level, seq)
    return seq.index(clamped)


def remap_rank(i: int, m: int, n: int) -> int:
    """source 思考子序列 rank i（0..m-1）→ target 思考子序列 rank（0..n-1）。

    - n == 1：target 只有 1 档，全部塌缩到该档（rank 0）。
    - m == 1：source 单思考档 → target 中位档 (n-1)//2（用户拍板：选"中位"，不是最强）。
    - 其余：按比例线性映射 i/(m-1) -> j/(n-1)，四舍五入（.5 向上，floor(x+0.5)）。
    """
    if n == 1:
        return 0
    if m == 1:
        return (n - 1) // 2
    return math.floor(i / (m - 1) * (n - 1) + 0.5)


def remap(intent: RawIntent, src_cap: ModelReasoningCapability,
          tgt_cap: ModelReasoningCapability) -> TargetEffort:
    """唯一的强度换算点：跨模型相对排名映射（原 align() 的单侧绝对钳位，改为双侧相对映射）。

    单调性证明见方案文档 §3.5：固定 src_cap/tgt_cap 时，intent.level 越大，
    remap(intent).level 不减。

    OFF 吸收态 clause（决策2：允许特殊分支，但统一收在这一处，另一处见 abstract_encode）：
    intent.level == OFF 时，若 target 有真实思考档（tgt_think 非空）则落到 tgt_cap.off_alias；
    若 target 无真实思考档（tgt_think 为空，即 effort_enum 是 `[]` 或 `["off"]`，或任何"去掉
    OFF 后没有真实思考档"的配置），off_alias 不被消费，统一走 STRIP——`[]` 和 `["off"]` 两种
    配置在这一点上强制等价。

    MAX 完全统一：作为思考子序列最高值，走跟其余思考档完全一样的 rank_of/remap_rank
    路径，本函数不出现任何针对 MAX 的专门判断。
    """
    if not intent.present:
        return TargetEffort(level=None, source_budget=None, stripped=False)          # ABSENT
    if not tgt_cap.enum:
        return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)   # STRIP
    if intent.level is None:
        return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)   # 发了但无法识别档 → 仍需清理

    tgt_think = think_seq(tgt_cap)   # 提前计算：OFF 吸收态 clause 也需要判断 tgt_think 是否为空

    # —— OFF 吸收态 clause（决策2：允许特殊分支，但统一收在这一处）——
    if intent.level == CanonicalEffort.OFF:
        if not tgt_think:            # target 无真实思考档 → 关闭意图统一 STRIP，不看 off_alias
            return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)
        return TargetEffort(level=tgt_cap.off_alias, source_budget=intent.source_budget,
                             stripped=(tgt_cap.off_alias is None))

    # —— 思考档相对映射 ——
    src_think = think_seq(src_cap)
    if not tgt_think:                              # target 只有 OFF、无思考档 → STRIP（不消费 off_alias）
        return TargetEffort(level=None, source_budget=intent.source_budget, stripped=True)
    if not src_think:                               # source 未建模思考子序列 → 回退绝对钳位（兼容兜底）
        clamped = clamp_absolute(intent.level, tgt_think)
        return TargetEffort(level=clamped, source_budget=intent.source_budget, stripped=False)

    i = rank_of(intent.level, src_think)             # 若 intent.level 不在 src_think 中，先绝对钳到最近档再取 rank
    j = remap_rank(i, len(src_think), len(tgt_think))
    return TargetEffort(level=tgt_think[j], source_budget=intent.source_budget, stripped=False)


def abstract_encode(te: TargetEffort) -> AbstractReasoning:
    """TargetEffort -> AbstractReasoning（协议无关）。

    这是全局唯一 if level==OFF 出现处之一（连同 remap() 里的 OFF clause，全代码库
    总共只有这两处，且职责不同：remap 负责"OFF 该落到 target 的哪个值"，
    abstract_encode 负责"OFF 这个值该转成什么抽象类型"，不重复判断同一件事）。
    """
    if te.stripped:
        return AbstractReasoning(kind=AbstractKind.STRIP, level=None, source_budget=None)
    if te.level is None:
        return AbstractReasoning(kind=AbstractKind.ABSENT, level=None, source_budget=None)
    if te.level == CanonicalEffort.OFF:
        return AbstractReasoning(kind=AbstractKind.DISABLED, level=None, source_budget=None)
    return AbstractReasoning(kind=AbstractKind.THINKING, level=te.level, source_budget=te.source_budget)
