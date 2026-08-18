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
import difflib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from _config_ops import compact_config_json, confirm, load_config
from _format_ops import strategy_route_desc
from core.reasoning.registry import resolve_protocol

# ---------------------------------------------------------------------------
# SessionStart hook（ensure_model_proxy.sh）路径常量
# ---------------------------------------------------------------------------

_MODEL_PROXY_DIR = Path(__file__).resolve().parent          # tools/model_proxy
_VAULT_ROOT = _MODEL_PROXY_DIR.parents[1]                    # vault 根
_CLAUDE_SETTINGS = _VAULT_ROOT / ".claude" / "settings.json"
_HOOK_SCRIPT_REL = str(
    _MODEL_PROXY_DIR.relative_to(_VAULT_ROOT) / "hooker" / "ensure_model_proxy.sh"
)  # 相对 vault 根，如 "tools/model_proxy/hooker/ensure_model_proxy.sh"

# codex model catalog 模板（仓库内置，base_instructions 占位符）与目标路径
_CODEX_CATALOG_TEMPLATE = _MODEL_PROXY_DIR / "assets" / "codex_catalog_template.json"
_CODEX_CATALOG_TARGET = Path.home() / ".codex" / "model-catalogs" / "model_proxy_catalog.json"
_CODEX_CATALOG_TOML_KEY = "model_catalog_json"
_CODEX_CATALOG_TOML_VALUE = "~/.codex/model-catalogs/model_proxy_catalog.json"
_PROMPT_MD_URL = "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/prompt.md"
_PROMPT_MD_TIMEOUT = 30  # 拉取超时秒数

# Claude Code onboarding 状态文件（与 settings.json 不同文件，不混用）
_CLAUDE_ONBOARDING = Path.home() / ".claude.json"

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

