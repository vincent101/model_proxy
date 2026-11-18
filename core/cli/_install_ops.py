#!/usr/bin/env python3
"""model_proxy install helper — 把 client_token 接入各 SDK 的本地配置。

由 model_proxy_cli.sh 的 install 子命令调用。职责：
1. 检测四个 SDK（claude/codex/hermes/openclaw）本机是否已装（配置目录/文件是否存在）。
2. 按协议从 strategies 里过滤出候选 client_token，交互式让用户选择。
3. 已装：备份原配置后按该 SDK 格式写入；未装：打印配置片段供手动粘贴。

红线：只读 strategies，不改 strategies（install 只是让 SDK 指向某个 token，不改
token→route 绑定关系）。纯标准库，不引入第三方依赖（yaml/json5 均不用第三方库）。
"""

import http.client
import json
from contextvars import ContextVar
from contextlib import ExitStack
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse

from core.secure_files import (capture_target, mac_private_protection, secure_replace,
                               SecureSnapshotError, SecureWriteError)
from pathlib import Path
from core import windows_secure_files as win_secure

from core.cli._config_ops import (compact_config_json, confirm, load_config,
                                  _mac_runtime_locations, _windows_runtime_locations,
                                  _windows_write_options, WritePreflightFailed)
from core.cli._format_ops import strategy_route_desc
from core.reasoning.registry import resolve_protocol

# ---------------------------------------------------------------------------
# SessionStart hook（ensure_model_proxy.sh）路径常量
# ---------------------------------------------------------------------------

_MODEL_PROXY_DIR = Path(__file__).resolve().parents[2]


def _recognize_vault(project_root: Path) -> Path | None:
    """Recognize only the explicit vault/tools/model_proxy layout, never ancestors."""
    if project_root.name != "model_proxy" or project_root.parent.name != "tools":
        return None
    vault = project_root.parent.parent
    markers = (vault / ".obsidian", vault / ".claude")
    hook = project_root / "hooker" / (
        "ensure_model_proxy.bat" if os.name == "nt" else "ensure_model_proxy.sh")
    if any(p.is_symlink() or not p.is_dir() for p in markers):
        return None
    if hook.is_symlink() or not hook.is_file():
        return None
    return vault


_VAULT_ROOT = _recognize_vault(_MODEL_PROXY_DIR)
_CLAUDE_SETTINGS = _VAULT_ROOT / ".claude" / "settings.json" if _VAULT_ROOT else None
_HOOK_SCRIPT_REL = "tools/model_proxy/hooker/ensure_model_proxy.sh"
_WINDOWS_HOOK_SCRIPT_REL = "tools/model_proxy/hooker/ensure_model_proxy.bat"

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
                    except ValueError:
                        print("  Warning: supply 协议解析失败，跳过该候选。")
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


def _known_install_secrets(cfg: dict, cands: list[dict]) -> tuple[set[str], bool]:
    """Collect all known credentials, not just those matching this SDK.

    An appkey_file means its value cannot be known without reading another
    file; in that case route details are suppressed rather than guessed.
    """
    secrets = set()
    certain = isinstance(cfg, dict)
    def add(value):
        nonlocal certain
        if value is None or value == '':
            return
        if not isinstance(value, str) or len(value) > 4096:
            certain = False
            return
        secrets.add(value.casefold())
    if not certain:
        return secrets, False
    add(cfg.get('admin_token'))
    strategies = cfg.get('strategies', [])
    supplies = cfg.get('supplies', [])
    if not isinstance(strategies, list) or not isinstance(supplies, list):
        return secrets, False
    for strategy in strategies:
        if not isinstance(strategy, dict):
            certain = False
            continue
        add(strategy.get('client_token'))
    for candidate in cands:
        add(candidate.get('client_token'))
    for supply in supplies:
        if not isinstance(supply, dict):
            certain = False
            continue
        for key in ('appkey', 'app_key', 'api_key', 'apiKey', 'token', 'secret',
                    'authorization', 'bearer_token', 'password'):
            add(supply.get(key))
        url = supply.get('url')
        if url:
            if not isinstance(url, str) or len(url) > 8192:
                certain = False
            else:
                try:
                    parsed = urllib.parse.urlsplit(url)
                    if not parsed.scheme or not parsed.netloc:
                        certain = False
                    add(urllib.parse.unquote(parsed.username, errors='strict') if parsed.username else None)
                    add(urllib.parse.unquote(parsed.password, errors='strict') if parsed.password else None)
                    for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, errors='strict'):
                        add(name)
                        add(value)
                    add(urllib.parse.unquote(parsed.fragment, errors='strict') if parsed.fragment else None)
                except (ValueError, UnicodeError):
                    certain = False
        if supply.get('appkey_file'):
            certain = False
        headers = supply.get('extra_headers', {})
        if headers is None:
            headers = {}
        if not isinstance(headers, dict):
            certain = False
        else:
            for name, value in headers.items():
                # Custom header names and values can both contain credentials.
                add(name)
                add(value)
                if isinstance(value, str) and value.lower().startswith('bearer '):
                    add(value[7:].strip())
    return secrets, certain


def _safe_route_label(value, secrets: set[str], certain: bool) -> str:
    """Show complete safe route labels only; never mask, hash or truncate secrets."""
    hidden = '<线路信息已隐藏>'
    if (not certain or not isinstance(value, str) or not value or len(value) > 160
            or not re.fullmatch(r'[A-Za-z0-9_.:/,+()\[\] -]+', value)):
        return hidden
    folded = value.casefold()
    for secret in secrets:
        # Hide the whole label if it contains a known credential or a
        # substantial contiguous fragment; never print a masked suffix.
        if secret and (secret in folded or folded in secret
                       or any(folded[i:i + 3] in secret for i in range(len(folded) - 2))):
            return hidden
    return value


def choose_token(cfg: dict, protocol: str | None, sdk_label: str) -> str | None:
    cands = candidate_tokens(cfg, protocol)
    if not cands:
        print(f"  [{sdk_label}] 无协议匹配（protocol={protocol}）的 client_token，跳过。"
              f"请先用 strategy add 新增对应协议的绑定。")
        return None
    secrets, certain = _known_install_secrets(cfg, cands)
    labels = [_safe_route_label(c.get('route_desc'), secrets, certain) for c in cands]
    if len(cands) == 1:
        print(f"  [{sdk_label}] 唯一匹配 token: <CLIENT_TOKEN>  线路: {labels[0]}")
        if input("  使用该 token? [Y/n]: ").strip().lower() in ('n', 'no'):
            return None
        return cands[0]['client_token']
    print(f"  [{sdk_label}] 协议匹配的候选 token（备注不显示）:")
    for i, label in enumerate(labels):
        print(f"    [{i}] <CLIENT_TOKEN>  线路: {label}")
    raw = input("  选择序号: ").strip()
    if not raw.isdigit() or not (0 <= int(raw) < len(cands)):
        print("  Error: 无效序号，跳过该 SDK。")
        return None
    if '<线路信息已隐藏>' in labels or len(set(labels)) != len(labels):
        print('  部分线路无法安全区分；请先从安全渠道核对候选顺序。')
        if not confirm('  已核对并确认选择该序号?'):
            return None
    return cands[int(raw)]['client_token']


# ---------------------------------------------------------------------------
# 备份 + 预览确认写入
# ---------------------------------------------------------------------------


def _expected_hook_command() -> str:
    """本工具在 SessionStart 里应该存在的、唯一正确的 command 字面量。"""
    if os.name == 'nt':
        # Claude's Windows hook shell invokes cmd.exe with a single quoted
        # command argument. Keep the expanded project path inside those quotes.
        return f'cmd.exe /d /c "\\"${{CLAUDE_PROJECT_DIR}}/{_WINDOWS_HOOK_SCRIPT_REL}\\""'
    return f'bash "${{CLAUDE_PROJECT_DIR}}/{_HOOK_SCRIPT_REL}"'


def _managed_hook_commands() -> tuple[str, ...]:
    commands = (_expected_hook_command(),)
    if os.name == 'nt':
        # Migrate only our exact previous command; do not touch lookalikes.
        commands += (f'bash "${{CLAUDE_PROJECT_DIR}}/{_HOOK_SCRIPT_REL}"',)
    return commands


