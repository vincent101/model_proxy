"""provider 静态选择：按 supply 显式声明分流，不靠 URL/模型名猜测。

设计记录：docs/designs/2026-11-21-CatPaw-IDE凭据单点管理方案.md
"""

LEGACY_HTTP = "legacy_http"
CATPAW_IDE = "catpaw_ide"

VALID_PROVIDERS = (LEGACY_HTTP, CATPAW_IDE)


def resolve_provider(supply: dict) -> str:
    """返回 supply 的 provider 种类。

    - 无 provider 字段 → legacy_http（历史行为，逐字不变）
    - catpaw-ide / catpaw_ide → catpaw_ide
    - 其他非空值 → ValueError（拼错必须暴露，不能静默降级到 HTTP）
    """
    raw = supply.get("provider")
    if raw is None or raw == "":
        return LEGACY_HTTP
    if not isinstance(raw, str):
        raise ValueError(
            f"supply {supply.get('id', '')}: provider 必须为字符串")
    name = raw.strip().lower().replace("_", "-")
    if name == "catpaw-ide":
        return CATPAW_IDE
    raise ValueError(
        f"supply {supply.get('id', '')}: 未知 provider {raw!r}"
        f"（支持: 留空 / catpaw-ide）")