def candidate_tokens(cfg: dict, protocol: str | None) -> list[dict]:
    """按协议过滤候选 client_token。protocol=None 时不过滤（hermes/openclaw 双协议可选）。

    兼容 route_id 单值与 route_pool 两种写法：取 strategy 绑定的第一个合法 route
    做协议推断。返回展示字段 route_desc 用 strategy_route_desc 生成兼容描述。

    返回 [{"client_token":..., "route_desc":..., "note":..., "protocol":...}, ...]
    """
    routes_map = {r.get("id"): r for r in cfg.get("routes", [])}
    supply_map = {s.get("id"): s for s in cfg.get("supplies", [])}
    result = []
    for st in cfg.get("strategies", []):
        # 兼容 route_id 单值与 route_pool 两种写法，收集该 strategy 引用的所有 route_id
        route_ids = []
        if st.get("route_id"):
            route_ids.append(st["route_id"])
        for item in st.get("route_pool") or []:
            if isinstance(item, dict) and item.get("route_id"):
                route_ids.append(item["route_id"])
        if not route_ids:
            continue
        # 取第一个合法 route 做协议推断（同一 strategy 内 route 理论上应同协议）
        route = None
        for rid in route_ids:
            route = routes_map.get(rid)
            if route is not None:
                break
        if route is None:
            continue
        # 取该 route 任一 tier 里第一个 supply 的 protocol 作为该 token 的协议
        # （同一 route 内所有 supply 理论上应同协议，取第一个即可代表）。
        # 这里是 CLI 交互展示阶引导（非转发主流程），某条 supply 配置有误（url/protocol
        # 都推断不出协议）时不应阻断其他候选 token 的展示，warning 后跳过该条即可。
        tier_protocol = None
        for tier_sids in (route.get("tiers") or {}).values():
            for sid in tier_sids or []:
                supply = supply_map.get(sid)
                if supply:
                    try:
                        tier_protocol = resolve_protocol(supply)
                    except ValueError as e:
                        print(f"  Warning: supply={sid!r} 协议解析失败（{e}），跳过该候选。")
                        continue
                    break
            if tier_protocol:
                break
        if protocol is not None and tier_protocol != protocol:
            continue
        result.append({
            "client_token": st.get("client_token"),
            "route_desc": strategy_route_desc(st),
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
              f"(route={c['route_desc']}, note={c['note']})")
        if input("  使用该 token? [Y/n]: ").strip().lower() in ("n", "no"):
            return None
        return c["client_token"]
    print(f"  [{sdk_label}] 协议匹配的候选 token:")
    for i, c in enumerate(cands):
        print(f"    [{i}] {c['client_token']:16} (route={c['route_desc']}, note={c['note']})")
    raw = input("  选择序号: ").strip()
    if not raw.isdigit() or not (0 <= int(raw) < len(cands)):
        print("  Error: 无效序号，跳过该 SDK。")
        return None
    return cands[int(raw)]["client_token"]


# ---------------------------------------------------------------------------
# 备份 + 预览确认写入
# ---------------------------------------------------------------------------

def preview_confirm_write(
    cfg_path: Path,
    old_text: str,
    new_text: str,
    sdk_label: str,
    success_msg_lines: list[str],
) -> bool:
    """展示 old_text -> new_text 的统一 diff 预览，用户确认后备份+写入。

    返回是否实际发生了写入（False 覆盖：无变化 / 用户取消 / 备份失败 / 写入失败）。
    """
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{cfg_path.name} (当前)",
        tofile=f"{cfg_path.name} (改动后)",
    ))
    if not diff_lines:
        print(f"  [{sdk_label}] 内容无变化，跳过。")
        return False

    print(f"  [{sdk_label}] 改动预览:")
    for line in diff_lines:
        print("  " + line.rstrip("\n"))

    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bak_path = cfg_path.with_name(cfg_path.name + f".bak.{ts}")
    has_original = cfg_path.exists()
    if has_original:
        print(f"  确认后将备份原文件到: {bak_path}")
    else:
        print(f"  [{sdk_label}] 原文件不存在，确认后将直接写入（无需备份）。")

    if not confirm("  确认执行以上改动?"):
        print("  已取消，未改动任何文件。")
        return False

    if has_original:
        try:
            bak_path.write_bytes(cfg_path.read_bytes())
        except OSError as e:
            print(f"  Error: backup failed ({e})，中止，不改动 {cfg_path}")
            return False
        print(f"  Backup: {bak_path}")

    try:
        cfg_path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        if cfg_path.exists():
            print(f"  Error: 写入失败 ({e})，原文件已备份到 {bak_path}")
        else:
            print(f"  Error: 写入失败 ({e})")
        return False

    for line in success_msg_lines:
        print(f"  {line}")
    return True


# ---------------------------------------------------------------------------
# SessionStart hook（ensure_model_proxy.sh）检测/归一化/修复
# ---------------------------------------------------------------------------

def _expected_hook_command() -> str:
    """本工具在 SessionStart 里应该存在的、唯一正确的 command 字面量。"""
    return f'bash "${{CLAUDE_PROJECT_DIR}}/{_HOOK_SCRIPT_REL}"'


def _is_model_proxy_hook(entry: dict) -> bool:
    """判断 SessionStart 数组里的一个元素是否命中本工具的 hook：
    command 字符串包含子串 "ensure_model_proxy.sh"（不依赖路径是否正确，
    以便抓到旧路径残留）。"""
    for h in entry.get("hooks", []) or []:
        if "ensure_model_proxy.sh" in (h.get("command") or ""):
            return True
    return False


def _resolve_hook_path(command: str, vault_root: Path) -> Path | None:
    """把 command 里的 ${CLAUDE_PROJECT_DIR} 替换为 vault_root，提取被引号包裹的
    路径并 resolve。提取不到引号内容时返回 None。"""
    expanded = command.replace("${CLAUDE_PROJECT_DIR}", str(vault_root))
    m = re.search(r'"([^"]+)"', expanded)
    if not m:
        return None
    return Path(m.group(1)).resolve()