def _is_model_proxy_hook(entry: dict) -> bool:
    """Recognize only our exact managed command; unknown lookalikes are preserved."""
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and h.get('type') == 'command'
               and h.get('command') in _managed_hook_commands()
               for h in (entry.get('hooks') or []))


def _resolve_hook_path(command: str, vault_root: Path) -> Path | None:
    if command != _expected_hook_command():
        return None
    return vault_root / (_WINDOWS_HOOK_SCRIPT_REL if os.name == 'nt' else _HOOK_SCRIPT_REL)


def _normalize_session_start(entries: list, vault_root: Path) -> list:
    """Deduplicate managed hooks only when scope and hook options match."""
    result = []
    seen = []
    for entry in entries:
        if not _is_model_proxy_hook(entry):
            result.append(entry)
            continue
        scope = {key: value for key, value in entry.items() if key != 'hooks'}
        kept_hooks = []
        for hook in entry.get('hooks', []):
            managed = (isinstance(hook, dict) and hook.get('type') == 'command'
                       and hook.get('command') in _managed_hook_commands())
            if not managed:
                kept_hooks.append(hook)
                continue
            options = {key: value for key, value in hook.items() if key != 'command'}
            if any(scope == previous_scope and options == previous_options
                   for previous_scope, previous_options in seen):
                continue
            seen.append((scope, options))
            if hook.get('command') != _expected_hook_command():
                hook = dict(hook, command=_expected_hook_command())
            kept_hooks.append(hook)
        if kept_hooks:
            changed = dict(entry)
            changed['hooks'] = kept_hooks
            result.append(changed)
    if not seen:
        result.append({'hooks': [{'type': 'command', 'command': _expected_hook_command()}]})
    return result


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
  HERMES_MODEL_PROXY_KEY=<CLIENT_TOKEN>
