"""Session 身份只读解析（CC 注册表 + Codex rollout），把 UUID 翻成可读 name。

status CLI（_format_ops.py）与代理 server 层（$route 回执）共用，展示形态
`notevault-44 · b6ceb46d`。两个来源：
- Claude Code：`~/.claude/sessions/*.json`（pid 注册表，含 name，权威）
- Codex：`~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl` 首行的 session_meta.payload
  （只带 session_id + cwd，名字由本模块派生）

设计约束：
- 只读：绝不写这些目录，不做任何持久化
- 按次扫描不缓存：CC 侧规模是活跃进程数（个位/十位数）；Codex 侧是历史累积
  （本机数百个 rollout），故按 mtime 只解析最近 _CODEX_MAX_SCAN 个文件，保持
  单次扫描在毫秒级。缓存反而引入失效问题
- 不进 runtime_paths.json：其职责是代理自产运行文件的路径，这些是外部状态
- 容错：坏 JSON/缺目录/缺字段一律跳过；未命中由调用方回退仅显示 UUID8

已知边界（可接受，不影响正确性）：
- Codex 派生名可能碰撞：同 cwd + uuid8 后两位相同（约 1/256）时两个会话显示同名，
  但 status 行首仍带完整 uuid8，可区分
- _CODEX_MAX_SCAN 计的是 rollout 文件数而非会话数；子 agent 与父会话共享
  session_id，会额外消耗窗口。极端并发下活跃会话可能落到窗口外 → 退回纯 uuid8
"""

import json
from datetime import datetime
from pathlib import Path

# 注册表默认位置。函数体内按调用时取值，测试可 patch 本属性注入临时目录。
DEFAULT_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Codex 不写 CC 那种 pid 注册表；会话元数据只在 rollout 首行。两源合并使用：
# CC 注册表是权威名，Codex 派生名只做补位（status / $route 展示）。
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_CODEX_ROLLOUT_GLOB = "*/*/*/rollout-*.jsonl"
_CODEX_HEAD_LIMIT = 256 * 1024     # 首行是 session_meta；本机实测 <25KB，留足余量
_CODEX_MAX_SCAN = 120              # rollout 是历史累积，按 mtime 只扫最近的

# procStart 为 time.ctime() 文本格式（"Mon Aug 24 03:42:10 2026"）。
# strptime 的 %d 可匹配空格补齐的日（"Aug  4"）；locale 非英文解析失败时
# 回退 startedAt（epoch ms）。
_PROC_START_FMT = "%a %b %d %H:%M:%S %Y"


def _proc_start_key(data: dict) -> datetime | None:
    """同 UUID 多文件（同 session 多进程快照）时取最新的排序键。

    procStart 优先；缺失/不可解析回退 startedAt（epoch ms）。两者皆无 → None。
    """
    ps = data.get("procStart")
    if isinstance(ps, str):
        try:
            return datetime.strptime(ps, _PROC_START_FMT)
        except ValueError:
            pass
    sa = data.get("startedAt")
    if isinstance(sa, (int, float)) and not isinstance(sa, bool):
        return datetime.fromtimestamp(sa / 1000.0)
    return None


def load_session_names(sessions_dir=None) -> dict[str, str]:
    """扫描注册表目录一次，返回 {sessionId: name}。

    容错：目录缺失/不可读、单文件 JSON 损坏、结构非 dict、sessionId/name
    缺失或空 → 跳过该文件，不影响其余。同 UUID 多文件取 procStart 最新者；
    都无法排序时取文件名序靠后者（注册表按 pid 命名，无稳定语义，任取其一）。
    """
    d = Path(sessions_dir) if sessions_dir is not None else DEFAULT_SESSIONS_DIR
    names: dict[str, str] = {}
    keys: dict[str, datetime | None] = {}
    try:
        entries = sorted(d.glob("*.json"))
    except OSError:
        return {}
    for p in entries:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        name = data.get("name")
        if not (isinstance(sid, str) and sid and isinstance(name, str) and name):
            continue
        key = _proc_start_key(data)
        if sid in keys:
            prev = keys[sid]
            # 已有可排序快照时只有更新的才顶替；旧快照无排序键时无条件顶替。
            if prev is not None and (key is None or key <= prev):
                continue
        keys[sid] = key
        names[sid] = name
    return names


