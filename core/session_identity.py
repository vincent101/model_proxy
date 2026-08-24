"""Claude Code session 身份注册表只读解析（~/.claude/sessions/*.json）。

status CLI（_format_ops.py）与代理 server 层（$route 回执）共用：把 session
UUID 翻译成人类可读的 name（展示形态 `notevault-44 · b6ceb46d`）。

设计约束（2026-08-24 方案）：
- 只读：绝不写注册表，不做任何持久化
- 按次扫描不缓存：目录规模是活跃进程数级别（个位/十位数），缓存反而引入失效问题
- 不进 runtime_paths.json：其职责是代理自产运行文件的路径，本注册表是外部状态
- 容错：坏 JSON/缺目录/缺字段一律跳过；未命中由调用方回退仅显示 UUID8
"""

import json
from datetime import datetime
from pathlib import Path

# 注册表默认位置。函数体内按调用时取值，测试可 patch 本属性注入临时目录。
DEFAULT_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

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


def format_session_identity(session_id, sessions_dir=None) -> str | None:
    """单次扫描注册表并返回身份串：`name · uuid8`（命中）/ `uuid8`（未命中）。

    $route 回执用（命令低频，按次扫描即可）。无 session → None（无可标识）。
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_display_id(load_session_names(sessions_dir), session_id)
