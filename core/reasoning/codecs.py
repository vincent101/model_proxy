"""core.reasoning.codecs — 各协议（anthropic/chat/responses）的编解码器。

依赖 ladder + capability，不依赖 registry/server/translate。

每个 codec 声明自己支持的语法变体（variants），把"选语法"统一化：
- AnthropicReasoningCodec：双变体（enabled/adaptive），需要 400 自适应重试学习。
- ChatReasoningCodec / ResponsesReasoningCodec：单变体，无需选择。

编解码约定：encode() 返回的 dict 是"该协议 reasoning 相关字段片段"，供调用方（server.py）
merge 进目标 body。约定：value 为 None 表示"删除该 key"（用于 PASSTHROUGH 场景清理游离的
output_config.effort），其余 value 表示"设置该 key"；不出现在返回 dict 里的 key 一律不动
（既不新增也不删除）。aligned.level 为 None 时返回 {}（不涉及任何 key）。
"""

from abc import ABC, abstractmethod

from .capability import AlignedEffort, ReasoningCapability
from .ladder import CanonicalEffort, ReasoningIntent, budget_to_canonical, canonical_to_budget

# 语法变体标识
CHAT_EFFORT = "chat_effort"
RESP_EFFORT = "resp_effort"
ANTHROPIC_ADAPTIVE = "anthropic_adaptive"
ANTHROPIC_ENABLED = "anthropic_enabled"


class ReasoningCodec(ABC):
    """协议编解码器协议（ABC）。"""

    protocol: str = ""
    variants: tuple = ()
    default_variant: str = ""

    @abstractmethod
    def decode(self, body: dict) -> ReasoningIntent:
        ...

    @abstractmethod
    def encode(self, aligned: AlignedEffort, cap: ReasoningCapability, variant: str) -> dict:
        ...

    def interpret_rejection(self, error_body: bytes, used_variant: str) -> "str | None":
        """从 400 错误体判断应改用哪个变体；单变体 codec 恒返回 None。"""
        return None

    def select_variant(self, pref: dict) -> str:
        """单变体直接返回该变体；多变体查 pref（运行时学到的偏好），缺省 default_variant。"""
        if len(self.variants) <= 1:
            return self.variants[0] if self.variants else self.default_variant
        v = (pref or {}).get("variant")
        if v in self.variants:
            return v
        return self.default_variant


# ============================================================================
# Anthropic：thinking.type=enabled(+budget_tokens) / adaptive(+output_config.effort) / disabled
# ============================================================================

# Anthropic 协议域内的档名字符串集合（有 max，没有 none/off——"不思考"是 thinking.type=disabled，
# 不是 effort=none）。
_ANTHROPIC_NAME_TO_CANONICAL = {
    "minimal": CanonicalEffort.MINIMAL,
    "low": CanonicalEffort.LOW,
    "medium": CanonicalEffort.MEDIUM,
    "high": CanonicalEffort.HIGH,
    "xhigh": CanonicalEffort.XHIGH,
    "max": CanonicalEffort.MAX,
}
_CANONICAL_TO_ANTHROPIC_NAME = {v: k for k, v in _ANTHROPIC_NAME_TO_CANONICAL.items()}


class AnthropicReasoningCodec(ReasoningCodec):
    """thinking.type ∈ {enabled, adaptive, disabled} 双变体编解码器。"""

    protocol = "anthropic"
    variants = (ANTHROPIC_ENABLED, ANTHROPIC_ADAPTIVE)
    default_variant = ANTHROPIC_ENABLED

    def decode(self, body: dict) -> ReasoningIntent:
        thinking = body.get("thinking") or {}
        ttype = thinking.get("type") if isinstance(thinking, dict) else None
        oc = body.get("output_config") or {}
        effort_str = oc.get("effort") if isinstance(oc, dict) else None

        if ttype == "disabled":
            return ReasoningIntent(level=None, source_budget=None, explicit_off=True, present=True)

        if ttype == "enabled":
            budget = thinking.get("budget_tokens", 10000)
            level = budget_to_canonical(budget)
            return ReasoningIntent(level=level, source_budget=budget, explicit_off=False, present=True)

        if ttype == "adaptive":
            level = _ANTHROPIC_NAME_TO_CANONICAL.get(effort_str)
            if level is None:
                # 未指定/不是 Anthropic 域内识别的标准词 → 沿用现状默认兜底：medium
                # （现状 bare adaptive 无 effort 时的既有行为，clamp 由 align() 统一处理）
                level = CanonicalEffort.MEDIUM
            return ReasoningIntent(level=level, source_budget=None, explicit_off=False, present=True)

        # thinking 缺失/其他值 → 不产出（含裸 output_config.effort 场景，保持现状"不生效"语义）
        return ReasoningIntent(level=None, source_budget=None, explicit_off=False, present=False)

    def encode(self, aligned: AlignedEffort, cap: ReasoningCapability, variant: str) -> dict:
        if aligned.level is None:
            return {}
        if aligned.level == CanonicalEffort.OFF:
            return {"thinking": {"type": "disabled"}, "output_config": None}

        if variant == ANTHROPIC_ENABLED:
            budget = aligned.source_budget if aligned.source_budget is not None else canonical_to_budget(aligned.level)
            # 目标语法 enabled：无损回填客户端原始 budget_tokens（决策 #2），没有则兜底反算；
            # 且清除游离的 output_config.effort（现状 haiku 报错防护逻辑保留）。
            return {"thinking": {"type": "enabled", "budget_tokens": budget}, "output_config": None}

        # ANTHROPIC_ADAPTIVE
        name = _CANONICAL_TO_ANTHROPIC_NAME.get(aligned.level, "medium")
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": name}}

    def interpret_rejection(self, error_body: bytes, used_variant: str) -> "str | None":
        """从 400 响应体识别应换用的变体（原 server.py::_parse_thinking_error 规则原样搬入）。"""
        try:
            text = error_body.decode("utf-8", errors="replace")
        except Exception:
            return None
        t = text.lower()
        if "thinking.type.enabled" in t and "not supported" in t:
            return ANTHROPIC_ADAPTIVE
        if "budget_tokens" in t and "not supported" in t:
            return ANTHROPIC_ADAPTIVE
        if "thinking.type.adaptive" in t and "not supported" in t:
            return ANTHROPIC_ENABLED
        # output_config 不被支持（如 haiku-4.5 不接受 adaptive 的 output_config.effort）
        if "output_config" in t and ("not permitted" in t or "not allowed" in t):
            return ANTHROPIC_ENABLED
        # GLM 等部分上游返回泛化中文错误（调用方带了 thinking 才走到此分支）
        if "参数有误" in t or "invalid parameter" in t.lower():
            return ANTHROPIC_ENABLED
        return None