""")



def print_manual_snippet(sdk: str, snippet: str) -> None:
    print(f"  [{sdk}] 未检测到配置目录/文件，或不支持自动写入，请手动添加以下配置片段：")
    print("  <CLIENT_TOKEN> 请从原有安全渠道获取。")
    print("  " + "-" * 58)
    for line in snippet.splitlines():
        print(f"  {line}")
    print("  " + "-" * 58)



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
            print("  忽略无效序号。")
            continue
        selected.append(ORDER[int(idx)])
    return selected



# A session only targets the selected user's HOME and, when recognized by the
# explicit layout above, this installation's own vault hook. No ancestor scan.
_MAX_CLIENT_BYTES = 8 * 1024 * 1024
_EVENTS = []
_CANCELLED = False
_WRITE_CONTEXT = ContextVar("model_proxy_install_write_context", default=None)
_COMMIT_GUARD = ContextVar("model_proxy_install_commit_guard", default=None)
_AUDIT_PROBE = object()
if '_AUDIT_READY' not in globals():
    _AUDIT_READY = False


def _commit_audit(event, args):
    # The current secure_replace implementation commits with os.replace or
    # os.link. Audit fires synchronously immediately before these OS calls.
    if event == 'model_proxy.install_guard_probe':
        if args and args[0] is _AUDIT_PROBE:
            _AUDIT_PROBE_STATE[0] = True
        return
    guard = _COMMIT_GUARD.get()
    if guard is not None and event in ('os.rename', 'os.link'):
        from core.cli.dispatch import write_preflight
        if not write_preflight(guard['root'], guard['config'], [guard['target']], guard['pairs'],
                               operation='install'):
            raise PermissionError('install commit preflight failed')
        guard['checks'] += 1


_AUDIT_PROBE_STATE = [False]


def _ensure_commit_audit():
    global _AUDIT_READY
    if _AUDIT_READY:
        return
    if sys.implementation.name != 'cpython':
        raise InstallFailure('提交前路径核验不可用')
    try:
        sys.addaudithook(_commit_audit)
        _AUDIT_PROBE_STATE[0] = False
        sys.audit('model_proxy.install_guard_probe', _AUDIT_PROBE)
    except (RuntimeError, OSError):
        raise InstallFailure('提交前路径核验不可用') from None
    if not _AUDIT_PROBE_STATE[0]:
        raise InstallFailure('提交前路径核验不可用')
    _AUDIT_READY = True


class InstallFailure(RuntimeError):
    pass


def _root_for(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    home = Path.home()
    if not home.is_absolute():
        raise InstallFailure('HOME 不是绝对路径')
    if _VAULT_ROOT is not None and _CLAUDE_SETTINGS is not None and path == _CLAUDE_SETTINGS:
        return _VAULT_ROOT
    try:
        path.relative_to(home)
    except ValueError:
        raise InstallFailure('目标不在授权的 HOME 内') from None
    return home


def _release_windows_snapshot(entry):
    """Release one held read snapshot once; a failed close blocks later capture."""
    if entry['done']:
        return
    entry['done'] = True
    try:
        snap = entry['snapshot']
        if getattr(snap, '_closed', False):
            monitor = getattr(snap, '_monitor', None)
            if (getattr(snap, '_native_release_unknown', False) or
                    getattr(monitor, 'release_unknown', False)):
                raise win_secure.SecureSnapshotError('release_unknown')
        else:
            snap.close()
    except BaseException:
        entry['context']['snapshot_release_unknown'] = True
        raise


def _finish_windows_snapshot(snap):
    if os.name != 'nt':
        return
    context = _WRITE_CONTEXT.get()
    entries = context.get('snapshot_leases', ()) if context is not None else ()
    for entry in entries:
        if entry['snapshot'] is snap:
            _release_windows_snapshot(entry)
            return
    raise InstallFailure('Windows 快照生命周期无法验证')


def _unchanged_snapshot(path, label, snap):
    """An unchanged step no longer needs a lease after its final recheck."""
    try:
        if os.name == 'nt':
            snap.revalidate()
        _record_unchanged_target(path, label)
    finally:
        _finish_windows_snapshot(snap)


def _snapshot_text(path: Path):
    try:
        context = _WRITE_CONTEXT.get()
        if context is None:
            raise InstallFailure('缺少安装写入前置上下文')
        from core.cli.dispatch import write_preflight
        if not write_preflight(context['root'], context['config'], [path], operation='install'):
            raise InstallFailure('运行路径或安装目标冲突')
        if os.name == 'nt':
            stack = context.get('snapshots')
            if stack is None:
                raise InstallFailure('缺少 Windows 快照生命周期')
            entries = context.setdefault('snapshot_leases', [])
            # Do not close an observation that may still authorize an earlier
            # edit; forbid any overlap, including same-target path aliases.
            if context.get('snapshot_release_unknown') or any(not entry['done'] for entry in entries):
                raise win_secure.SecureSnapshotError('prior_snapshot_unreleased')
            from core.discovery import _windows_trusted_boundary, PreflightError
            authorized_root = _root_for(path)
            try:
                boundary = _windows_trusted_boundary(context['root'], str(path))
            except PreflightError:
                raise InstallFailure('安装目标的路径边界无法确认') from None
            # _root_for selects the authorized HOME/vault target. The
            # traversal boundary is not itself a grant to write it.
            snap = win_secure.capture_target(str(path), authorized_root=str(authorized_root),
                                             max_bytes=_MAX_CLIENT_BYTES,
                                             trusted_boundary=boundary)
            entry = {'snapshot': snap, 'context': context, 'done': False}
            entries.append(entry)
            stack.callback(_release_windows_snapshot, entry)
        else:
            snap = capture_target(path, authorized_root=_root_for(path),
                                  protection_reader=mac_private_protection, max_bytes=_MAX_CLIENT_BYTES)
        return snap, (snap.contents or b'').decode('utf-8')
    except (SecureSnapshotError, win_secure.SecureSnapshotError, UnicodeError, InstallFailure) as exc:
        _EVENTS.append((path.name, 'PREPARE_FAILED', None))
        code = getattr(exc, 'code', 'invalid_target')
        raise InstallFailure('目标身份或保护无法验证 (' + code + ')') from None


_SUMMARY_FIELDS = frozenset({
    'model', 'model_provider', 'model_catalog_json',
    'model_providers.model_proxy.base_url', 'model_providers.model_proxy.wire_api',
    'model_providers.model_proxy.experimental_bearer_token',
    'env.ANTHROPIC_BASE_URL', 'env.ANTHROPIC_AUTH_TOKEN',
    'env.ANTHROPIC_DEFAULT_HAIKU_MODEL', 'env.ANTHROPIC_DEFAULT_SONNET_MODEL',
    'env.ANTHROPIC_DEFAULT_OPUS_MODEL', 'hasCompletedOnboarding', 'hooks.SessionStart',
    'models.providers.model_proxy.baseUrl', 'models.providers.model_proxy.apiKey',
    'models.providers.model_proxy.api', 'models[].base_instructions',
    'models[].model_messages.instructions_template',
})


def _changed_json_fields(before: dict, after: dict, paths: tuple[str, ...]) -> list[str]:
    missing = object()
    def get_value(data, name):
        for part in name.split('.'):
            if not isinstance(data, dict) or part not in data:
                return missing
            data = data[part]
        return data
    return [name for name in paths if get_value(before, name) != get_value(after, name)]


def _target_role(label):
    return 'codex config' if label in ('codex', 'codex catalog reference') else label


def _target_pairs(context, target, group):
    pairs = []
    for previous_group, previous_path in context['targets']:
        same_name = os.path.normcase(str(previous_path)) == os.path.normcase(str(target))
        if group == previous_group and same_name:
            continue
        pairs.append((target, previous_path))
    return pairs


def _install_protected_targets(target, pairs):
    """Keep distinct previously authorized roles, without folding aliases."""
    result = [os.fspath(target)]
    for _, prior in pairs:
        value = os.fspath(prior)
        if value not in result:
            result.append(value)
    return tuple(result)


def _mac_install_locations(guard, snapshot, targets):
    from itertools import combinations
    from core.cli import dispatch
    from core.mac_config_path import capture_config

    # Prepare the fixed directories using the business config's retained
    # read-only evidence; this does not authorize the client target.
    business = capture_config(guard['config'])
    baseline = dispatch._config_write_evidence(guard['root'], business, 'install')
    dispatch._prepare_mac_config_runtime(guard['root'], business, 'install', baseline)
    sidecar = os.path.join(os.path.dirname(business.source_path), 'session_overrides.json')
    pairs = list(combinations((*targets, sidecar), 2))

    def preflight(current):
        return (current is snapshot and dispatch.write_preflight(
            guard['root'], guard['config'], targets, pairs, operation='install') is True)

    # A target outside the project may legitimately sit on another volume.
    # The design permits target-parent staging there, but only after the user
    # is told where backup and staging really go and confirms this target.
    from core.secure_files import _same_volume
    allow_target_staging = False
    target_parent = os.path.dirname(os.fspath(guard['target']))
    try:
        stage_path = os.path.join(guard['root'], '.runtime', 'tmp')
        stage_fd = os.open(stage_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            parent_fd = os.open(target_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                cross_volume = not _same_volume(stage_fd, parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.close(stage_fd)
    except OSError:
        raise InstallFailure('无法确认安装目标与暂存目录的卷关系；目标未写入') from None
    if cross_volume:
        print('  安装目标与项目暂存目录跨卷；本次将仅在该目标父目录创建私有暂存，'
              '备份仍写入项目 .runtime/backups。')
        if not confirm('  确认在该目标父目录创建本次暂存?'):
            raise InstallFailure('跨卷暂存未获授权；目标未写入')
        allow_target_staging = True
    locations = _mac_runtime_locations(guard['root'], snapshot, preflight,
                                       allow_target_staging=allow_target_staging)

    def boundary_guard():
        from core.secure_files import capture_private_directory
        if (not preflight(snapshot)
                or capture_private_directory(locations.staging.path, purpose='staging')
                != locations.staging
                or capture_private_directory(locations.backup.path, purpose='backup')
                != locations.backup):
            raise InstallFailure('安装目标、角色或私有目录已改变')
    return locations, boundary_guard


def _record_unchanged_target(path, label):
    context = _WRITE_CONTEXT.get()
    if context is None:
        raise InstallFailure('缺少安装写入前置上下文')
    from core.cli.dispatch import write_preflight
    target = Path(os.path.abspath(path))
    group = _target_role(label)
    if not write_preflight(context['root'], context['config'], [target],
                           _target_pairs(context, target, group), operation='install'):
        _EVENTS.append((label, 'PREPARE_FAILED', None))
        raise InstallFailure('运行路径或安装目标冲突')
    context['targets'].append((group, target))
    _EVENTS.append((label, 'UNCHANGED', None))


def preview_confirm_write(cfg_path: Path, old_text: str, new_text: str, sdk_label: str,
                          success_msg_lines: list[str], *, snapshot=None, field_changes=None) -> bool:
    """Confirm one snapshot, then safely back up, recheck and commit it."""
    global _CANCELLED
    snap, captured = (snapshot, old_text) if snapshot is not None else _snapshot_text(cfg_path)
    try:
        return _preview_confirm_captured(cfg_path, old_text, new_text, sdk_label,
                                         success_msg_lines, snap=snap, captured=captured,
                                         field_changes=field_changes)
    finally:
        _finish_windows_snapshot(snap)


def _preview_confirm_captured(cfg_path, old_text, new_text, sdk_label, success_msg_lines,
                              *, snap, captured, field_changes):
    global _CANCELLED
    # Bind the write-preflight target to the same exact spelling that was
    # captured. Do not equate Windows aliases using case/8.3 heuristics.
    if os.name == 'nt' and getattr(snap, 'path', None) != str(cfg_path):
        _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
        raise InstallFailure('目标与预览快照不一致')
    if captured != old_text or (snap.contents or b'') != old_text.encode('utf-8'):
        _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
        raise InstallFailure('预览与目标快照不一致')
    if old_text == new_text:
        if os.name == 'nt':
            snap.revalidate()
        _record_unchanged_target(cfg_path, sdk_label)
        print(f'  [{sdk_label}] 内容无变化，跳过。')
        return False
    print(f'  [{sdk_label}] 字段变更摘要（所有值已隐藏）:')
    if field_changes is not None and all(field in _SUMMARY_FIELDS for field in field_changes):
        for field in dict.fromkeys(field_changes):
            print(f'  - {field}: 更新（值隐藏）')
        if not field_changes:
            print('  - 配置结构更新（值隐藏）')
    else:
        print('  - 配置字段更新（值隐藏）')
    print('  请在安装期间暂停其他进程对该文件的编辑；最终检查与替换不是原子比较交换。')
    if not confirm('  确认执行以上改动?'):
        _CANCELLED = True
        _EVENTS.append((sdk_label, 'CANCELLED', None))
        print('  已取消，目标未替换。')
        return False
    context = _WRITE_CONTEXT.get()
    if context is None:
        _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
        raise InstallFailure('缺少安装写入前置上下文')
    from core.cli.dispatch import write_preflight
    target = Path(os.path.abspath(cfg_path))
    # Codex deliberately updates the same config twice: initial settings,
    # then the optional catalog reference. Other roles must not alias.
    group = _target_role(sdk_label)
    pairs = _target_pairs(context, target, group)
    if not write_preflight(context['root'], context['config'], [target], pairs, operation='install'):
        _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
        raise InstallFailure('运行路径或安装目标冲突，目标未替换')
    guard = {'root': context['root'], 'config': context['config'],
             'target': target, 'pairs': pairs, 'checks': 0}
    targets = _install_protected_targets(target, pairs)
    try:
        if os.name == 'nt':
            snap.revalidate()
            from core.discovery import PreflightError
            from core.platform_windows import WindowsPrivateCreationError
            try:
                locations = _windows_runtime_locations(guard['root'], guard['config'], 'install',
                                                       None, targets)
            except (PreflightError, WindowsPrivateCreationError, WritePreflightFailed,
                    win_secure.SecureSnapshotError, OSError):
                _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
                raise InstallFailure('运行目录或路径证据不可验证；目标未写入，目录或句柄状态可能未知') from None
            from core.cli._config_ops import _windows_cross_volume
            cross_volume = _windows_cross_volume(guard['root'], snap, locations)
            if cross_volume:
                print('  安装目标与项目暂存目录跨卷；本次将仅在该目标父目录创建私有暂存，'
                      '备份仍写入项目 .runtime/backups。')
                if not confirm('  确认在该目标父目录创建本次暂存?'):
                    _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
                    raise InstallFailure('跨卷暂存未获授权；目标未写入')
            options = _windows_write_options(guard['root'], guard['config'], 'install',
                                             snap, None, locations, make_backup=True,
                                             targets=targets,
                                             allow_target_staging=cross_volume)
            actual_preflight = options['preflight']
            def preflight():
                ok = actual_preflight()
                if ok is True:
                    guard['checks'] += 1
                return ok
            options['preflight'] = preflight
            try:
                preflight()
            except WritePreflightFailed as exc:
                _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
                raise InstallFailure(str(exc)) from None
            except (PreflightError, win_secure.SecureSnapshotError, OSError):
                _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
                raise InstallFailure('安装目标或运行路径无法验证；目标未写入') from None
            outcome = win_secure.write_target(snap, new_text.encode('utf-8'), **options)
        else:
            from core.secure_files import secure_replace_runtime
            from core.discovery import PreflightError
            try:
                locations, boundary_guard = _mac_install_locations(guard, snap, targets)
            except (PreflightError, SecureSnapshotError, WritePreflightFailed, OSError):
                _EVENTS.append((sdk_label, 'PREPARE_FAILED', None))
                raise InstallFailure('运行目录或路径证据不可验证；目标未写入，目录或句柄状态可能未知') from None
            _ensure_commit_audit()
            guard_token = _COMMIT_GUARD.set(guard)
            try:
                outcome = secure_replace_runtime(snap, new_text.encode('utf-8'),
                                                 max_bytes=_MAX_CLIENT_BYTES, locations=locations,
                                                 boundary_guard=boundary_guard)
            finally:
                _COMMIT_GUARD.reset(guard_token)
    except (SecureWriteError, win_secure.SecureWriteError) as exc:
        if isinstance(exc, win_secure.SecureWriteError):
            target_state = exc.target_state
            event_state = ('UNKNOWN_AFTER_TARGET_VERIFIED' if exc.target_was_verified else
                           'VERIFIED_RELEASE_UNKNOWN' if target_state == 'VERIFIED' and
                           exc.release_unknown else target_state)
            _EVENTS.append((sdk_label, event_state, exc.backup))
            if exc.target_was_verified:
                print(f'  [{sdk_label}] 目标曾通过后验，但后续事务状态未知；整体安装未完成。')
            elif target_state == 'VERIFIED':
                print(f'  [{sdk_label}] 目标已替换，后验验证通过；整体安装未完成。')
            elif target_state in ('REPLACED', 'POSTCHECK_FAILED'):
                print(f'  [{sdk_label}] 目标已替换，后验验证失败或未完成；不要继续写入。')
            elif target_state == 'NOT_REPLACED':
                print(f'  [{sdk_label}] 目标未替换。')
            else:
                print(f'  [{sdk_label}] 目标替换状态未知；不要继续写入。')
            print(f'  [{sdk_label}] 备份状态: {exc.backup_status}')
            if exc.release_unknown:
                print(f'  [{sdk_label}] 句柄关闭状态未知；不要继续写入。')
            if exc.creation_unknown:
                print(f'  [{sdk_label}] 候选文件创建状态未知；不要继续写入。')
        elif exc.code == 'parent_close_unknown_after_verified':
            # The write and postcheck succeeded; only releasing the directory
            # handle is unconfirmed. Never report this as a successful install.
            _EVENTS.append((sdk_label, 'VERIFIED_RELEASE_UNKNOWN', exc.backup))
            print(f'  [{sdk_label}] 目标已替换、后验通过、目录句柄关闭状态未知，停止后续写入。')
        else:
            _EVENTS.append((sdk_label, exc.state, exc.backup))
            if exc.state == 'POSTCHECK_FAILED':
                print(f'  [{sdk_label}] 目标已替换，后验验证失败或未完成；不要继续写入。')
            elif exc.state == 'NOT_REPLACED':
                print(f'  [{sdk_label}] 目标未替换。')
            else:
                print(f'  [{sdk_label}] 目标替换状态未知；不要继续写入。')
        if exc.backup:
            print('  备份或残留文件:', exc.backup)
        if exc.staging:
            print('  临时文件清理状态未知:', exc.staging)
        raise InstallFailure('安全提交失败 (' + exc.code + ')') from None
    if not guard['checks']:
        _EVENTS.append((sdk_label, 'POSTCHECK_FAILED', outcome.backup))
        raise InstallFailure('无法确认提交前核验；目标可能已替换，停止后续写入')
    context['targets'].append((group, target))
    _EVENTS.append((sdk_label, outcome.state, outcome.backup))
    if outcome.backup:
        print('  安全备份:', outcome.backup)
    for line in success_msg_lines:
        print('  ' + line)
    return True


def _unchanged_readonly_project_hook(settings_path: Path) -> bool:
    """Allow only an already-correct 0644 macOS project hook without writing."""
    if os.name != 'posix' or sys.platform != 'darwin':
        return False
    import stat
    from core.secure_files import (ProtectionEvidence, _mac_fd_protection, _meta,
                                   target_unchanged)
    try:
        info = os.lstat(settings_path)
    except OSError:
        return False
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_uid != os.geteuid() or info.st_nlink != 1):
        return False

    target = os.path.abspath(settings_path)

    def read_protection(path, observed):
        if path != target:
            return mac_private_protection(path, observed)
        if (not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o644
                or observed.st_uid != os.geteuid() or observed.st_nlink != 1):
            raise SecureSnapshotError('target_changed')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if (_meta(before) != _meta(observed) or
                    getattr(before, 'st_flags', 0) != getattr(observed, 'st_flags', 0)):
                raise SecureSnapshotError('target_changed')
            try:
                _mac_fd_protection(fd, directory=False)
            except SecureSnapshotError as exc:
                # This specific mode rejection occurs after the existing
                # filesystem, ACL, flags and extended-attribute checks.
                if exc.code != 'file_permissions_unsafe':
                    raise
            else:
                raise SecureSnapshotError('read_protection_unverified')
            after = os.fstat(fd)
            if (_meta(before) != _meta(after) or
                    getattr(before, 'st_flags', 0) != getattr(after, 'st_flags', 0)):
                raise SecureSnapshotError('target_changed')
            return ProtectionEvidence(True, (_meta(after), getattr(after, 'st_flags', 0)))
        finally:
            os.close(fd)

    context = _WRITE_CONTEXT.get()
    if context is None:
        raise InstallFailure('缺少安装写入前置上下文')
    from core.cli.dispatch import write_preflight
    if not write_preflight(context['root'], context['config'], [settings_path], operation='install'):
        raise InstallFailure('运行路径或安装目标冲突')
    try:
        snapshot = capture_target(target, authorized_root=str(_root_for(settings_path)),
                                  protection_reader=read_protection, max_bytes=_MAX_CLIENT_BYTES)
        cfg = json.loads((snapshot.contents or b'').decode('utf-8'))
        entries = (cfg.get('hooks') or {}).get('SessionStart') or []
        if not isinstance(entries, list) or entries != _normalize_session_start(entries, _VAULT_ROOT):
            return False
        if not target_unchanged(snapshot, protection_reader=read_protection, max_bytes=_MAX_CLIENT_BYTES):
            raise SecureSnapshotError('target_changed')
    except (SecureSnapshotError, OSError) as exc:
        _EVENTS.append((settings_path.name, 'PREPARE_FAILED', None))
        code = getattr(exc, 'code', 'target_unverified')
        raise InstallFailure('目标身份或保护无法验证 (' + code + ')') from None
    except (UnicodeError, ValueError, TypeError, AttributeError):
        # Invalid or non-matching content still follows the protected writer.
        return False
    _record_unchanged_target(settings_path, 'model_proxy hook')
    print('[model_proxy hook] SessionStart 无需改动。')
    return True


def ensure_session_hook(settings_path: Path | None = None) -> None:
    if _VAULT_ROOT is None or _CLAUDE_SETTINGS is None:
        print('[model_proxy hook] 非标准 vault/tools/model_proxy 布局，跳过 hook；不搜索其他目录。')
        return
    # No caller may turn a hook into an arbitrary file write.
    settings_path = _CLAUDE_SETTINGS if settings_path is None else Path(settings_path)
    if settings_path != _CLAUDE_SETTINGS:
        raise InstallFailure('hook 路径不属于当前安装')
    if os.name == 'nt' and '%' in str(_VAULT_ROOT):
        print('[model_proxy hook] Windows CMD 可能展开路径中的 %；跳过自动配置，'
              '已有 hook 不会修改，请检查或停用。')
        return
    if _unchanged_readonly_project_hook(settings_path):
        return
    snap, old = _snapshot_text(settings_path)
    if not snap.exists:
        try:
            if os.name == 'nt':
                snap.revalidate()
        finally:
            _finish_windows_snapshot(snap)
        print('[model_proxy hook] 当前 vault 未配置 settings.json，跳过。')
        return
    try:
        cfg = json.loads(old)
        entries = (cfg.get('hooks') or {}).get('SessionStart') or []
        if not isinstance(entries, list):
            raise ValueError('invalid hooks')
        updated = _normalize_session_start(entries, _VAULT_ROOT)
        if entries == updated:
            _unchanged_snapshot(settings_path, 'model_proxy hook', snap)
            print('[model_proxy hook] SessionStart 无需改动。')
            return
        cfg.setdefault('hooks', {})['SessionStart'] = updated
        text = compact_config_json(cfg) + '\n'
    except (ValueError, TypeError, AttributeError):
        raise InstallFailure('hook 配置格式无效') from None
    preview_confirm_write(settings_path, old, text, 'model_proxy hook', ['SessionStart 已更新。'],
                          snapshot=snap, field_changes=['hooks.SessionStart'])


def base_url_for(port: str) -> str:
    return f'http://localhost:{port}/'


def _ensure_onboarding_completed(onboarding_path: Path | None = None) -> None:
    path = _CLAUDE_ONBOARDING if onboarding_path is None else Path(onboarding_path)
    snap, old = _snapshot_text(path)
    try:
        cfg = json.loads(old) if snap.exists else {}
        if not isinstance(cfg, dict):
            raise ValueError()
    except ValueError:
        _finish_windows_snapshot(snap)
        print('  [claude onboarding] 配置无法解析；请手工设置 hasCompletedOnboarding。')
        return
    if cfg.get('hasCompletedOnboarding') is True:
        _unchanged_snapshot(path, 'claude onboarding', snap)
        print('  [claude onboarding] 已设置，跳过。')
        return
    cfg['hasCompletedOnboarding'] = True
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + '\n'
    preview_confirm_write(path, old, text, 'claude onboarding', ['onboarding 设置已更新。'],
                          snapshot=snap, field_changes=['hasCompletedOnboarding'])


def install_claude(token: str, port: str) -> None:
    path = SDKS['claude']['config_path']
    if not path.parent.is_dir():
        print_manual_snippet('claude', '请将 ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN=<CLIENT_TOKEN> 添加到客户端设置，并在 ~/.claude.json 设置 hasCompletedOnboarding=true。')
        return
    snap, old = _snapshot_text(path)
    if not snap.exists:
        _finish_windows_snapshot(snap)
        print_manual_snippet('claude', '请将 ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN=<CLIENT_TOKEN> 添加到客户端设置，并在 ~/.claude.json 设置 hasCompletedOnboarding=true。')
        return
    try:
        cfg = json.loads(old)
        import copy
        before = copy.deepcopy(cfg)
        env = cfg.setdefault('env', {})
        env['ANTHROPIC_BASE_URL'] = base_url_for(port)
        env['ANTHROPIC_AUTH_TOKEN'] = token
        for tier in ('HAIKU', 'SONNET', 'OPUS'):
            env.setdefault('ANTHROPIC_DEFAULT_' + tier + '_MODEL', 'claude-' + tier.lower())
        text = compact_config_json(cfg) + '\n'
    except (ValueError, TypeError, AttributeError):
        raise InstallFailure('Claude 客户端配置无效') from None
    fields = _changed_json_fields(before, cfg, ('env.ANTHROPIC_BASE_URL', 'env.ANTHROPIC_AUTH_TOKEN',
                    'env.ANTHROPIC_DEFAULT_HAIKU_MODEL', 'env.ANTHROPIC_DEFAULT_SONNET_MODEL',
                    'env.ANTHROPIC_DEFAULT_OPUS_MODEL'))
    preview_confirm_write(path, old, text, 'claude', ['Claude 客户端配置已更新。'],
                          snapshot=snap, field_changes=fields)
    if not _CANCELLED:
        _ensure_onboarding_completed()


# TOML is validated before and after a small, lossless text edit. Python 3.10
# has no standard-library TOML parser; it must fail closed rather than guess.
def _toml_load(text: str) -> dict:
    try:
        import tomllib as toml_parser
    except ImportError:
        # Optional backport, never installed by the application. Without one,
        # retain fail-closed parsing rather than guessing at unknown TOML.
        try:
            import tomli as toml_parser
        except ImportError:
            raise InstallFailure('安全编辑 Codex TOML 需要 Python 3.11 或可用的 tomli') from None
    try:
        result = toml_parser.loads(text)
        if not isinstance(result, dict):
            raise ValueError('not an object')
        return result
    except (ValueError, TypeError, RecursionError):
        raise InstallFailure('Codex TOML 无法安全解析，未写入') from None


def _toml_single_path(data: dict, *, header: bool) -> tuple[tuple[str, ...], bool]:
    path = []
    array = False
    node = data
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        if not isinstance(key, str):
            raise InstallFailure('Codex TOML 结构无法安全识别')
        path.append(key)
        if isinstance(node, list):
            if not header or array or len(node) != 1 or not isinstance(node[0], dict):
                raise InstallFailure('Codex TOML 结构无法安全识别')
            array = True
            node = node[0]
    if not path or (node != {} if header else type(node) is not int or node != 0):
        raise InstallFailure('Codex TOML 结构无法安全识别')
    return tuple(path), array


def _toml_value_end(text: str, start: int) -> tuple[int, int]:
    """Locate an already-validated TOML value without interpreting its contents."""
    i, n, stack = start, len(text), []
    def trimmed(end):
        while end > start and text[end - 1] in ' \t\r':
            end -= 1
        return end
    quote = None
    multiline = False
    while i < n:
        char = text[i]
        if quote is not None:
            if quote == '"' and char == '\\':
                i += 2
                continue
            if multiline and text.startswith(quote * 3, i):
                j = i
                while j < n and text[j] == quote:
                    j += 1
                if j - i > 5:
                    raise InstallFailure('Codex TOML 字符串边界无法安全识别')
                i = j
                quote = None
                multiline = False
                continue
            if not multiline and char == quote:
                quote = None
            i += 1
            continue
        if char in ('"', "'"):
            quote = char
            multiline = text.startswith(char * 3, i)
            i += 3 if multiline else 1
            continue
        if char in '[{':
            stack.append(char)
            i += 1
            continue
        if char in ']}':
            if not stack or (stack.pop(), char) not in (('[', ']'), ('{', '}')):
                raise InstallFailure('Codex TOML 容器边界无法安全识别')
            i += 1
            continue
        if char == '#':
            end = text.find('\n', i)
            if not stack:
                value_end = trimmed(i)
                return value_end, n if end < 0 else end + 1
            if end < 0:
                raise InstallFailure('Codex TOML 容器边界无法安全识别')
            i = end + 1
            continue
        if char == '\n' and not stack:
            return trimmed(i), i + 1
        i += 1
    if quote is not None or stack:
        raise InstallFailure('Codex TOML 值边界无法安全识别')
    return trimmed(n), n


def _toml_statements(text: str) -> list[dict]:
    """Read statements and table context; the complete document is validated separately."""
    result = []
    i, n = 0, len(text)
    table, table_array = (), False
    while i < n:
        line_start = i
        while i < n and text[i] in ' \t\r':
            i += 1
        if i == n:
            break
        if text[i] == '\n':
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = n if end < 0 else end + 1
            continue
        if text[i] == '[':
            end = text.find('\n', i)
            line_end = n if end < 0 else end
            header = text[i:line_end].rstrip('\r')
            table, table_array = _toml_single_path(_toml_load(header), header=True)
            result.append({'kind': 'header', 'start': line_start, 'table': table, 'array': table_array})
            i = n if end < 0 else end + 1
            continue
        key_start = i
        key_quote = None
        while i < n:
            char = text[i]
            if key_quote:
                if key_quote == '"' and char == '\\':
                    i += 2
                    continue
                if char == key_quote:
                    key_quote = None
            elif char in ('"', "'"):
                key_quote = char
            elif char == '=':
                break
            elif char in '#\n':
                raise InstallFailure('Codex TOML 键无法安全识别')
            i += 1
        if i >= n or key_quote:
            raise InstallFailure('Codex TOML 键无法安全识别')
        key_text = text[key_start:i].strip()
        key, _ = _toml_single_path(_toml_load(key_text + ' = 0'), header=False)
        i += 1
        while i < n and text[i] in ' \t':
            i += 1
        start = i
        end, i = _toml_value_end(text, start)
        if end <= start:
            raise InstallFailure('Codex TOML 值无法安全识别')
        result.append({'kind': 'assignment', 'table': table, 'array': table_array,
                       'key': key, 'value_start': start, 'value_end': end})
    return result


def _toml_semantic_equal(left, right) -> bool:
    import math
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_toml_semantic_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_toml_semantic_equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _rewrite_codex_toml(text: str, *, top: dict[str, str], provider: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Modify only specified root/provider fields; preserve all other text and semantics."""
    import copy
    original = _toml_load(text)
    if (provider is not None and _CODEX_CATALOG_TOML_KEY in original
            and not isinstance(original[_CODEX_CATALOG_TOML_KEY], str)):
        raise InstallFailure('Codex TOML catalog 字段类型不支持，未写入')
    statements = _toml_statements(text)
    newline = '\r\n' if text.count('\n') and text.count('\n') == text.count('\r\n') else '\n'
    expected = copy.deepcopy(original)
    changed = []
    for key, value in top.items():
        if key in original and not isinstance(original[key], str):
            raise InstallFailure('Codex TOML 目标字段类型不支持，未写入')
        if original.get(key) != value:
            changed.append(key)
        expected[key] = value
    if provider is not None:
        providers = expected.setdefault('model_providers', {})
        if not isinstance(providers, dict):
            raise InstallFailure('Codex TOML provider 结构不支持，未写入')
        current = providers.setdefault('model_proxy', {})
        if not isinstance(current, dict):
            raise InstallFailure('Codex TOML provider 结构不支持，未写入')
        for key, value in provider.items():
            if key in current and not isinstance(current[key], str):
                raise InstallFailure('Codex TOML provider 字段类型不支持，未写入')
            if current.get(key) != value:
                changed.append('model_providers.model_proxy.' + key)
            current[key] = value
    headers = [st for st in statements if st['kind'] == 'header']
    provider_headers = [st for st in headers if st['table'] == ('model_providers', 'model_proxy')]
    if provider_headers and (len(provider_headers) != 1 or provider_headers[0]['array']):
        raise InstallFailure('Codex TOML provider 表无法安全识别，未写入')
    matches = {}
    for st in statements:
        if st['kind'] != 'assignment' or st['array'] or len(st['key']) != 1:
            continue
        key = st['key'][0]
        if st['table'] == () and key in top:
            matches[('top', key)] = st
        if provider is not None and st['table'] == ('model_providers', 'model_proxy') and key in provider:
            matches[('provider', key)] = st
    edits = []
    def encode(value):
        return json.dumps(value, ensure_ascii=True)
    for key, value in top.items():
        st = matches.get(('top', key))
        if st and original.get(key) != value:
            edits.append((st['value_start'], st['value_end'], encode(value), 0))
    missing_top = [key for key in top if ('top', key) not in matches]
    if missing_top:
        # A root value represented by a dotted key cannot be safely relocated.
        if any(key in original for key in missing_top):
            raise InstallFailure('Codex TOML 顶层字段无法安全定位，未写入')
        pos = headers[0]['start'] if headers else len(text)
        prefix = '' if pos == 0 or text[pos - 1] == '\n' else newline
        edits.append((pos, pos, prefix + ''.join(key + ' = ' + encode(top[key]) + newline for key in missing_top), 0))
    if provider is not None:
        for key, value in provider.items():
            st = matches.get(('provider', key))
            old_provider = original.get('model_providers', {}).get('model_proxy', {})
            if st and old_provider.get(key) != value:
                edits.append((st['value_start'], st['value_end'], encode(value), 0))
        missing_provider = [key for key in provider if ('provider', key) not in matches]
        if provider_headers:
            header = provider_headers[0]
            later = [st['start'] for st in headers if st['start'] > header['start']]
            pos = min(later) if later else len(text)
            prefix = '' if pos == 0 or text[pos - 1] == '\n' else newline
            if missing_provider:
                edits.append((pos, pos, prefix + ''.join(key + ' = ' + encode(provider[key]) + newline for key in missing_provider), 1))
        else:
            if original.get('model_providers', {}).get('model_proxy') is not None:
                raise InstallFailure('Codex TOML provider 未使用独立表，无法安全编辑')
            pos = len(text)
            prefix = '' if pos == 0 or text[pos - 1] == '\n' else newline
            edits.append((pos, pos, prefix + '[model_providers.model_proxy]' + newline + ''.join(
                key + ' = ' + encode(value) + newline for key, value in provider.items())
                + '# env_key = "MODEL_PROXY_CODEX_TOKEN"  # 可选：使用环境变量' + newline, 1))
    # Multiple insertions at EOF need deterministic order: top level precedes provider.
    for start, end, replacement, _ in sorted(edits, key=lambda edit: (edit[0], edit[1], edit[3]), reverse=True):
        text = text[:start] + replacement + text[end:]
    if not _toml_semantic_equal(_toml_load(text), expected):
        raise InstallFailure('Codex TOML 编辑前后结构不一致，未写入')
    return text, changed


