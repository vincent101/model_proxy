#!/usr/bin/env python3
"""model_proxy install helper — 把 client_token 接入各 SDK 的本地配置。

由 model_proxy_cli.sh 的 install 子命令调用。职责：
1. 检测四个 SDK（claude/codex/hermes/openclaw）本机是否已装（配置目录/文件是否存在）。
2. 按协议从 strategies 里过滤出候选 client_token，交互式让用户选择。
3. 已装：备份原配置后按该 SDK 格式写入；未装：打印配置片段供手动粘贴。

红线：只读 strategies，不改 strategies（install 只是让 SDK 指向某个 token，不改
token→route 绑定关系）。纯标准库，不引入第三方依赖（yaml/json5 均不用第三方库）。
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# SDK 元信息：协议 / 配置路径 / 检测方式
# ---------------------------------------------------------------------------

SDKS = {
    "claude": {
        "label": "claude (Claude Code)",
        "protocol": "anthropic",
        "config_path": Path.home() / ".claude" / "settings.json",
    },
    "codex": {
        "label": "codex (codex-cli)",
        "protocol": "responses",
        "config_path": Path.home() / ".codex" / "config.toml",
    },
    "hermes": {
        "label": "hermes",
        "protocol": None,  # 可选，按 api_mode 决定，见下
        "config_path": Path.home() / ".hermes" / "config.yaml",
    },
    "openclaw": {
        "label": "openclaw",
        "protocol": None,  # 可选，按 api 决定
        "config_path": Path.home() / ".openclaw" / "openclaw.json",
    },
}

ORDER = ["claude", "codex", "hermes", "openclaw"]


def detect_installed(name: str) -> bool:
    """SDK 是否"已装"：配置目录存在即认为已装（不要求配置文件本身存在）。"""
    cfg_path = SDKS[name]["config_path"]
    return cfg_path.parent.exists()


# ---------------------------------------------------------------------------
# strategy → 协议过滤候选 token
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def candidate_tokens(cfg: dict, protocol: str | None) -> list[dict]:
    """按协议过滤候选 client_token。protocol=None 时不过滤（hermes/openclaw 双协议可选）。

    返回 [{"client_token":..., "route_id":..., "note":..., "protocol":...}, ...]
    """
    routes_map = {r.get("id"): r for r in cfg.get("routes", [])}
    supply_map = {s.get("id"): s for s in cfg.get("supplies", [])}
    result = []
    for st in cfg.get("strategies", []):
        route = routes_map.get(st.get("route_id"))
        if route is None:
            continue
        # 取该 route 任一 tier 里第一个 supply 的 protocol 作为该 token 的协议
        # （同一 route 内所有 supply 理论上应同协议，取第一个即可代表）
        tier_protocol = None
        for tier_sids in (route.get("tiers") or {}).values():
            for sid in tier_sids or []:
                supply = supply_map.get(sid)
                if supply:
                    tier_protocol = supply.get("protocol")
                    break
            if tier_protocol:
                break
        if protocol is not None and tier_protocol != protocol:
            continue
        result.append({
            "client_token": st.get("client_token"),
            "route_id": st.get("route_id"),
            "note": st.get("note", "") or "",
            "protocol": tier_protocol,
        })
    return result


def choose_token(cfg: dict, protocol: str | None, sdk_label: str) -> str | None:
    cands = candidate_tokens(cfg, protocol)
    if not cands:
        print(f"  [{sdk_label}] 无协议匹配（protocol={protocol}）的 client_token，跳过。"
              f"请先用 strategy add 新增对应协议的绑定。")
        return None
    if len(cands) == 1:
        c = cands[0]
        print(f"  [{sdk_label}] 唯一匹配 token: {c['client_token']} "
              f"(route_id={c['route_id']}, note={c['note']})")
        if input("  使用该 token? [Y/n]: ").strip().lower() in ("n", "no"):
            return None
        return c["client_token"]
    print(f"  [{sdk_label}] 协议匹配的候选 token:")
    for i, c in enumerate(cands):
        print(f"    [{i}] {c['client_token']:16} (route_id={c['route_id']}, note={c['note']})")
    raw = input("  选择序号: ").strip()
    if not raw.isdigit() or not (0 <= int(raw) < len(cands)):
        print("  Error: 无效序号，跳过该 SDK。")
        return None
    return cands[int(raw)]["client_token"]


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

def backup(path: Path) -> Path | None:
    """备份原配置，失败返回 None（调用方应中止写入，不动原文件）。"""
    if not path.exists():
        return path  # 不存在不需要备份，视为"无需备份即成功"
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bak = path.with_name(path.name + f".bak.{ts}")
    try:
        bak.write_bytes(path.read_bytes())
    except OSError as e:
        print(f"  Error: backup failed ({e})，中止，不改动 {path}")
        return None
    print(f"  Backup: {bak}")
    return bak


# ---------------------------------------------------------------------------
# 各 SDK 安装实现
# ---------------------------------------------------------------------------

def base_url_for(port: str) -> str:
    return f"http://localhost:{port}/"


def install_claude(token: str, port: str) -> None:
    cfg_path = SDKS["claude"]["config_path"]
    base_url = base_url_for(port)
    if not cfg_path.exists():
        print_manual_snippet("claude", f"""\