def match_session_name(names: dict[str, str], session_id) -> str | None:
    """在已加载映射上匹配 name：完整 UUID 精确命中，或 uuid8 前缀唯一命中。

    前缀命中多个不同 UUID（歧义）、未命中、输入非非空字符串 → None（宁缺勿错）。
    """
    if not isinstance(session_id, str) or not session_id or not names:
        return None
    if session_id in names:
        return names[session_id]
    if len(session_id) == 8:
        matches = [v for k, v in names.items() if k.startswith(session_id)]
        if len(matches) == 1:
            return matches[0]
    return None


def session_display_id(names: dict[str, str], session_id: str) -> str:
    """status 行首 id 展示：`name · uuid8`（命中）/ `uuid8`（未命中）。

    "(none)" 等 uuid 以外的占位串原样截断传递（长度 <8 不受损）。
    """
    sid = session_id or ""
    name = match_session_name(names, sid)
    return f"{name} · {sid[:8]}" if name else sid[:8]


def _basename_any(path: str) -> str:
    """取路径最后一段，兼容 POSIX 与 Windows 分隔符（cwd 可能来自另一平台）。"""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _codex_session_meta(path: Path) -> tuple[str, str] | None:
    """读 rollout 首行，取 (session_id, cwd)；任何异常或字段缺失 → None。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            line = f.readline(_CODEX_HEAD_LIMIT)
    except (OSError, UnicodeDecodeError):
        return None
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return None
    sid, cwd = payload.get("session_id"), payload.get("cwd")
    if not (isinstance(sid, str) and sid and isinstance(cwd, str) and cwd):
        return None
    return sid, cwd


def load_codex_session_names(sessions_dir=None, *, max_scan: int = _CODEX_MAX_SCAN) -> dict[str, str]:
    """Codex rollout → {sessionId: name}，名字形态与 CC 对齐。

    Codex 侧唯一稳定的会话元数据是 `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl`
    的首行（`session_meta.payload` 含 session_id / cwd）。派生名取
    `<cwd 目录名小写>-<uuid8 后两位>`：Codex 的 id 是 UUIDv7，前两位恒为 `01`，取前缀无法区分不同会话；后两位与 status 行首的 uuid8 字面对应，便于核对。

    与 load_session_names 同一套约束：只读、按次扫描不缓存、容错（坏文件跳过）。
    rollout 是历史累积（本机数百个），按 mtime 降序只解析最近 max_scan 个。
    """
    d = Path(sessions_dir) if sessions_dir is not None else DEFAULT_CODEX_SESSIONS_DIR
    try:
        candidates = list(d.glob(_CODEX_ROLLOUT_GLOB))
    except OSError:
        return {}
    stamped: list[tuple[float, Path]] = []
    for path in candidates:
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    stamped.sort(key=lambda item: item[0], reverse=True)
    names: dict[str, str] = {}
    for _, path in stamped[:max_scan]:
        meta = _codex_session_meta(path)
        if meta is None:
            continue
        sid, cwd = meta
        if sid in names:                       # 同会话多次 rollout 取最近一个
            continue
        label = _basename_any(cwd).lower()
        if not label:
            continue
        head = sid.split("-", 1)[0]
        names[sid] = f"{label}-{head[-2:]}" if len(head) >= 2 else f"{label}-{sid[-2:]}"
    return names


def load_merged_session_names(sessions_dir=None, codex_sessions_dir=None,
                              *, max_scan: int = _CODEX_MAX_SCAN) -> dict[str, str]:
    """合并 CC 注册表与 Codex rollout 派生名；同一 UUID 以 CC 注册表为准。"""
    names = load_codex_session_names(codex_sessions_dir, max_scan=max_scan)
    names.update(load_session_names(sessions_dir))
    return names


def format_session_identity(session_id, sessions_dir=None, codex_sessions_dir=None) -> str | None:
    """单次扫描会话身份源并返回 `name · uuid8`（命中）/ `uuid8`（未命中）。

    $route 回执用（命令低频，按次扫描即可）。无 session → None（无可标识）。
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_display_id(
        load_merged_session_names(sessions_dir, codex_sessions_dir), session_id)
