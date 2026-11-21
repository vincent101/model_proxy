"""CatPaw CLI provider：mcli 网关的样板字段内置。

设计记录：docs/designs/2026-11-21-message_start用量形态与三类provider配置设计.md §3.4

**职责边界**：本模块只提供「该 provider 的固定属性」，不重新实现路由、协议转换、
流解析、signature 学习、cooldown、failover 或 count_tokens 行为。出站走
`http_compat` 与 legacy **完全相同**的 HTTP 路径。

**凭据归属**：文件由 mtsso_keeper 的 mcli 段维护（探测 health → 失效重铸 → 原子回写），
本模块只读。这与 catpaw-ide 是同一分工：keeper 管、proxy 只读。
"""
from __future__ import annotations

import os

# ── 内置常量（原写在每条 supply 上，12 条各重复一遍）──────────────────
ENDPOINT = "https://mcli.sankuai.com/v1/messages"

# mc CLI 自身也在用这个文件，keeper 代管而非独占；model_proxy 只读取。
CREDENTIAL_FILE = "~/.config/mcopilot-cli/.config.yaml"

EXTRA_HEADERS = {"X-Working-Dir": "/"}

# 计费标识块。cc_version 跟随 Claude Code 版本，升级 CC 时 bump。
# 依据：当前值含 cch=00000（占位）仍能正常调用，说明 mcli 大概率不严格校验该头；
# 若将来实测发现会校验，再考虑提为可配置项。
SYSTEM_INJECT = (
    "x-anthropic-billing-header: cc_version=2.1.196; "
    "cc_entrypoint=sdk-cli; cch=00000;"
)

# 上游线协议（模块自带，不接受配置覆盖）
WIRE_PROTOCOL = "anthropic"


class ConfigError(ValueError):
    """supply 声明与本模块内置属性冲突。"""


# provider supply 上禁止出现的字段：这些已内置，写了说明配置有误
_FORBIDDEN_SUPPLY_FIELDS = ("url", "appkey", "appkey_file",
                            "extra_headers", "system_inject", "credential_file",
                            "bridge_lib")


def validate_supply(supply: dict) -> None:
    """校验 CatPaw CLI supply 形态。

    provider supply 只应声明 `id` / `protocol` / `target_model` /
    `reasoning_capability`（及 `cooldown_seconds`）。其余字段若出现即报错——
    宁可让人看见配置错误，也不要静默忽略后产生难以排查的行为差异。
    """
    sid = supply.get("id", "")
    present = [k for k in _FORBIDDEN_SUPPLY_FIELDS if supply.get(k)]
    if present:
        raise ConfigError(
            f"supply {sid}: catpaw-cli 不支持字段 {present}——"
            "端点/凭据/header/注入均已内置到模块，请删除这些字段")


def credential_path() -> str:
    """凭据文件实际路径（展开 ~）。供误导排查与测试 patch。"""
    return os.path.expanduser(CREDENTIAL_FILE)


def target_model_of(supply: dict) -> str:
    model = supply.get("target_model")
    if not model:
        raise ConfigError(
            f"supply {supply.get('id', '')}: catpaw-cli 须配置 target_model")
    return model


def materialize(supply: dict) -> dict:
    """把 catpaw-cli supply 展开成 legacy 可直接消费的形态。

    展开后 `protocol` 变为线协议（anthropic），`url`/`appkey_file`/
    `extra_headers`/`system_inject` 全部补齐。这样下游的
    `resolve_appkey` / `inject_system_block` / `build_standard_headers` /
    target_url 构造**完全复用既有实现**，无需为 CLI 增加任何分支。

    返回新 dict，不改动入参（供 cooldown/日志等继续使用原始 supply）。
    """
    out = dict(supply)
    out["url"] = ENDPOINT
    out["appkey_file"] = CREDENTIAL_FILE
    out["extra_headers"] = dict(EXTRA_HEADERS)
    out["system_inject"] = SYSTEM_INJECT
    out["protocol"] = WIRE_PROTOCOL
    return out
