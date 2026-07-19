"""core.reasoning.capability — per-supply 能力描述 + align()（唯一钳位点）。

依赖 ladder，不依赖 codecs/registry/server/translate。
"""

from dataclasses import dataclass

from .ladder import CanonicalEffort, ReasoningIntent, name_to_canonical

# 默认 5 档（等价旧版硬编码行为）：OFF/LOW/MEDIUM/HIGH/XHIGH，max→XHIGH，off→OFF。
_DEFAULT_ENUM = (
    CanonicalEffort.OFF,
    CanonicalEffort.LOW,
    CanonicalEffort.MEDIUM,
    CanonicalEffort.HIGH,
    CanonicalEffort.XHIGH,
)


@dataclass(frozen=True)
class ReasoningCapability:
    """一个 supply 真实支持的 reasoning 档位能力描述。"""
    enum: tuple             # 有序的 CanonicalEffort 元组（低→高），该 supply 真实支持的档位
    off_alias: "CanonicalEffort | None"  # explicit_off 的落点；缺省 = enum 含 OFF 则 OFF 否则 None

    @classmethod
    def from_config(cls, supply: "dict | None") -> "ReasoningCapability":
        """解析 supply 的 reasoning_capability 字段（新 schema），缺省用默认 5 档。

        新 schema：
            {"effort_enum": ["none","low","medium","high"], "off_alias": "none"}
        effort_enum 里的字符串档名（协议无关规范名，见 ladder.name_to_canonical）转换成
        CanonicalEffort 元组，按值升序排列、去重。未识别的档名忽略（不报错，容错优先）。
        MAX 档不设专属别名，统一走 align() 里的 _clamp_to_enum 钳到 enum 最高档。
        """
        supply = supply or {}
        rc = supply.get("reasoning_capability") or {}

        raw_enum = rc.get("effort_enum")
        if raw_enum:
            parsed = []
            for name in raw_enum:
                level = name_to_canonical(name)
                if level is not None and level not in parsed:
                    parsed.append(level)
            enum = tuple(sorted(parsed)) if parsed else _DEFAULT_ENUM
        else:
            enum = _DEFAULT_ENUM

        if "off_alias" in rc:
            off_alias = name_to_canonical(rc.get("off_alias"))
            if off_alias is not None and off_alias not in enum:
                off_alias = None
        else:
            off_alias = CanonicalEffort.OFF if CanonicalEffort.OFF in enum else None

        return cls(enum=enum, off_alias=off_alias)


@dataclass(frozen=True)
class AlignedEffort:
    """align() 的产出：已钳位到某 capability 上的强度，供 codec.encode 使用。"""
    level: "CanonicalEffort | None"   # None 表示不塞 reasoning 字段
    source_budget: "int | None"        # 透传自 ReasoningIntent，供无损回填


def _clamp_to_enum(level: CanonicalEffort, enum: tuple) -> CanonicalEffort:
    """把 level 按 canonical 序数就近钳到 enum 里的一档。

    - 超过 enum 最高档 → enum 最高档
    - 低于 enum 最低档 → enum 最低档
    - 精确命中 → 原样返回
    - 落在范围内但未精确命中 → 取序数最近邻；并列取更高档（偏保守保留思考质量）
    """
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


def align(intent: ReasoningIntent, cap: ReasoningCapability) -> AlignedEffort:
    """唯一的强度钳位点。

    数学上单调不减：intent.level 越大，输出 level 不减（由 _clamp_to_enum 的单调
    钳位性质保证：enum 固定时，clamp 函数对输入单调不减）。
    """
    if not intent.present:
        return AlignedEffort(level=None, source_budget=None)
    if intent.explicit_off:
        return AlignedEffort(level=cap.off_alias, source_budget=intent.source_budget)
    if intent.level is None:
        return AlignedEffort(level=None, source_budget=intent.source_budget)
    clamped = _clamp_to_enum(intent.level, cap.enum)
    return AlignedEffort(level=clamped, source_budget=intent.source_budget)