def _set_top_key_static(text: str, key: str, value: str) -> str:
    if key not in ('model', 'model_provider', _CODEX_CATALOG_TOML_KEY):
        raise InstallFailure('Codex TOML 目标字段不支持')
    return _rewrite_codex_toml(text, top={key: value})[0]


def _windows_catalog_directory_preflight(context, directory, target):
    """Preflight a missing directory without treating it as a file target.

    The ordinary write preflight rejects a file with an unobserved parent.
    Reuse the same held Windows role leases and containment comparison, while
    checking the first absent component as a directory. The final file is
    preflighted again by the normal install path after directory creation.
    """
    import ntpath
    from pathlib import PureWindowsPath
    from core import discovery, windows_paths
    from core.cli.dispatch import runtime_paths, write_preflight
    root = context['root']
    config = ntpath.abspath(os.fspath(context['config']))
    directory = PureWindowsPath(directory)
    target = PureWindowsPath(target)
    if target.parent != directory:
        return False
    held = []
    monitor = None
    try:
        if not write_preflight(root, config, operation='install'):
            # The existing bool API emits its own diagnostic but cannot pass
            # its error code back. Keep the meaning of release_unknown clear.
            raise InstallFailure('catalog 基础预检失败；如前述为 release_unknown，句柄关闭状态未知')
        paths = runtime_paths(root)
        _, roles = discovery._windows_open_preflight(root, paths)
        held.extend(roles.values())
        monitor = roles['__root__']._backend
        # The selected catalog parent exists; the catalog directory itself
        # does not. The boundary scopes traversal, it grants no write access.
        directory_boundary = discovery._windows_trusted_boundary(root, str(directory))
        directory_lease = windows_paths.inspect(str(directory), backend=monitor,
                                                 kind='directory', private=True,
                                                 trusted_boundary=directory_boundary)
        held.append(directory_lease)
        target_lease = windows_paths.inspect(str(target), backend=monitor,
                                              kind='file', private=True,
                                              trusted_boundary=directory_boundary)
        held.append(target_lease)
        config_lease = windows_paths.inspect(
            config, backend=monitor, kind='file', private=True,
            trusted_boundary=discovery._windows_trusted_boundary(root, config))
        held.append(config_lease)
        paths_file = str(PureWindowsPath(root) / 'config' / 'runtime_paths.json')
        paths_lease = windows_paths.inspect(paths_file, backend=monitor, kind='file', private=True,
                                             trusted_boundary=os.fspath(root))
        held.append(paths_lease)
        other_paths = [str(PureWindowsPath(config).parent / 'session_overrides.json')]
        other_paths.extend(os.fspath(path) for _, path in context.get('targets', ()))
        other_leases = []
        for path in other_paths:
            lease = windows_paths.inspect(
                path, backend=monitor, kind='file', private=True,
                trusted_boundary=discovery._windows_trusted_boundary(root, path))
            held.append(lease)
            other_leases.append(lease)
        de, te = directory_lease.evidence, target_lease.evidence
        if (de.exists or de.missing != (directory.name,) or
                te.missing != (directory.name, target.name) or
                de.components[-1].creation_kind != 'directory' or
                te.components[-1].creation_kind != 'directory'):
            return False
        for lease in held:
            lease.revalidate()
        protected = [lease for name, lease in roles.items() if name != '__root__']
        protected.extend((config_lease, paths_lease, *other_leases))
        # Comparing the directory in both directions also covers any target
        # nested underneath it, including the future catalog file itself.
        for lease in protected:
            if (discovery._windows_evidence_contains(de, lease.evidence) or
                    discovery._windows_evidence_contains(lease.evidence, de)):
                return False
        for lease in held:
            lease.revalidate()
        return not monitor.release_unknown
    except InstallFailure:
        raise
    except Exception as exc:
        if (getattr(exc, 'code', None) in ('release_unknown', 'close_failed',
                                            'resource_release_failed', 'handle_close_failed')
                or getattr(exc, 'release_unknown', False) is True
                or (monitor is not None and monitor.release_unknown)):
            raise InstallFailure('catalog 路径检查句柄关闭状态未知') from None
        return False
    finally:
        if held:
            try:
                discovery._windows_close_leases(held)
            except Exception:
                raise InstallFailure('catalog 路径检查句柄关闭状态未知') from None


