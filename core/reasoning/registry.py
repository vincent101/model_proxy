"""core.reasoning.registry — protocol 字符串 → codec 单例。

依赖 codecs，不依赖 server/translate。
"""

from .codecs import AnthropicReasoningCodec, ChatReasoningCodec, ReasoningCodec, ResponsesReasoningCodec

_ANTHROPIC = AnthropicReasoningCodec()
_CHAT = ChatReasoningCodec()
_RESPONSES = ResponsesReasoningCodec()

_REGISTRY = {
    "anthropic": _ANTHROPIC,
    "chat": _CHAT,
    "responses": _RESPONSES,
}


def get_codec(protocol: str) -> ReasoningCodec:
    codec = _REGISTRY.get(protocol)
    if codec is None:
        raise KeyError(f"no reasoning codec registered for protocol={protocol!r}")
    return codec


def apply_fields(body: dict, fields: dict) -> None:
    """把 encode() 返回的字段片段 merge 进 body（原地修改）。

    约定：value 为 None 表示删除该 key（用于清理游离的 output_config.effort 等场景）；
    其余 value 表示设置该 key；fields 里不出现的 key 不动。
    """
    for key, value in (fields or {}).items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
