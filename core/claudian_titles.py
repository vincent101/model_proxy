"""Claudian 会话标题只读解析（vault/.claudian/sessions/conv-*.meta.json）。

$route 回执（server.py）用：把 session UUID 翻译成 claudian 会话标题，展示形态
`session <name · uuid8>（<标题>）`。

与 session_identity.py 的分工（2026-10-27 方案）：
- 注册表目录是个位数小文件 → session_identity 整文件 json.load；本目录是
  178 文件 / 539 MB 大文件场景 → 每文件只读头部 8 KiB + 行锚定正则
- 两者同为只读、按次扫描不缓存、容错跳坏、未命中由调用方回退
- 路径按包位置推导（parents[3] = vault 根），不加 config 字段：model_proxy
  物理在 vault 内是既成部署事实，脱离 vault / LAN 远端 session 的失效形态
  与 config 缺字段完全一致（静默回退 uuid8），config 只增加配置面

头部正则依赖 claudian SessionStorage.toSessionMetadata 的固定键序
（JSON.stringify(meta, null, 2) 的副产品）：title/updatedAt/顶层 sessionId
均落在头部（实测 177/177 命中）。行锚定（^ 顶层两空格 / ^    providerState
层）+ JSON 字符串内换行必被转义，正则不会误匹配字符串值内部的伪键。键序
漂移的检出闭环 = test_claudian_titles.py 的真实目录冒烟（命中率跌破 95%
先红）。
"""

import json
import re
from pathlib import Path

# 会话元数据默认位置。函数体内按调用时取值，测试可 patch 本属性注入临时目录。
DEFAULT_CLAUDIAN_SESSIONS_DIR = Path(__file__).resolve().parents[3] / ".claudian" / "sessions"

# 只读头部大小：实测最活跃会话（85 MB）的 title/sessionId 均在头部 600 B 内。
_HEAD_BYTES = 8192

# 顶层字符串值键：命中取 group(1)（已转义的原文），null 分支 group(1) 为 None。
_STR = r'(?:"((?:[^"\\]|\\.)*)"|null)'
_RE_TITLE = re.compile(r'^  "title": ' + _STR, re.M)
_RE_UPDATED = re.compile(r'^  "updatedAt": (\d+)', re.M)
_RE_SESSION = re.compile(r'^  "sessionId": ' + _STR, re.M)
_RE_PROVIDER = re.compile(r'^    "providerSessionId": "((?:[^"\\]|\\.)*)"', re.M)


def _iter_title_entries(sessions_dir=None):
    """每文件产出 (session_uuid, title, updated_at)；不合规文件静默跳过。

    join 键：顶层 sessionId；为 null / 头部未命中时回退
    providerState.providerSessionId（两者在同时存在时语义等同）。
    容错：OSError / UnicodeDecodeError / 正则未命中 / title 为 null 或空 /
    无任何 UUID → 跳过该文件，不影响其余。
    """
    d = Path(sessions_dir) if sessions_dir is not None else DEFAULT_CLAUDIAN_SESSIONS_DIR
    try:
        files = sorted(d.glob("*.meta.json"))
    except OSError:
        return
    for p in files:
        try:
            with open(p, "rb") as f:
                chunk = f.read(_HEAD_BYTES)
            # 8 KiB 边界可能切在多字节字符/半行中间：行锚定正则只消费完整行，
            # 丢弃截断尾行（真实目录有 6 个文件因此整文件误跳、命中率跌破
            # 95% 检出阈值）；截断后的解码失败才是真损坏（二进制垃圾）。
            if len(chunk) == _HEAD_BYTES:
                cut = chunk.rfind(b"\n")
                if cut != -1:
                    chunk = chunk[:cut + 1]
            head = chunk.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        m = _RE_TITLE.search(head)
        if not m or m.group(1) is None:
            continue
        try:
            title = json.loads(f'"{m.group(1)}"')
        except json.JSONDecodeError:
            continue
        if not isinstance(title, str) or not title:
            continue
        sm = _RE_SESSION.search(head)
        session_uuid = sm.group(1) if (sm and sm.group(1) is not None) else None
        if session_uuid is None:
            pm = _RE_PROVIDER.search(head)
            session_uuid = pm.group(1) if pm else None
        if not session_uuid:
            continue
        um = _RE_UPDATED.search(head)
        updated = int(um.group(1)) if um else -1
        yield session_uuid, title, updated


def load_session_titles(sessions_dir=None) -> dict[str, str]:
    """扫描会话元数据目录一次，返回 {sessionUuid: title}。

    同 UUID 多 conv（fork/演进）→ updatedAt 最新者胜（无 updatedAt 视为最旧；
    相等时取文件名序靠前者，任取其一语义等价）。
    """
    best: dict[str, tuple[int, str]] = {}
    for session_uuid, title, updated in _iter_title_entries(sessions_dir):
        prev = best.get(session_uuid)
        if prev is None or updated > prev[0]:
            best[session_uuid] = (updated, title)
    return {k: v[1] for k, v in best.items()}


def lookup_session_title(session_id, sessions_dir=None) -> str | None:
    """单次扫描并返回 session 对应的会话标题；未命中/非法输入 → None。

    session_key 恒为完整 UUID，只做精确匹配（不做前缀匹配：宁缺勿错）。
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    return load_session_titles(sessions_dir).get(session_id)