def _create_catalog_directory(directory: Path, *, target: Path | None = None) -> None:
    """Create only the missing ~/.codex/model-catalogs directory.

    Existing directories/ACLs are never repaired. A failed verification may
    leave the newly created empty directory; it has received no client data.
    """
    directory = Path(os.path.abspath(directory))
    home = Path(os.path.abspath(Path.home()))
    if directory != home / '.codex' / 'model-catalogs':
        raise InstallFailure('catalog 目录不在授权的 HOME 范围')
    context = _WRITE_CONTEXT.get()
    if context is None:
        raise InstallFailure('缺少安装写入前置上下文')
    from core.cli.dispatch import write_preflight
    if os.name == 'nt':
        from core.platform_windows import ensure_private_directory, WindowsPrivateCreationError
        from core.discovery import _windows_trusted_boundary, PreflightError
        target = target or directory / 'model_proxy_catalog.json'
        if not _windows_catalog_directory_preflight(context, directory, target):
            raise InstallFailure('catalog 目录与运行路径或配置冲突或权限不可验证')
        try:
            # Creating the catalog file also requires FILE_ADD_FILE on its parent.
            # HOME selection and the checks above authorize the operation;
            # this boundary only limits traversal/ACL policy.
            boundary = _windows_trusted_boundary(context['root'], str(directory))
            ensure_private_directory(str(directory), required_access=0x200A3,
                                     trusted_boundary=boundary)
        except PreflightError:
            raise InstallFailure('catalog 目录的路径边界无法确认；目录未创建') from None
        except WindowsPrivateCreationError as exc:
            state = ('空目录可能已创建' if exc.created is True else
                     '目录创建状态未知' if exc.created is None else '目录未创建')
            if exc.release_unknown:
                state += '；句柄关闭状态未知'
            raise InstallFailure('catalog 目录创建或保护无法验证 (' + exc.code + ')；' + state) from None
        return
    if sys.platform != 'darwin' or os.name != 'posix':
        raise InstallFailure('目录创建保护不可验证')
    import stat
    from core.secure_files import _parents
    if not write_preflight(context['root'], context['config'], operation='install'):
        raise InstallFailure('运行路径或安装目标冲突')
    parent = str(directory.parent)
    fd = child = None
    created = False
    try:
        before = _parents(parent, mac_private_protection)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) != before[-1][1]:
            raise InstallFailure('catalog 父目录已改变')
        if _parents(parent, mac_private_protection) != before:
            raise InstallFailure('catalog 父目录保护已改变')
        # Restrict only this newly created directory; the caller's umask can
        # remove additional rights. Existing directories are never repaired.
        os.mkdir(directory.name, 0o755, dir_fd=fd)
        created = True
        child = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=fd)
        info = os.fstat(child)
        current = os.stat(directory.name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid) !=
                (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid)):
            raise InstallFailure('新 catalog 目录身份或权限无法验证')
        proof = mac_private_protection(str(directory), info)
        if not proof.secure or _parents(parent, mac_private_protection) != before:
            raise InstallFailure('新 catalog 目录保护无法验证')
    except InstallFailure as exc:
        if created:
            raise InstallFailure(str(exc) + '；空目录可能已创建') from None
        raise
    except (OSError, SecureSnapshotError) as exc:
        reason = ('；目录或祖先对组或其他用户可写'
                  if getattr(exc, 'code', None) == 'parent_writable' else '')
        raise InstallFailure('catalog 目录创建或保护无法验证' + reason +
                             ('；空目录可能已创建' if created else '')) from exc
    finally:
        close_unknown = False
        for handle in (child, fd):
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    close_unknown = True
        if close_unknown:
            raise InstallFailure('catalog 目录句柄关闭状态未知' +
                                 ('；空目录可能已创建' if created else '')) from None


