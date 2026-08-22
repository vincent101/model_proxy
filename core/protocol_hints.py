"""协议转换观测与 operation compatibility 纯函数。"""

from .reasoning.registry import resolve_protocol

PROTOCOL_HINT_WINDOW_DAYS = 7
VALID_SOURCES = ("anthropic", "responses", "chat")
OP_MESSAGES = "messages"
OP_COUNT_TOKENS = "count_tokens"
OP_RESPONSES = "responses"
OP_CHAT_COMPLETIONS = "chat_completions"
OP_UNKNOWN = "unknown"

_KIND_BY_PAIR = {
    ("anthropic", "responses"): "a2r",
    ("responses", "anthropic"): "r2a",
    ("anthropic", "chat"): "a2chat",
    ("chat", "anthropic"): "chat2a",
    ("responses", "chat"): "r2chat",
    ("chat", "responses"): "chat2r",
}


def protocol_conversion_kind(source: str, target: str) -> str:
    """返回固定的协议方向枚举；同协议为 passthrough。"""
    if source == target and source in VALID_SOURCES:
        return "passthrough"
    return _KIND_BY_PAIR.get((source, target), "")


def operation_source(operation: str) -> str:
    return {
        OP_MESSAGES: "anthropic",
        OP_COUNT_TOKENS: "anthropic",
        OP_RESPONSES: "responses",
        OP_CHAT_COMPLETIONS: "chat",
    }.get(operation, "unknown")


def operation_compatible(operation: str, target_protocol: str) -> bool:
    """本期只有 count_tokens 限定 Anthropic；主生成操作沿用 translator 判定。"""
    return operation != OP_COUNT_TOKENS or target_protocol == "anthropic"


def build_protocol_conversion_hints(config: dict) -> list[dict]:
    """生成 route×tier×supply×source 的当前配置静态预览。"""
    supply_map = {
        s.get("id"): s for s in config.get("supplies", []) if isinstance(s, dict) and s.get("id")
    }
    hints = []
    for route in config.get("routes", []):
        if not isinstance(route, dict):
            continue
        for tier, supply_ids in (route.get("tiers") or {}).items():
            for supply_id in supply_ids or []:
                supply = supply_map.get(supply_id)
                if not supply:
                    continue
                try:
                    target = resolve_protocol(supply)
                except ValueError:
                    continue
                for source in VALID_SOURCES:
                    kind = protocol_conversion_kind(source, target)
                    hints.append({
                        "route": route.get("id", ""),
                        "tier": tier,
                        "supply": supply_id,
                        "source": source,
                        "target_protocol": target,
                        "is_conversion": kind != "passthrough",
                        "kind": kind,
                    })
    return hints
