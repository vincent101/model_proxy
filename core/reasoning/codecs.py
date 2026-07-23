"""core.reasoning.codecs — 各协议（anthropic/chat/responses）的编解码器。

依赖 ladder + capability，不依赖 registry/server/translate。

每个 codec 声明自己支持的语法变体（variants），把"选语法"统一化：
- AnthropicReasoningCodec：双变体（enabled/adaptive），需要 400 自适应重试学习。
- ChatReasoningCodec / ResponsesReasoningCodec：单变体，无需选择。

编解码约定：syntax_adapt() 返回的 dict 是"该协议 reasoning 相关字段片段"，供调用方
（server.py）merge 进目标 body。约定：value 为 None 表示"删除该 key"（用于 STRIP/
PASSTHROUGH 场景清理游离字段），其余 value 表示"设置该 key"；不出现在返回 dict 里的
key 一律不动（既不新增也不删除）。abstract.kind == ABSENT 时返回 {}（不涉及任何 key）。

decode() 只产出 RawIntent（协议无关，未经跨模型换算），不再做任何钳位/换算——钳位/
映射统一收在 capability.remap()。syntax_adapt() 只根据 AbstractReasoning.kind 分派到
对应的 wire 结构模板，不需要重复判断 level 本身、也不需要 cap 参数（remap 阶段已把
level 收窄到该 target 能力范围内的合法值）。

OFF/MAX 统一约束（决策2，全代码库范围）：
- OFF 唯一允许的两处判断：capability.remap() 里的 OFF 吸收态 clause + capability.
  abstract_encode() 里 level==OFF -> DISABLED 的判断。本模块不再出现任何
  `if ... == CanonicalEffort.OFF` 判断，只根据 AbstractKind 分派。
- MAX 完全不允许特殊分支：作为思考子序列最高值，走跟 LOW/MEDIUM/HIGH 完全一样的
  查表路径，本模块不出现任何 `if ... == CanonicalEffort.MAX` 判断。
"""

from abc import ABC, abstractmethod

from .capability import AbstractKind, AbstractReasoning
from .ladder import CanonicalEffort, RawIntent, budget_to_canonical, canonical_to_budget

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
    def decode(self, body: dict) -> RawIntent:
        ...

    @abstractmethod
    def syntax_adapt(self, abstract: AbstractReasoning, variant: str) -> dict:
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
    # 默认变体改为 adaptive（原为 enabled）：Anthropic 新一代模型（Fable 系列，即当前
    # config 里的 claude-sonnet-5/aws.claude-opus-4.8）已废弃 enabled+budget_tokens 语法，
    # 只认 adaptive+output_config.effort（有本地运行日志证据：这两个模型的 enabled 语法
    # 被上游 400 拒绝、代理学到切换 adaptive 后成功）；glm-5.2/deepseek-v4-pro-tencent/
    # deepseek-v4-flash-tencent 三个模型的 adaptive 语法亦经真实探测确认可用（全部 200
    # 且真实产生 thinking 内容）。"adaptive 不可用"的模型（如 aws.claude-haiku-4.5）会在
    # 首次请求触发一次 400，由 interpret_rejection + pref_store.learn() 自适应重试机制
    # 自动识别并退回 enabled、缓存 48 小时，之后不再重复吃 400——一次性、能自愈，可接受。
    default_variant = ANTHROPIC_ADAPTIVE

    def decode(self, body: dict) -> RawIntent:
        thinking = body.get("thinking") or {}
        ttype = thinking.get("type") if isinstance(thinking, dict) else None
        oc = body.get("output_config") or {}
        effort_str = oc.get("effort") if isinstance(oc, dict) else None

        if ttype == "disabled":
            # 显式关闭：归一到 level=OFF, present=True（不再用独立 explicit_off 字段，
            # 关闭意图统一交给 remap() 里的 OFF 吸收态 clause 处理）。
            return RawIntent(level=CanonicalEffort.OFF, source_budget=None, present=True)

        if ttype == "enabled":
            budget = thinking.get("budget_tokens", 10000)
            level = budget_to_canonical(budget)
            return RawIntent(level=level, source_budget=budget, present=True)

        if ttype == "adaptive":
            level = _ANTHROPIC_NAME_TO_CANONICAL.get(effort_str)
            if level is None:
                # 未指定/不是 Anthropic 域内识别的标准词 → 沿用现状默认兜底：medium
                # （现状 bare adaptive 无 effort 时的既有行为，钳位/映射由 remap() 统一处理）
                level = CanonicalEffort.MEDIUM
            return RawIntent(level=level, source_budget=None, present=True)

        # thinking 缺失/其他值 → 不产出（含裸 output_config.effort 场景，保持现状"不生效"语义）
        return RawIntent(level=None, source_budget=None, present=False)

    def syntax_adapt(self, abstract: AbstractReasoning, variant: str) -> dict:
        if abstract.kind == AbstractKind.ABSENT:
            return {}
        if abstract.kind == AbstractKind.STRIP:
            return {"thinking": None, "output_config": None}
        if abstract.kind == AbstractKind.DISABLED:
            return {"thinking": {"type": "disabled"}, "output_config": None}

        # THINKING
        if variant == ANTHROPIC_ENABLED:
            budget = abstract.source_budget if abstract.source_budget is not None else canonical_to_budget(abstract.level)
            # 目标语法 enabled：无损回填客户端原始 budget_tokens（决策 #2），没有则兜底反算；
            # 且清除游离的 output_config.effort（现状 haiku 报错防护逻辑保留）。
            return {"thinking": {"type": "enabled", "budget_tokens": budget}, "output_config": None}

        # ANTHROPIC_ADAPTIVE：查表取协议域档名字符串，MAX 走正常查表，无特殊分支。
        name = _CANONICAL_TO_ANTHROPIC_NAME.get(abstract.level, "medium")
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