在 {cfg_path} 的 "env" 字段下添加/修改：
  "ANTHROPIC_BASE_URL": "{base_url}",
  "ANTHROPIC_AUTH_TOKEN": "{token}",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus"
""")
        return
    if backup(cfg_path) is None:
        return
    cfg = load_config(str(cfg_path))
    env = cfg.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku")
    env.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet")
    env.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"  已写入 {cfg_path}：ANTHROPIC_BASE_URL={base_url} ANTHROPIC_AUTH_TOKEN={token}")
    print("  请重启 Claude Code 生效。")


def install_codex(token: str, port: str) -> None:
    """codex-cli：TOML 配置。标准库无 TOML writer，用正则文本拼接，只处理本工具
    自己命名的 [model_providers.model_proxy] 段 + 顶层 model/model_provider 两个 key，
    不触碰其他已存在的 provider 段。

    base_url 拼到 /v1 层级（不含 /responses），wire_api="responses" 由 codex 自己
    拼 /responses 后缀——这一拼接规则依据本项目 server.py detect_source() 对
    /v1/responses 路径后缀的识别逻辑反推，未逐字核对 codex 官方文档字段名，
    如实际接入报 404/400 请核对 codex 官方 config.toml 文档调整 base_url 层级。
    """
    cfg_path = SDKS["codex"]["config_path"]
    base_url = base_url_for(port).rstrip("/") + "/v1"
    env_key = "MODEL_PROXY_CODEX_TOKEN"
    provider_name = "model_proxy"
    # codex 顶层 model 需要是 model_proxy 认识的三个档位字面值之一（见 server.py
    # _MODEL_TIER_MAP，只精确匹配 claude-opus/claude-sonnet/claude-haiku）
    model_label = "claude-sonnet"

    provider_block = (
        f'[model_providers.{provider_name}]\n'
        f'base_url = "{base_url}"\n'
        f'wire_api = "responses"\n'
        f'env_key = "{env_key}"\n'
    )

    if not cfg_path.exists():
        print_manual_snippet("codex", f"""\
在 {cfg_path} 中添加：

model = "{model_label}"
model_provider = "{provider_name}"

{provider_block}
并在启动 codex 前设置环境变量（appkey 用途，这里用 model_proxy 的 client_token）：
  export {env_key}="{token}"
""")
        return

    if backup(cfg_path) is None:
        return

    text = cfg_path.read_text(encoding="utf-8")

    # 替换/追加 [model_providers.model_proxy] 段（删掉旧的同名段，避免重复）
    section_re = re.compile(
        rf"\[model_providers\.{re.escape(provider_name)}\].*?(?=\n\[|\Z)", re.DOTALL)
    if section_re.search(text):
        text = section_re.sub(provider_block.rstrip("\n"), text)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + provider_block

    # 顶层 model / model_provider：存在则替换，不存在则在文件头追加
    def _set_top_key(t: str, key: str, value: str) -> str:
        pattern = re.compile(rf"^{key}\s*=.*$", re.MULTILINE)
        line = f'{key} = "{value}"'
        if pattern.search(t):
            return pattern.sub(line, t, count=1)
        return line + "\n" + t

    text = _set_top_key(text, "model", model_label)
    text = _set_top_key(text, "model_provider", provider_name)

    cfg_path.write_text(text, encoding="utf-8")
    print(f"  已写入 {cfg_path}：model_provider={provider_name} base_url={base_url}")
    print(f"  请在启动 codex 前设置环境变量: export {env_key}=\"{token}\"")
    print("  （appkey 不写入配置文件本体，走环境变量注入，与 codex env_key 机制一致）")


def install_hermes(token: str, port: str) -> None:
    """hermes：yaml 配置。标准库无 yaml 解析/写入器，为避免盲目文本拼接破坏现有
    yaml 结构（不解析就改写风险不可控），统一走"打印片段供手动粘贴"分支，
    不管配置文件是否已存在都不自动写入。"""
    cfg_path = SDKS["hermes"]["config_path"]
    base_url = base_url_for(port)
    provider_name = "model_proxy"
    print_manual_snippet("hermes", f"""\
（hermes 用 yaml 配置，标准库无 yaml 解析器，为避免破坏现有文件结构不自动写入，
 请手动编辑 {cfg_path}）

custom_providers:
  - name: {provider_name}
    base_url: {base_url}
    key_env: HERMES_MODEL_PROXY_KEY
    api_mode: anthropic_messages   # 或 chat_completions，按你 strategy 绑定的 route 协议选

model:
  provider: "custom:{provider_name}"

并在 {cfg_path.parent / '.env'} 中添加：
  HERMES_MODEL_PROXY_KEY={token}