# ============================================================================
# Chat（OpenAI chat/completions）：reasoning_effort 字段，单变体
# ============================================================================

# Chat/Responses 协议域内的档名字符串集合（有 none，没有 max/minimal）。
_CHAT_NAME_TO_CANONICAL = {
    "none": CanonicalEffort.OFF,
    "low": CanonicalEffort.LOW,
    "medium": CanonicalEffort.MEDIUM,
    "high": CanonicalEffort.HIGH,
    "xhigh": CanonicalEffort.XHIGH,
}
_CANONICAL_TO_CHAT_NAME = {v: k for k, v in _CHAT_NAME_TO_CANONICAL.items()}


def _canonical_to_chat_domain_name(level: CanonicalEffort) -> str:
    """canonical → Chat/Responses 域内档名字符串。MAX 钳到 xhigh（该域无 max）；
    MINIMAL 退化到 low（该域无 minimal，防御性兜底，正常配置不会走到这支）。"""
    if level == CanonicalEffort.MAX:
        return "xhigh"
    if level == CanonicalEffort.MINIMAL:
        return "low"
    return _CANONICAL_TO_CHAT_NAME.get(level, "medium")


class ChatReasoningCodec(ReasoningCodec):
    """reasoning_effort 字段，单变体。"""

    protocol = "chat"
    variants = (CHAT_EFFORT,)
    default_variant = CHAT_EFFORT

    def decode(self, body: dict) -> ReasoningIntent:
        effort_str = body.get("reasoning_effort")
        if effort_str is None:
            return ReasoningIntent(level=None, source_budget=None, explicit_off=False, present=False)
        level = _CHAT_NAME_TO_CANONICAL.get(effort_str)
        if level is None:
            return ReasoningIntent(level=None, source_budget=None, explicit_off=False, present=False)
        return ReasoningIntent(level=level, source_budget=None, explicit_off=False, present=True)

    def encode(self, aligned: AlignedEffort, cap: ReasoningCapability, variant: str) -> dict:
        if aligned.level is None:
            return {}
        return {"reasoning_effort": _canonical_to_chat_domain_name(aligned.level)}


# ============================================================================
# Responses（OpenAI Responses API）：reasoning.effort 字段，单变体
# ============================================================================

class ResponsesReasoningCodec(ReasoningCodec):
    """reasoning.effort 字段，单变体。"""

    protocol = "responses"
    variants = (RESP_EFFORT,)
    default_variant = RESP_EFFORT

    def decode(self, body: dict) -> ReasoningIntent:
        r = body.get("reasoning") or {}
        effort_str = r.get("effort") if isinstance(r, dict) else None
        if effort_str is None:
            return ReasoningIntent(level=None, source_budget=None, explicit_off=False, present=False)
        level = _CHAT_NAME_TO_CANONICAL.get(effort_str)
        if level is None:
            return ReasoningIntent(level=None, source_budget=None, explicit_off=False, present=False)
        return ReasoningIntent(level=level, source_budget=None, explicit_off=False, present=True)

    def encode(self, aligned: AlignedEffort, cap: ReasoningCapability, variant: str) -> dict:
        if aligned.level is None:
            return {}
        return {"reasoning": {"effort": _canonical_to_chat_domain_name(aligned.level)}}