# Chat/Responses 协议域内的档名字符串集合（有 none，没有 max/minimal——该域词表本身比
# canonical 全序窄，属协议 wire 层限制，应体现为该域 ModelReasoningCapability 的
# effort_enum 配置不含 max/minimal，而不是在转换函数里写 if 分支降级）。
_CHAT_NAME_TO_CANONICAL = {
    "none": CanonicalEffort.OFF,
    "low": CanonicalEffort.LOW,
    "medium": CanonicalEffort.MEDIUM,
    "high": CanonicalEffort.HIGH,
    "xhigh": CanonicalEffort.XHIGH,
}
_CANONICAL_TO_CHAT_NAME = {v: k for k, v in _CHAT_NAME_TO_CANONICAL.items()}


def _canonical_to_openai_effort_name(level: CanonicalEffort) -> str:
    """canonical → Chat/Responses 域内档名字符串，纯查表 + 默认兜底，无 MAX/MINIMAL 专门
    if 分支（决策2：MAX 完全统一，走查表路径；该域本就不该被配置出 MAX，配置层责任见
    方案文档 §3.1，这里只做兜底防御，不做针对 MAX 的判断）。
    """
    return _CANONICAL_TO_CHAT_NAME.get(level, "medium")


class ChatReasoningCodec(ReasoningCodec):
    """reasoning_effort 字段，单变体。"""

    protocol = "chat"
    variants = (CHAT_EFFORT,)
    default_variant = CHAT_EFFORT

    def decode(self, body: dict) -> RawIntent:
        effort_str = body.get("reasoning_effort")
        if effort_str is None:
            return RawIntent(level=None, source_budget=None, present=False)
        level = _CHAT_NAME_TO_CANONICAL.get(effort_str)
        if level is None:
            return RawIntent(level=None, source_budget=None, present=False)
        return RawIntent(level=level, source_budget=None, present=True)

    def syntax_adapt(self, abstract: AbstractReasoning, variant: str) -> dict:
        if abstract.kind == AbstractKind.ABSENT:
            return {}
        if abstract.kind == AbstractKind.STRIP:
            return {"reasoning_effort": None}
        if abstract.kind == AbstractKind.DISABLED:
            return {"reasoning_effort": "none"}
        return {"reasoning_effort": _canonical_to_openai_effort_name(abstract.level)}


# ============================================================================
# Responses（OpenAI Responses API）：reasoning.effort 字段，单变体
# ============================================================================

class ResponsesReasoningCodec(ReasoningCodec):
    """reasoning.effort 字段，单变体。"""

    protocol = "responses"
    variants = (RESP_EFFORT,)
    default_variant = RESP_EFFORT

    def decode(self, body: dict) -> RawIntent:
        r = body.get("reasoning") or {}
        effort_str = r.get("effort") if isinstance(r, dict) else None
        if effort_str is None:
            return RawIntent(level=None, source_budget=None, present=False)
        level = _CHAT_NAME_TO_CANONICAL.get(effort_str)
        if level is None:
            return RawIntent(level=None, source_budget=None, present=False)
        return RawIntent(level=level, source_budget=None, present=True)

    def syntax_adapt(self, abstract: AbstractReasoning, variant: str) -> dict:
        if abstract.kind == AbstractKind.ABSENT:
            return {}
        if abstract.kind == AbstractKind.STRIP:
            return {"reasoning": None}
        if abstract.kind == AbstractKind.DISABLED:
            return {"reasoning": {"effort": "none"}}
        return {"reasoning": {"effort": _canonical_to_openai_effort_name(abstract.level)}}