def _normalize_session_start(entries: list, vault_root: Path) -> list:
    """把 SessionStart 数组归一化为：恰好一条正确的 model_proxy hook，零条 stale，
    其他非命中条目原样保留（原序、原位置）。不修改入参 entries，返回新列表。

    归一化规则：
    - 命中集合里若已存在 >=1 条 correct → 保留第一条 correct（原位置不动），
      删除其余所有命中条目（多余 correct 重复 + 全部 stale）。
    - 命中集合里无 correct、有 stale → 删除全部 stale，末尾新增一条 correct。
    - 无任何命中 → 末尾新增一条 correct。
    """
    expected_path = (vault_root / _HOOK_SCRIPT_REL).resolve()
    correct_entry = {
        "hooks": [
            {"type": "command", "command": _expected_hook_command()},
        ]
    }

    hits = [e for e in entries if _is_model_proxy_hook(e)]
    if not hits:
        return list(entries) + [correct_entry]

    def _is_correct(entry: dict) -> bool:
        for h in entry.get("hooks", []) or []:
            cmd = h.get("command") or ""
            if "ensure_model_proxy.sh" not in cmd:
                continue
            if _resolve_hook_path(cmd, vault_root) == expected_path:
                return True
        return False

    correct_hits = [e for e in hits if _is_correct(e)]

    if correct_hits:
        keep = correct_hits[0]
        result = []
        kept_once = False
        for e in entries:
            if e is keep and not kept_once:
                result.append(e)
                kept_once = True
            elif e in hits:
                continue  # 其余命中条目（多余 correct 重复 + 全部 stale）删除
            else:
                result.append(e)
        return result

    # 无 correct、有 stale：删全部 stale，末尾追加 correct
    result = [e for e in entries if e not in hits]
    result.append(correct_entry)
    return result


def ensure_session_hook(settings_path: "Path | None" = None) -> None:
    """检测 .claude/settings.json 的 SessionStart 是否含一条正确的 model_proxy
    自启 hook，缺失/路径错误则清理+补齐（预览确认后才写入）。

    settings.json 不存在时打印提示并跳过（install 不负责创建整份 settings.json）；
    文件存在但无 hooks/SessionStart 键时视为空列表，注入时补建。

    settings_path 默认 None 时在函数体内读取模块级 _CLAUDE_SETTINGS（而不是把它
    写进参数默认值）——参数默认值在 def 执行时就一次性求值绑定死，之后即便
    patch 掉模块级 _CLAUDE_SETTINGS，已绑定的默认值不会跟着变，会导致测试无法
    通过 patch 模块常量的方式覆盖默认路径分支（cmd_install() 的真实调用路径）。
    写成运行时读取，patch("_install_ops._CLAUDE_SETTINGS", tmp_path) 即可让
    cmd_install() 实际触发的默认参数分支也被测试覆盖。
    """
    if settings_path is None:
        settings_path = _CLAUDE_SETTINGS
    if not settings_path.exists():
        print(f"[model_proxy hook] {settings_path} 不存在，跳过 hook 检查"
              "（install 不负责创建整份 settings.json）。")
        return

    old_text = settings_path.read_text(encoding="utf-8")
    cfg = json.loads(old_text)

    expected_path = (_VAULT_ROOT / _HOOK_SCRIPT_REL).resolve()
    if not expected_path.exists():
        print(f"[model_proxy hook] Warning: {expected_path} 不存在"
              "（hooker 可能被删），仍会写入该条目。")

    old_session_start = (cfg.get("hooks") or {}).get("SessionStart") or []
    new_session_start = _normalize_session_start(old_session_start, _VAULT_ROOT)

    if new_session_start == old_session_start:
        print("[model_proxy hook] SessionStart 已是正确状态，无需改动。")
        return

    stale_paths = []
    for e in old_session_start:
        if not _is_model_proxy_hook(e):
            continue
        for h in e.get("hooks", []) or []:
            cmd = h.get("command") or ""
            if "ensure_model_proxy.sh" in cmd:
                resolved = _resolve_hook_path(cmd, _VAULT_ROOT)
                if resolved != expected_path:
                    stale_paths.append(str(resolved))

    print("[model_proxy hook] 检测 .claude/settings.json 的 SessionStart：")
    if stale_paths:
        print(f"  - 发现 stale 条目 {len(stale_paths)} 条（路径不指向当前安装位置）："
              f"{stale_paths}")
    else:
        print("  - 未发现现有 hook 条目（缺失）")
    print(f"  - 将删除上述条目，并确保末尾存在 1 条指向 {expected_path} 的 hook")

    cfg.setdefault("hooks", {})["SessionStart"] = new_session_start
    new_text = compact_config_json(cfg) + "\n"
    preview_confirm_write(settings_path, old_text, new_text, "model_proxy hook", [
        "SessionStart 已确保存在正确的 model_proxy 自启 hook。",
    ])


