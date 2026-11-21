"""provider 静态选择：按 supply 的 `protocol` 字段分流，不靠 URL/模型名猜测。

设计记录：docs/designs/2026-11-21-message_start用量形态与三类provider配置设计.md §3.2

**语义**：`protocol` 表示「交由哪个协议模块处理」，不再表示「上游线协议」。
对 legacy（anthropic/chat/responses）两者恰好重合；对 catpaw-* 则不同——
它们内部都走 anthropic 线协议，但由各自的模块处理。

这样做的好处：`provider: catpaw-cli` + `protocol: chat` 这类矛盾
**在结构上无法表达**，省掉一条互斥校验。
"""
from __future__ import annotations

# 处理模块标识
LEGACY_HTTP = "legacy_http"
CATPAW_CLI = "catpaw_cli"
CATPAW_IDE = "catpaw_ide"

# legacy 三个取值同时是「处理模块」与「线协议」
_LEGACY_PROTOCOLS = ("anthropic", "chat", "responses")

# 配置里可接受的 protocol 全集（供 CLI 交互流与文档引用）
VALID_CONFIG_PROTOCOLS = _LEGACY_PROTOCOLS + ("catpaw-cli", "catpaw-ide")

# provider 模块 → 上游线协议
_WIRE_PROTOCOL = {
    CATPAW_CLI: "anthropic",
    CATPAW_IDE: "anthropic",
}


def resolve_handler(supply: dict) -> str:
    """配置的 protocol 字段 → 处理模块标识。

    - anthropic / chat / responses → 同名 legacy 处理路径（历史行为，逐字不变）
    - catpaw-cli / catpaw_ide   → CATPAW_CLI
    - catpaw-ide / catpaw_ide   → CATPAW_IDE
    - 空值 → 交由 legacy 的 URL 推断（保持历史行为）
    - 其他 → ValueError（拼错必须暴露，不能静默降级）
    """
    raw = supply.get("protocol")
    sid = supply.get("id", "")
    if raw is None or raw == "":
        # 历史行为：无 protocol 时由 legacy 的 resolve_protocol 从 url 推断。
        return LEGACY_HTTP
    if not isinstance(raw, str):
        raise ValueError(f"supply {sid}: protocol 必须为字符串")
    name = raw.strip().lower().replace("_", "-")
    if name in _LEGACY_PROTOCOLS:
        return LEGACY_HTTP
    if name == "catpaw-cli":
        return CATPAW_CLI
    if name == "catpaw-ide":
        return CATPAW_IDE
    raise ValueError(
        f"supply {sid}: 未知 protocol {raw!r}"
        f"（支持: {' / '.join(VALID_CONFIG_PROTOCOLS)}）")


def wire_protocol(handler: str, legacy_protocol: str = "") -> str:
    """处理模块 → 上游线协议。

    legacy 的线协议由调用方提供的 legacy_protocol 决定（来自 URL 推断或显式声明）；
    catpaw-* 恒为 anthropic。
    """
    return _WIRE_PROTOCOL.get(handler, legacy_protocol)


def is_provider(handler: str) -> bool:
    """是否为独立 provider 模块（非 legacy）。"""
    return handler in (CATPAW_CLI, CATPAW_IDE)
