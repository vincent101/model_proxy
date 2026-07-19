"""core.reasoning.ladder — canonical 强度全序枚举 + budget↔canonical 锚点表。

零依赖模块：不 import 本包内其他模块，也不 import server/translate。

CanonicalEffort 是内部统一的强度全序（跨协议），各协议档名字符串在 codecs.py 里
双向映射到这个全序上。budget_to_canonical / canonical_to_budget 是 Anthropic
enabled 语法（budget_tokens 整数）与 canonical 之间的唯一换算锚点——budget 语义是
Anthropic 侧固定的，与 per-supply 配置无关，故上收为全局常量（不再是 per-supply
可配置的 budget_breakpoints，参见架构决策 #1）。
"""

from dataclasses import dataclass
from enum import IntEnum


class CanonicalEffort(IntEnum):
    """canonical 强度全序，值越大强度越高。"""
    OFF = 0
    MINIMAL = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    XHIGH = 5
    MAX = 6


@dataclass(frozen=True)
class ReasoningIntent:
    """decode() 的产出：客户端表达的 reasoning 意图，协议无关。"""
    level: "CanonicalEffort | None"   # 归一化后的规范强度；None 表示未表达（present 仍可能为 True，见 decode 各分支）
    source_budget: "int | None"        # 客户端原始 budget_tokens（仅 Anthropic enabled 语法有意义），供无损回填
    explicit_off: bool                 # thinking.type=disabled 这类显式关闭，区别于"未指定"
    present: bool                      # 客户端是否表达了 reasoning 意图（否则 encode 返回 {} 不塞字段）


# budget → canonical 的唯一锚点表（全局常量，Anthropic budget 语义固定）。
# 语义：budget < 断点值 → 对应档；超过所有断点 → MAX。
# 数值沿用现状 map_reasoning_effort 的既有分档（<2000→low,<8000→medium,<32000→high,
# >=32000→xhigh，现状最高档就是 xhigh、没有 max），本次扩展加入 64000→XHIGH 边界、
# >=64000 归入新增的 MAX 档（现状不存在 max 档，故这条边界是本次架构新增，未被历史
# 行为验证过；默认 5 档 capability 没有 MAX 元素，align() 会把 MAX 钳回 xhigh，
# 不会因为多出这一档而破坏既有行为）。
_BUDGET_ANCHORS: list = [
    (2000, CanonicalEffort.LOW),
    (8000, CanonicalEffort.MEDIUM),
    (32000, CanonicalEffort.HIGH),
    (64000, CanonicalEffort.XHIGH),
]

# canonical → budget 代表值反算表（仅在目标要 budget 语法但没有 source_budget 可回填
# 时兜底使用）。LOW/MEDIUM/HIGH/XHIGH/MAX 由 architect 给定；OFF/MINIMAL 不在原设计
# 范围内——OFF 在 AnthropicReasoningCodec.encode 里总是转成 thinking.type=disabled，
# 不会走到这张表；MINIMAL 理论上可能在 source_budget 缺失且 aligned.level=MINIMAL 时
# 触发（仅当某 anthropic supply 的 reasoning_capability 显式配置了 minimal 档，且恰好
# 该请求没有原始 budget 可回填），这条边界 architect 蓝图未给出数值，此处按 LOW 断点
# 的一半（1000）保守外推，保持"数值越大强度越高"单调，未被真实场景验证。
_CANONICAL_TO_BUDGET = {
    CanonicalEffort.OFF: 0,
    CanonicalEffort.MINIMAL: 1000,
    CanonicalEffort.LOW: 2000,
    CanonicalEffort.MEDIUM: 8000,
    CanonicalEffort.HIGH: 32000,
    CanonicalEffort.XHIGH: 64000,
    CanonicalEffort.MAX: 128000,
}

# 配置文件 / 通用协议字符串 → canonical 的规范名称表（协议无关，供 capability.from_config
# 解析 effort_enum/off_alias 字符串用）。"off"/"none" 两种拼法都接受
# （旧 schema 用 "none" 表达最低/关闭档，这里两者等价）。
_NAME_TO_CANONICAL = {
    "off": CanonicalEffort.OFF,
    "none": CanonicalEffort.OFF,
    "minimal": CanonicalEffort.MINIMAL,
    "low": CanonicalEffort.LOW,
    "medium": CanonicalEffort.MEDIUM,
    "high": CanonicalEffort.HIGH,
    "xhigh": CanonicalEffort.XHIGH,
    "max": CanonicalEffort.MAX,
}


def name_to_canonical(name: "str | None") -> "CanonicalEffort | None":
    """通用协议无关字符串 → canonical；未识别返回 None。"""
    if not isinstance(name, str):
        return None
    return _NAME_TO_CANONICAL.get(name.strip().lower())


def budget_to_canonical(budget: int) -> CanonicalEffort:
    """budget_tokens 整数 → canonical 强度档（唯一锚点，语义见模块头注释）。"""
    for threshold, tier in _BUDGET_ANCHORS:
        if budget < threshold:
            return tier
    return CanonicalEffort.MAX


def canonical_to_budget(level: CanonicalEffort) -> int:
    """canonical → budget 代表值反算（兜底用，见 _CANONICAL_TO_BUDGET 注释）。"""
    return _CANONICAL_TO_BUDGET.get(level, _CANONICAL_TO_BUDGET[CanonicalEffort.MEDIUM])