_CATALOG_NOT_PREPARED = object()


def _prepare_catalog_asset(template_path: Path | None = None) -> str | None:
    """Prepare the optional catalog in memory, without client credentials."""
    template_path = template_path or _CODEX_CATALOG_TEMPLATE
    if not template_path.is_file():
        print('  [codex catalog] 模板不存在或目标目录不可用；保留现有 catalog 配置。')
        _EVENTS.append(('codex catalog', 'SKIPPED', None))
        return None
    try:
        template = json.loads(template_path.read_text(encoding='utf-8'))
        if not isinstance(template, dict):
            raise ValueError('invalid catalog template')
        models = template.get('models', [])
        if not isinstance(models, list):
            raise ValueError('invalid catalog models')
        for model in models:
            if not isinstance(model, dict):
                raise ValueError('invalid catalog model')
            messages = model.get('model_messages')
            if messages is not None and not isinstance(messages, dict):
                raise ValueError('invalid catalog model')
        # Fixed public URL only: no client token or configuration is passed.
        prompt = urllib.request.urlopen(_PROMPT_MD_URL, timeout=_PROMPT_MD_TIMEOUT).read().decode('utf-8')
        for model in models:
            model['base_instructions'] = prompt
            if (model.get('model_messages') or {}).get('instructions_template') is not None:
                model['model_messages']['instructions_template'] = prompt
        return json.dumps(template, ensure_ascii=False, indent=2)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, http.client.IncompleteRead):
        print('  [codex catalog] 拉取 prompt.md 失败或模板不可用；保留现有 catalog 配置。')
        _EVENTS.append(('codex catalog', 'SKIPPED', None))
        return None