""")


def install_openclaw(token: str, port: str) -> None:
    """openclaw：json5 配置，标准库 json 是 json5 的严格子集解析器——若现有文件
    只用了标准 JSON 语法（无注释/尾逗号），json.load 能正常读取，可安全写回；
    若解析失败（用了 json5 专属语法），降级为打印片段，不强行写入。"""
    cfg_path = SDKS["openclaw"]["config_path"]
    base_url = base_url_for(port)
    provider_name = "model_proxy"
    api_mode = "anthropic"  # 或 "openai-completions"，按 strategy 协议选

    if not cfg_path.exists():
        print_manual_snippet("openclaw", f"""\
在 {cfg_path} 的 "models"."providers" 下添加：
  "{provider_name}": {{
    "baseUrl": "{base_url}",
    "apiKey": "{token}",
    "api": "{api_mode}"
  }}
""")
        return

    try:
        cfg = load_config(str(cfg_path))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  {cfg_path} 解析失败（可能用了 json5 专属语法，标准库 json 无法解析）: {e}")
        print_manual_snippet("openclaw", f"""\
在 {cfg_path} 的 "models"."providers" 下添加：
  "{provider_name}": {{
    "baseUrl": "{base_url}",
    "apiKey": "{token}",
    "api": "{api_mode}"
  }}
""")
        return

    if backup(cfg_path) is None:
        return

    models = cfg.setdefault("models", {})
    providers = models.setdefault("providers", {})
    providers[provider_name] = {
        "baseUrl": base_url,
        "apiKey": token,
        "api": api_mode,
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"  已写入 {cfg_path}：providers.{provider_name}.baseUrl={base_url}")


def print_manual_snippet(sdk: str, snippet: str) -> None:
    print(f"  [{sdk}] 未检测到配置目录/文件，或不支持自动写入，请手动添加以下配置片段：")
    print("  " + "-" * 58)
    for line in snippet.splitlines():
        print(f"  {line}")
    print("  " + "-" * 58)


_INSTALLERS = {
    "claude": install_claude,
    "codex": install_codex,
    "hermes": install_hermes,
    "openclaw": install_openclaw,
}


# ---------------------------------------------------------------------------
# 顶层命令
# ---------------------------------------------------------------------------

def cmd_list(config_path: str, port: str) -> None:
    cfg = load_config(config_path)
    print("SDK 检测状态:")
    for name in ORDER:
        meta = SDKS[name]
        installed = detect_installed(name)
        status = "已装" if installed else "未装"
        proto = meta["protocol"] or "可选(按api_mode/api)"
        print(f"  [{name:10}] {meta['label']:24} protocol={proto:24} "
              f"config_dir={meta['config_path'].parent}  状态={status}")


def _interactive_select() -> list[str]:
    """交互式列出四个 SDK + 检测状态，读用户输入（编号/逗号分隔）选择要装哪些。"""
    print("支持的 SDK:")
    for i, name in enumerate(ORDER):
        meta = SDKS[name]
        installed = detect_installed(name)
        status = "已装" if installed else "未装"
        print(f"  [{i}] {name:10} {meta['label']:24} 状态={status}")
    raw = input("选择要安装的 SDK 序号（可逗号分隔多个，如 0,2）: ").strip()
    if not raw:
        return []
    idxs = [x.strip() for x in raw.split(",") if x.strip()]
    selected = []
    for idx in idxs:
        if not idx.isdigit() or not (0 <= int(idx) < len(ORDER)):
            print(f"  忽略无效序号: {idx}")
            continue
        selected.append(ORDER[int(idx)])
    return selected


def cmd_install(config_path: str, port: str, selected: list[str]) -> None:
    cfg = load_config(config_path)
    if not selected:
        selected = _interactive_select()
    if not selected:
        print("未选择任何 SDK，退出。")
        return
    for name in selected:
        meta = SDKS[name]
        print(f"\n== 安装 {meta['label']} ==")
        installed = detect_installed(name)
        print(f"  检测状态: {'已装' if installed else '未装'} ({meta['config_path'].parent})")
        token = choose_token(cfg, meta["protocol"], meta["label"])
        if token is None:
            continue
        _INSTALLERS[name](token, port)


def main() -> None:
    if len(sys.argv) < 4:
        print("用法: _install_ops.py list|install <config_file> <port> [sdk1,sdk2,...]",
              file=sys.stderr)
        sys.exit(1)
    action = sys.argv[1]
    config_path = sys.argv[2]
    port = sys.argv[3]

    if action == "list":
        cmd_list(config_path, port)
        return
    if action == "install":
        selected_raw = sys.argv[4] if len(sys.argv) > 4 else ""
        selected = [s.strip() for s in selected_raw.split(",") if s.strip()]
        bad = [s for s in selected if s not in SDKS]
        if bad:
            print(f"Error: 未知 SDK: {bad}（可选: {ORDER}）", file=sys.stderr)
            sys.exit(1)
        # selected 为空时（未从命令行传 sdk 列表）交由 cmd_install 走交互式选择
        cmd_install(config_path, port, selected)
        return
    print(f"Error: 未知 action: {action}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
