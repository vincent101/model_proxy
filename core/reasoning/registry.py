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


_PROTOCOL_URL_SUFFIX = {          # 尾缀 → protocol，唯一权威表，按此顺序检查即可（三者互斥无公共后缀）
    "/v1/messages": "anthropic",
    "/v1/responses": "responses",
    "/chat/completions": "chat",
}


def infer_protocol_from_url(url: str) -> "str | None":
    """从完整终态 url 的路径尾缀推断 protocol。三个尾缀互斥无公共后缀，可用 endswith 可靠判断。
    推断不到返回 None（调用方决定是否报错），不做任何猜测性兜底。
    """
    u = (url or "").split("?", 1)[0].rstrip("/")
    for suffix, proto in _PROTOCOL_URL_SUFFIX.items():
        if u.endswith(suffix):
            return proto
    return None


def resolve_protocol(supply: dict) -> str:
    """supply → protocol 的唯一权威解析，全代码库仅此一处实现该判断逻辑。

    优先级：显式 supply["protocol"]（若合法）> 从 supply["url"] 尾缀推断 > 抛错。
    绝不兜底默认某个协议——推断不到就是配置错误，必须让用户看到明确报错去修，
    而不是让请求悄悄走错转换分支产生难排查的错误响应。
    """
    explicit = (supply.get("protocol") or "").strip()
    if explicit:
        if explicit not in _REGISTRY:
            raise ValueError(f"supply {supply.get('id')!r}: 非法 protocol {explicit!r}，"
                              f"合法值: {sorted(_REGISTRY.keys())}")
        return explicit
    inferred = infer_protocol_from_url(supply.get("url", ""))
    if inferred is None:
        raise ValueError(
            f"supply {supply.get('id')!r}: 无法从 url 推断 protocol "
            f"（url={supply.get('url')!r} 尾缀不属于 {list(_PROTOCOL_URL_SUFFIX)}），"
            f"请显式填写 protocol 字段")
    return inferred


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