# ---------------------------------------------------------------------------
# 各 SDK 安装实现
# ---------------------------------------------------------------------------

def base_url_for(port: str) -> str:
    return f"http://localhost:{port}/"


def _ensure_onboarding_completed(onboarding_path: "Path | None" = None) -> None:
    """确保 ~/.claude.json 含 hasCompletedOnboarding=true，跳过 Claude Code
    官方 onboarding/登录引导。新机器首次 install 后直接可用。

    分支（详见设计文档 ~/.claude.json 各分支处理表）：
    - 文件不存在 → 写入最小文件 {"hasCompletedOnboarding": true}
    - 文件存在 + JSON 可解析 + hasCompletedOnboarding 已为 true → 跳过
    - 文件存在 + JSON 可解析 + hasCompletedOnboarding 为 false 或缺失 → merge 写入 true
    - 文件存在 + JSON 解析失败 → 降级打印手动片段

    onboarding_path 默认 None 时运行时读取模块级 _CLAUDE_ONBOARDING（同
    ensure_session_hook 的默认参数设计，支持 patch 模块常量做测试）。
    """
    if onboarding_path is None:
        onboarding_path = _CLAUDE_ONBOARDING

    if not onboarding_path.exists():
        new_text = json.dumps(
            {"hasCompletedOnboarding": True}, indent=2, ensure_ascii=False
        ) + "\n"
        preview_confirm_write(onboarding_path, "", new_text, "claude(onboarding)", [
            "已写入 ~/.claude.json: hasCompletedOnboarding=true",
            "Claude Code 首次启动将跳过官方 onboarding/登录引导。",
        ])
        return

    old_text = onboarding_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(old_text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [claude(onboarding)] {onboarding_path} 解析失败: {e}")
        print(f"  请手动在 {onboarding_path} 中添加: "
              f'"hasCompletedOnboarding": true')
        return

    if cfg.get("hasCompletedOnboarding") is True:
        print(f"  [claude(onboarding)] hasCompletedOnboarding 已为 true，跳过。")
        return

    if cfg.get("hasCompletedOnboarding") is False:
        print(f"  [claude(onboarding)] 检测到 hasCompletedOnboarding=false，"
              f"将覆盖为 true（install 目的即接入 model_proxy，"
              f"保留 false 会被 onboarding 拦截）。")

    cfg["hasCompletedOnboarding"] = True
    new_text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    preview_confirm_write(onboarding_path, old_text, new_text, "claude(onboarding)", [
        "已写入 ~/.claude.json: hasCompletedOnboarding=true",
        "Claude Code 首次启动将跳过官方 onboarding/登录引导。",
    ])


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

同时在 ~/.claude.json 中添加（跳过官方 onboarding/登录引导）：
  {{"hasCompletedOnboarding": true}}
""")
        return
    old_text = cfg_path.read_text(encoding="utf-8")
    cfg = json.loads(old_text)
    env = cfg.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku")
    env.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet")
    env.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus")
    new_text = compact_config_json(cfg) + "\n"
    preview_confirm_write(cfg_path, old_text, new_text, "claude", [
        f"已写入：ANTHROPIC_BASE_URL={base_url} ANTHROPIC_AUTH_TOKEN={token}",
        "请重启 Claude Code 生效。",
    ])
    _ensure_onboarding_completed()


def _install_catalog_asset(
    template_path: "Path | None" = None,
    target_path: "Path | None" = None,
) -> bool:
    """安装 codex model catalog 到 ~/.codex/model-catalogs/。

    读仓库模板（base_instructions 为占位符 __PROMPT_MD__）→ 从网络拉 codex 官方
    prompt.md 全文 → 替换占位符 → 写入目标文件。

    网络失败时降级：不写 catalog 文件、install_codex 不加 model_catalog_json 行，
    codex 保留 metadata warning 但能用 fallback 正常运行。

    返回 True=装成功/已是最新，False=失败/取消/模板不存在。

    template_path/target_path 默认 None 时运行时读取模块级常量
    （同 ensure_session_hook 的默认参数设计，支持 patch 模块常量做测试）。
    """
    if template_path is None:
        template_path = _CODEX_CATALOG_TEMPLATE
    if target_path is None:
        target_path = _CODEX_CATALOG_TARGET

    # 1. 读仓库模板
    if not template_path.exists():
        print(f"  [codex catalog] Warning: 模板 {template_path} 不存在，跳过 catalog 安装。")
        return False

    template = json.loads(template_path.read_text(encoding="utf-8"))

    # 2. 拉 prompt.md
    try:
        prompt_content = urllib.request.urlopen(
            _PROMPT_MD_URL, timeout=_PROMPT_MD_TIMEOUT
        ).read().decode("utf-8")
    except (urllib.request.URLError, OSError, TimeoutError) as e:
        print(f"  [codex catalog] Warning: 拉取 prompt.md 失败（{e}），"
              f"跳过 catalog 安装，codex 将保留 metadata warning 但能用 fallback 正常运行，"
              f"网络恢复后重跑 install 补上。")
        return False

    # 3. 拼装：替换占位符（base_instructions + instructions_template 两处都替换）
    for m in template.get("models", []):
        m["base_instructions"] = prompt_content
        if m.get("model_messages") and m["model_messages"].get("instructions_template") is not None:
            m["model_messages"]["instructions_template"] = prompt_content

    # 4. 确保目录存在
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 5. 序列化
    new_bytes = json.dumps(template, ensure_ascii=False, indent=2).encode("utf-8")

    # 6. 目标不存在 → confirm → 写入
    if not target_path.exists():
        size_kb = len(new_bytes) / 1024
        print(f"  [codex catalog] 将写入 {target_path}（约 {size_kb:.0f}KB）")
        if not confirm("  确认写入 catalog 文件?"):
            print("  已取消，未写入 catalog 文件。")
            return False
        target_path.write_bytes(new_bytes)
        print(f"  [codex catalog] 已写入 {target_path}")
        return True

    # 7. 目标存在 + bytes 相同 → 跳过
    if target_path.read_bytes() == new_bytes:
        print(f"  [codex catalog] {target_path} 已是最新，跳过。")
        return True

    # 8. 目标存在 + bytes 不同 → confirm → 备份 → 覆盖
    size_kb = len(new_bytes) / 1024
    print(f"  [codex catalog] {target_path} 内容有变更（新文件约 {size_kb:.0f}KB）")
    if not confirm("  确认覆盖 catalog 文件?"):
        print("  已取消，catalog 文件不变。")
        return False

    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bak_path = target_path.with_name(target_path.name + f".bak.{ts}")
    target_path.rename(bak_path)
    target_path.write_bytes(new_bytes)
    print(f"  [codex catalog] 已备份旧文件到 {bak_path}")
    print(f"  [codex catalog] 已写入 {target_path}")
    return True


def _set_top_key_static(text: str, key: str, value: str) -> str:
    """TOML 顶层 key 存在则替换，不存在则在文件头追加。"""
    pattern = re.compile(rf"^{key}\s*=.*$", re.MULTILINE)
    line = f'{key} = "{value}"'
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    return line + "\n" + text


def install_codex(token: str, port: str) -> None:
    """codex-cli：TOML 配置。标准库无 TOML writer，用正则文本拼接，只处理本工具
    自己命名的 [model_providers.model_proxy] 段 + 顶层 model/model_provider 两个 key，
    不触碰其他已存在的 provider 段。

    base_url 拼到 /v1 层级（不含 /responses），wire_api="responses" 由 codex 自己
    拼 /responses 后缀——已对 codex 0.145.0 官方 config-reference 逐字核对确认：
    base_url 含 /v1 不含 /responses（codex 自拼后缀）、wire_api="responses" 是官方
    唯一合法值且默认即此、env_key 走环境变量、experimental_bearer_token 是官方
    dev-only 直填 token 字段。依据来源：learn.chatgpt.com/codex/config-reference
    与 config-sample。
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
        f'experimental_bearer_token = "{token}"\n'
        f'# env_key = "{env_key}"  # 备选：改用环境变量时注释掉上一行 bearer_token、取消此行注释并 export {env_key}="{token}"\n'
    )

    if not cfg_path.exists():
        new_text = (
            f'model = "{model_label}"\n'
            f'model_provider = "{provider_name}"\n'
            f'\n'
            f'{provider_block}'
        )
        wrote = preview_confirm_write(cfg_path, "", new_text, "codex", [
            f"已写入：model_provider={provider_name} base_url={base_url}",
            "已用 experimental_bearer_token 直填（dev-only），免 export；"
            f"若需改走环境变量，见 config 内 env_key 注释",
        ])
        if wrote:
            if _install_catalog_asset():
                cur = cfg_path.read_text(encoding="utf-8")
                cur = _set_top_key_static(cur, _CODEX_CATALOG_TOML_KEY, _CODEX_CATALOG_TOML_VALUE)
                cfg_path.write_text(cur, encoding="utf-8")
                print(f"  已在 config.toml 补写 {_CODEX_CATALOG_TOML_KEY} 行")
        return

    old_text = cfg_path.read_text(encoding="utf-8")
    text = old_text

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
    text = _set_top_key_static(text, "model", model_label)
    text = _set_top_key_static(text, "model_provider", provider_name)

    # catalog 装成功才加/改 model_catalog_json 行；失败则保留现有行不动
    catalog_ok = _install_catalog_asset()
    if catalog_ok:
        text = _set_top_key_static(text, _CODEX_CATALOG_TOML_KEY, _CODEX_CATALOG_TOML_VALUE)
    else:
        if re.search(rf'^{re.escape(_CODEX_CATALOG_TOML_KEY)}\s*=', text, re.MULTILINE):
            print(f"  catalog 装失败，保留现有 {_CODEX_CATALOG_TOML_KEY} 行"
                  f"（指向旧文件，网络恢复后重跑 install 更新）")

    new_text = text

    preview_confirm_write(cfg_path, old_text, new_text, "codex", [
        f"已写入：model_provider={provider_name} base_url={base_url}",
        "已用 experimental_bearer_token 直填（dev-only），免 export；"
        f"若需改走环境变量，见 config 内 env_key 注释",
    ])


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

    old_text = cfg_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(old_text)
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

    models = cfg.setdefault("models", {})
    providers = models.setdefault("providers", {})
    providers[provider_name] = {
        "baseUrl": base_url,
        "apiKey": token,
        "api": api_mode,
    }
    new_text = compact_config_json(cfg) + "\n"
    preview_confirm_write(cfg_path, old_text, new_text, "openclaw", [
        f"已写入：providers.{provider_name}.baseUrl={base_url}",
    ])


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
    ensure_session_hook()
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