def _install_catalog_asset(template_path: Path | None = None, target_path: Path | None = None,
                           *, prepared=_CATALOG_NOT_PREPARED) -> bool:
    """Install prepared content, or prepare it once for direct callers."""
    target_path = target_path or _CODEX_CATALOG_TARGET
    if prepared is _CATALOG_NOT_PREPARED:
        prepared = _prepare_catalog_asset(template_path)
    if prepared is None:
        return False
    if not isinstance(prepared, str):
        raise InstallFailure('catalog 内容不可用')
    text = prepared
    if not target_path.parent.is_dir():
        _create_catalog_directory(target_path.parent, target=target_path)
    snap, old = _snapshot_text(target_path)
    if old == text:
        _unchanged_snapshot(target_path, 'codex catalog', snap)
        return True
    wrote = preview_confirm_write(target_path, old, text, 'codex catalog', ['catalog 已更新。'],
                                  snapshot=snap, field_changes=['models[].base_instructions',
                                  'models[].model_messages.instructions_template'])
    return wrote


def _preflight_existing_catalog() -> None:
    """Check an existing catalog target before changing the Codex config."""
    target = Path(os.path.abspath(_CODEX_CATALOG_TARGET))
    if not os.path.lexists(target.parent):
        return  # Missing directory retains the existing creation workflow.
    if not target.parent.is_dir():
        raise InstallFailure('catalog 目录不可用；Codex 配置未修改')
    try:
        snap, _ = _snapshot_text(target)
        try:
            if os.name == 'nt':
                snap.revalidate()
            else:
                from core.secure_files import target_unchanged
                if not target_unchanged(snap, protection_reader=mac_private_protection,
                                        max_bytes=_MAX_CLIENT_BYTES):
                    raise SecureSnapshotError('target_changed')
            context = _WRITE_CONTEXT.get()
            from core.cli.dispatch import write_preflight
            pairs = _target_pairs(context, target, _target_role('codex catalog'))
            if not write_preflight(context['root'], context['config'], [target], pairs,
                                   operation='install'):
                raise InstallFailure('catalog 路径冲突')
        finally:
            _finish_windows_snapshot(snap)
    except (InstallFailure, SecureSnapshotError, win_secure.SecureSnapshotError, OSError) as exc:
        code = getattr(exc, 'code', '')
        detail = f' ({code})' if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]+', code) else ''
        raise InstallFailure('catalog 目录或文件预检失败' + detail +
                             '；请检查权限与 ACL；Codex 配置未修改') from None


def install_codex(token: str, port: str) -> None:
    path = SDKS['codex']['config_path']
    if not path.parent.is_dir():
        print_manual_snippet('codex', '请在客户端设置 model_provider=model_proxy、base_url，以及 experimental_bearer_token="<CLIENT_TOKEN>"。')
        return
    if not isinstance(token, str):
        raise InstallFailure('客户端 token 类型无效')
    # User allows the existing public GET before confirmation. Preparation
    # stays in memory; a failed optional catalog must not block Codex setup.
    prepared_catalog = _prepare_catalog_asset()
    if prepared_catalog is not None:
        _preflight_existing_catalog()
    snap, old = _snapshot_text(path)
    base_url = base_url_for(port).rstrip('/') + '/v1'
    text, changed = _rewrite_codex_toml(old, top={'model': 'claude-sonnet', 'model_provider': 'model_proxy'},
                                        provider={'base_url': base_url, 'wire_api': 'responses',
                                                  'experimental_bearer_token': token})
    preview_confirm_write(path, old, text, 'codex', ['Codex 客户端配置已更新。'],
                          snapshot=snap, field_changes=changed)
    if _CANCELLED:
        return
    try:
        catalog_ok = _install_catalog_asset(prepared=prepared_catalog)
    except InstallFailure:
        if any(label == 'codex' and state == 'VERIFIED' for label, state, _ in _EVENTS):
            print('  [codex] 主配置已保存并验证；catalog 未完成，停止后续写入。')
        raise
    if catalog_ok:
        snap2, current = _snapshot_text(path)
        updated, fields = _rewrite_codex_toml(current, top={_CODEX_CATALOG_TOML_KEY: _CODEX_CATALOG_TOML_VALUE})
        preview_confirm_write(path, current, updated, 'codex catalog reference', ['catalog 引用已更新。'],
                              snapshot=snap2, field_changes=fields)
    elif _CODEX_CATALOG_TOML_KEY in _toml_load(text):
        print('  catalog 不可用，保留现有 model_catalog_json 引用。')


def install_openclaw(token: str, port: str) -> None:
    path = SDKS['openclaw']['config_path']
    if not path.parent.is_dir():
        print_manual_snippet('openclaw', '请手工设置 model_proxy 的 baseUrl、apiKey=<CLIENT_TOKEN> 和 api。')
        return
    snap, old = _snapshot_text(path)
    if not snap.exists:
        _finish_windows_snapshot(snap)
        print_manual_snippet('openclaw', '请手工设置 model_proxy 的 baseUrl、apiKey=<CLIENT_TOKEN> 和 api。')
        return
    try:
        cfg = json.loads(old)
        import copy
        before = copy.deepcopy(cfg)
        providers = cfg.setdefault('models', {}).setdefault('providers', {})
        providers['model_proxy'] = {'baseUrl': base_url_for(port), 'apiKey': token, 'api': 'anthropic'}
        text = compact_config_json(cfg) + '\n'
    except (ValueError, TypeError, AttributeError):
        _finish_windows_snapshot(snap)
        print_manual_snippet('openclaw', '客户端配置不是标准 JSON；请手工设置 apiKey=<CLIENT_TOKEN>。')
        return
    fields = _changed_json_fields(before, cfg, ('models.providers.model_proxy.baseUrl',
                    'models.providers.model_proxy.apiKey', 'models.providers.model_proxy.api'))
    preview_confirm_write(path, old, text, 'openclaw', ['OpenClaw 客户端配置已更新。'],
                          snapshot=snap, field_changes=fields)


_INSTALLERS = {'claude': install_claude, 'codex': install_codex,
               'hermes': install_hermes, 'openclaw': install_openclaw}


def cmd_install(config_path: str, port: str, selected: list[str], *, preflight_root=None) -> int:
    global _CANCELLED
    _EVENTS.clear()
    _CANCELLED = False
    root = Path(preflight_root) if preflight_root is not None else _MODEL_PROXY_DIR
    context_token = _WRITE_CONTEXT.set({'root': root, 'config': config_path, 'targets': []})
    try:
        with ExitStack() as snapshots:
            _WRITE_CONTEXT.get()['snapshots'] = snapshots
            from core.cli.dispatch import write_preflight
            if not write_preflight(root, config_path, operation='install'):
                raise InstallFailure('运行路径或配置不可用')
            if not str(port).isascii() or not str(port).isdigit() or not 1 <= int(port) <= 65535:
                raise InstallFailure('端口无效')
            cfg = load_config(config_path)
            if not selected:
                selected = _interactive_select()
            if not selected:
                print('未选择任何 SDK，退出。')
                return 0
            if any(name not in _INSTALLERS for name in selected):
                raise InstallFailure('未知 SDK')
            ensure_session_hook()
            if _CANCELLED:
                return 0
            for name in selected:
                meta = SDKS[name]
                print(f'\n== 安装 {meta["label"]} ==')
                token = choose_token(cfg, meta['protocol'], meta['label'])
                if token is None:
                    continue
                _INSTALLERS[name](token, port)
                if _CANCELLED:
                    break
            return 0
    except InstallFailure as exc:
        print(f'Error: 安装未完成（{exc}）；停止后续写入。', file=sys.stderr)
        return 1
    except (SecureSnapshotError, win_secure.SecureSnapshotError, OSError, ValueError, TypeError):
        print('Error: 安装未完成或快照释放状态未知；停止后续写入。', file=sys.stderr)
        return 1
    finally:
        _WRITE_CONTEXT.reset(context_token)
        if _EVENTS:
            print('安装结果（逐文件；未列出的目标未执行）:')
            for label, state, backup in _EVENTS:
                print(f'  {label}: {state}' + (f' 备份/残留: {backup}' if backup else ''))


def main() -> int:
    if len(sys.argv) < 4:
        print('用法: _install_ops.py list|install <config_file> <port> [sdk1,sdk2,...]', file=sys.stderr)
        return 1
    action, config_path, port = sys.argv[1:4]
    try:
        if action == 'list':
            cmd_list(config_path, port)
            return 0
        if action == 'install':
            raw = sys.argv[4] if len(sys.argv) > 4 else ''
            return cmd_install(config_path, port, [s.strip() for s in raw.split(',') if s.strip()])
        print('Error: 未知 action', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n已取消。')
        return 130
    except (OSError, ValueError, TypeError):
        print('Error: 安装失败。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
