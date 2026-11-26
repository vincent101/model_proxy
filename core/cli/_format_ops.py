#!/usr/bin/env python3
"""model_proxy status 格式化单一来源（单源化，消除 CLI status 与菜单 list 双实现漂移）。

由 model_proxy_cli.sh 的 cmd_status 调用，也可被 _config_ops.py import。

CLI 入口：
    python3 _format_ops.py status-format <config> <totals>   # stdin 读 server JSON

格式化函数返回 list[str]（不直接 print），可被 unittest 覆盖。
"""

import json
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.protocol_hints import PROTOCOL_HINT_WINDOW_DAYS
from core.session_identity import (
    load_merged_session_names,
    match_session_name,
    session_display_id,
)

TIER_NAMES = ("opus", "sonnet", "haiku")

# P0 degraded 阈值（用户拍板：fail%>30% 且样本≥5）
DEGRADED_MIN_REQUESTS = 5
DEGRADED_FAIL_PCT = 30.0

# CST 时区（与 server _cst_now 一致，账本 today 桶按 CST 取）
_CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def display_width(s: str) -> int:
    """基于 unicodedata.east_asian_width 的显示宽度（W/F 计 2，其余计 1）。

    用于 80 列判定与测试断言。中文 note 恒置于行尾，不参与定宽 padding。
    """
    w = 0
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def _pad(s: str, width: int) -> str:
    """左对齐定宽填充（按 display_width 计算填充量，兼容 CJK）。"""
    pad = width - display_width(s)
    return s + " " * max(0, pad)


def mask_appkey(appkey: str) -> str:
    """脱敏：...尾4，空值 (空)。自 _config_ops.py 迁入，全仓唯一脱敏入口。"""
    return f"...{appkey[-4:]}" if appkey else "(空)"


def strategy_route_desc(st: dict) -> str:
    """打印用的 route 归属描述：兼容旧单值 route_id 与新 route_pool 写法。

    自 _config_ops.py _strategy_route_desc 迁入。调用方（strategy_edit/switch）通过
    反向 import 本函数，行为不变。
    """
    route_pool = st.get("route_pool")
    if route_pool:
        parts = []
        for item in route_pool:
            if not isinstance(item, dict):
                continue
            rid = item.get("route_id", "?")
            weight = item.get("weight", 1)
            parts.append(f"{rid}:{weight}")
        return "pool[" + ",".join(parts) + "]"
    return st.get("route_id", "?")


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def normalize_supply(d: dict) -> dict:
    """归一两种来源的 supply dict，产出统一展示字段。

    - config 原生：含 appkey（需 mask）
    - server status JSON：已剥 appkey，含 appkey_tail4
    """
    sid = d.get("id", "?")
    proto = d.get("protocol", "?")
    model = d.get("target_model", "?")
    if "appkey" in d:
        key_masked = mask_appkey(d.get("appkey", ""))
    else:
        tail4 = d.get("appkey_tail4", "")
        key_masked = f"...{tail4}" if tail4 else "(空)"
    has_rcap = bool(d.get("reasoning_capability"))
    if "cooldown_seconds" in d:
        cooldown_display = f"{d['cooldown_seconds']}s"
    else:
        cooldown_display = "(默认)"
    return {
        "id": sid,
        "protocol": proto,
        "model": model,
        "key_masked": key_masked,
        "has_rcap": has_rcap,
        "cooldown_display": cooldown_display,
    }


# ---------------------------------------------------------------------------
# P0 数据函数（全部 stdlib，零 server 改动）
# ---------------------------------------------------------------------------

def _parse_combo_key(key: str) -> dict:
    """拆 combo 键 `supply=X|route=Y|strategy=Z` 成 dict。

    与 cmd_stats 的 parse_combo_key（model_proxy_cli.sh:503-508）同逻辑；
    heredoc 内嵌 python 无法 import，此为有注释互指的小重复。
    """
    dims = {}
    for part in key.split("|"):
        k, v = part.split("=", 1)
        dims[k] = v
    return dims


def _load_config_tolerant(config_path: str) -> tuple[dict, str | None]:
    """容错读 config：JSON 损坏/读取失败返回 ({}, 错误描述)，不抛异常。

    与 proxy 侧 `_reload_locked` 的容错口径一致（损坏不至于让 CLI status 崩溃）。
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, OSError) as e:
        return {}, f"config 读取失败: {e}"


def load_supply_health(totals_path: str) -> dict[str, dict]:
    """读账本 CST today 桶，combos 按 supply 聚合 {requests, ok, fail}。

    文件缺失/JSON 损坏 → 返回 {}（降级不报错）。
    """
    try:
        with open(totals_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    today_str = datetime.now(_CST).strftime("%Y-%m-%d")
    day = data.get("days", {}).get(today_str, {})
    combos = day.get("combos", {})

    health: dict[str, dict] = {}
    for key, v in combos.items():
        dims = _parse_combo_key(key)
        sid = dims.get("supply", "?")
        agg = health.setdefault(sid, {"requests": 0, "ok": 0, "fail": 0})
        agg["requests"] += v.get("requests", 0)
        agg["ok"] += v.get("ok", 0)
        agg["fail"] += v.get("fail", 0)
    return health


def _route_tokens(cfg: dict, *, explicit_only: bool = False) -> dict[str, list[str]]:
    """route_id → 引用它的 strategy token 列表（route_id 单值 + route_pool 两种写法）。"""
    route_tokens: dict[str, list[str]] = {}
    for st in cfg.get("strategies", []):
        if explicit_only and "client_token" not in st:
            continue
        tok = st.get("client_token", "?")
        if st.get("route_id"):
            route_tokens.setdefault(st["route_id"], []).append(tok)
        for item in st.get("route_pool") or []:
            if isinstance(item, dict) and item.get("route_id"):
                route_tokens.setdefault(item["route_id"], []).append(tok)
    return route_tokens


class StatusReferenceConflict(ValueError):
    """A visible status identity or error path conflicts with the admin token."""


def _supply_refs(cfg: dict, *, forbidden_reference_token=None,
                 visible_supply_ids=None) -> dict[str, list[str]]:
    """supply_id → 引用它的 `route(token,...) tier` 列表。

    用于在 degraded/cooldown 行尾标注该 supply 被哪个 route 的哪档、哪个 strategy 引用。
    """
    route_tokens = _route_tokens(cfg)
    explicit_tokens = (_route_tokens(cfg, explicit_only=True)
                       if forbidden_reference_token is not None else None)
    refs: dict[str, list[str]] = {}
    for r in cfg.get("routes", []):
        rid = r.get("id", "?")
        toks = route_tokens.get(rid, [])
        toks_str = f"({','.join(toks)})" if toks else ""
        for tn in TIER_NAMES:
            for sid in r.get("tiers", {}).get(tn, []):
                if (forbidden_reference_token is not None and visible_supply_ids is not None
                        and sid in visible_supply_ids and
                        (("id" in r and str(rid) == forbidden_reference_token) or
                         forbidden_reference_token in explicit_tokens.get(rid, []))):
                    raise StatusReferenceConflict("status reference conflicts with admin token")
                refs.setdefault(sid, []).append(f"{rid}{toks_str} {tn}")
    return refs


# ---------------------------------------------------------------------------
# 活跃 session 链路健康（CLI 端解析 ACCESS 日志，零 server 改动）
# ---------------------------------------------------------------------------

def parse_access_line(line: str) -> dict | None:
    """解析单条 ACCESS 日志行，返回字段 dict 或 None（非 ACCESS / 坏行）。

    日志格式：``YYYY-MM-DD HH:MM:SS,mmm req_id=<id> ACCESS ms= status= ...``

    关键陷阱：``route_failover=`` 含子串 ``failover=``——必须空格分词后
    精确匹配 key，不能子串匹配（否则 failover 计数虚增一倍）。
    """
    if len(line) < 24:
        return None
    try:
        ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None
    rest = line[23:].lstrip()
    idx = rest.find(" ACCESS ")
    if idx < 0:
        return None
    prefix = rest[:idx]
    kvs_str = rest[idx + 8:]
    req_id = ""
    for tok in prefix.split():
        if tok.startswith("req_id="):
            req_id = tok.split("=", 1)[1]
            break
    # 空格分词后精确 key 匹配（route_failover 不误命中 failover）
    fields: dict[str, str] = {}
    for tok in kvs_str.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return {
        "ts": ts,
        "req_id": req_id,
        "status": fields.get("status", ""),
        "source": fields.get("source", ""),
        "operation": fields.get("operation", ""),
        "target_protocol": fields.get("target_protocol", ""),
        "conversion_kind": fields.get("conversion_kind", ""),
        "usage_in": fields.get("usage_in", ""),
        "usage_out": fields.get("usage_out", ""),
        "token": fields.get("token", ""),
        "route": fields.get("route", ""),
        "tier": fields.get("tier", ""),
        "supply": fields.get("supply", ""),
        "provider": fields.get("provider", ""),
        "failover": fields.get("failover", ""),
        "session": fields.get("session", ""),
        "route_failover": fields.get("route_failover", ""),
        "builtin": fields.get("builtin", ""),
        "final_error": fields.get("final_error", ""),
        "response_committed": fields.get("response_committed", ""),
        "stream_integrity": fields.get("stream_integrity", ""),
        "terminal_status": fields.get("terminal_status", ""),
        "terminal_reason": fields.get("terminal_reason", ""),
        "first_event_ms": fields.get("first_event_ms", ""),
        # ms / attempts 供 status 的供给段算吞吐与重试率；老日志行没有就留空。
        "ms": fields.get("ms", ""),
        "attempts": fields.get("attempts", ""),
    }


def _short_supply(sid: str) -> str:
    """supply id 缩写：去掉 vendor 段（-sankuai / -openai）。

    kimi-k3-sankuai-3339 → kimi-k3-3339；glm-52-sankuai-openai-3339 → glm-52-3339。
    """
    return sid.replace("-sankuai", "").replace("-openai", "")


def tail_access_rows(log_path: str, *, tail_bytes: int = 2 * 1024 * 1024) -> dict:
    """读日志末 tail_bytes 并解析出全部 ACCESS 行。

    返回 ``{"rows": [parsed...], "seek_tail": bool, "log_missing": bool}``。

    抽出的原因：status 的供给段与活跃 session 段用的是同一份日志滚窗，
    各读一遍 2MB 再各解析一遍是纯浪费（一次 status 调用读两次盘）。
    调用方各自再做时间窗过滤，因为两段的窗口口径可能不同。
    """
    if not os.path.isfile(log_path):
        return {"rows": [], "seek_tail": False, "log_missing": True}
    file_size = os.path.getsize(log_path)
    seek_tail = file_size > tail_bytes
    try:
        with open(log_path, "rb") as f:
            if seek_tail:
                f.seek(-tail_bytes, 2)
                f.readline()  # 丢弃首条残行
            else:
                f.seek(0)
            raw = f.read()
    except OSError:
        return {"rows": [], "seek_tail": False, "log_missing": True}
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parsed = parse_access_line(line)
        if parsed is not None:
            rows.append(parsed)
    return {"rows": rows, "seek_tail": seek_tail, "log_missing": False}


def load_active_sessions(log_path: str, *, now: datetime | None = None,
                         window_minutes: int = 30,
                         tail_bytes: int = 2 * 1024 * 1024,
                         rows=None, seek_tail: bool | None = None) -> dict:
    """tail 读日志末 tail_bytes，过滤 30min 窗口内 ACCESS 行，按 session 分组聚合。

    返回 ``{"sessions": {...}, "truncated": bool, "log_missing": bool}``。

    - builtin=route 行计入活跃、不计入统计（n/fail/fo）
    - 空串 session 聚成 ``(none)`` 桶
    - 截断检测：文件 > tail_bytes 且 buffer 内首条可解析行 ts 已在窗口内
    """
    if now is None:
        now = datetime.now()

    if rows is not None:
        # 调用方（status）已读过同一份日志，直接复用，避免二次读盘/二次解析。
        return _group_sessions(rows, now=now, window_minutes=window_minutes,
                               seek_tail=bool(seek_tail), log_missing=False)
    tail = tail_access_rows(log_path, tail_bytes=tail_bytes)
    if tail["log_missing"]:
        return {"sessions": {}, "truncated": False, "log_missing": True}
    return _group_sessions(tail["rows"], now=now, window_minutes=window_minutes,
                           seek_tail=tail["seek_tail"],
                           log_missing=False)


def _group_sessions(rows, *, now: datetime | None = None,
                    window_minutes: int = 30, seek_tail: bool = False,
                    log_missing: bool = False) -> dict:
    """把已解析的 ACCESS 行按 session 聚合成 load_active_sessions 的返回形态。

    抽出是为了让 status 一次 tail 的结果同时喂给供给段和 session 段；
    ``load_active_sessions`` 保留为「读盘 + 聚合」的兼容入口。
    """
    if now is None:
        now = datetime.now()
    window_start = now - timedelta(minutes=window_minutes)

    sessions: dict[str, dict] = {}
    first_ts_in_buffer: datetime | None = None

    for parsed in rows:
        if first_ts_in_buffer is None:
            first_ts_in_buffer = parsed["ts"]
        if parsed["ts"] < window_start:
            continue
        sid = parsed["session"] or "(none)"
        agg = sessions.setdefault(sid, {
            "n": 0, "fail": 0, "fo": 0,
            "last_ts": None, "last_status": "", "last_route": "",
            "last_tier": "", "last_supply": "", "last_error": "",
            "last_req_id": "", "builtin_only": True,
            "chains": {}, "routes": {}, "tokens": {},
        })
        # token 即 client 身份（cc=ClaudeCode / codex=Codex / ailoan 等），
        # 聚合后按次数取主 token 渲染，区分不同 SDK 的 session。
        _tok = parsed["token"]
        if _tok:
            agg["tokens"][_tok] = agg["tokens"].get(_tok, 0) + 1
        is_builtin = parsed["builtin"] == "route"
        if is_builtin:
            # builtin 行只在 builtin_only session 上更新 last_*（用于活跃判定）
            if agg["builtin_only"]:
                if agg["last_ts"] is None or parsed["ts"] > agg["last_ts"]:
                    agg["last_ts"] = parsed["ts"]
                    agg["last_status"] = parsed["status"]
                    agg["last_route"] = parsed["route"]
                    agg["last_tier"] = parsed["tier"]
                    agg["last_supply"] = parsed["supply"]
                    agg["last_req_id"] = parsed["req_id"]
        else:
            agg["n"] += 1
            integrity = parsed["stream_integrity"]
            if ((integrity and integrity != "valid")
                    or (not integrity and parsed["status"] != "200")):
                agg["fail"] += 1
            fo = 0
            try:
                fo += int(parsed["failover"] or 0)
            except ValueError:
                pass
            try:
                fo += int(parsed["route_failover"] or 0)
            except ValueError:
                pass
            agg["fo"] += fo
            agg["builtin_only"] = False
            chain = (parsed["tier"], parsed["supply"])
            agg["chains"][chain] = agg["chains"].get(chain, 0) + 1
            agg["routes"][parsed["route"]] = agg["routes"].get(parsed["route"], 0) + 1
            if agg["last_ts"] is None or parsed["ts"] > agg["last_ts"]:
                agg["last_ts"] = parsed["ts"]
                agg["last_status"] = parsed["status"]
                agg["last_route"] = parsed["route"]
                agg["last_tier"] = parsed["tier"]
                agg["last_supply"] = parsed["supply"]
                agg["last_error"] = parsed["final_error"]
                agg["last_req_id"] = parsed["req_id"]
                agg["last_stream_integrity"] = parsed["stream_integrity"]
                agg["last_terminal_status"] = parsed["terminal_status"]

    # 截断判定：buffer 首条可解析行已在窗口内 → 窗口起点可能在 buffer 外
    truncated = bool(
        seek_tail and first_ts_in_buffer
        and first_ts_in_buffer >= window_start
    )

    return {"sessions": sessions, "truncated": truncated, "log_missing": log_missing}


# ---------------------------------------------------------------------------
# 供给健康段：窗口内的可用状态 / 成功率 / 首 token / 吞吐
# ---------------------------------------------------------------------------

SUPPLY_WINDOW_MINUTES = 30     # 与活跃 session 段同一滚窗
SUPPLY_MIN_SAMPLES = 5         # 低于此样本量不给成功率（避免假精度）
SUPPLY_FAIL_PCT = 30.0         # 失败率阈值（与 DEGRADED_FAIL_PCT 同口径）
_FLAKY_CONSECUTIVE = 2         # stream_health 连续 invalid ≥ 此值判 flaky


def _is_supply_sample(parsed: dict) -> bool:
    """该 ACCESS 行是否计入供给统计。

    排除三类——它们不代表上游供给的健康度：
    - ``builtin=route``：`$route` 查询/切换，不发给上游
    - ``provider`` 为空：count_tokens 本地短路等未出站的请求
    - ``client_disconnect``：客户端中途断开，不是上游故障
    """
    if parsed.get("builtin") == "route":
        return False
    if not parsed.get("provider"):
        return False
    return parsed.get("stream_integrity") != "client_disconnect"


def _access_ok(parsed: dict) -> bool:
    """与账本 UsageTotalsStore.record() 的 ok/fail 判定保持一致。

    有 stream_integrity 时以它为准（200+invalid 视为失败）；没有时只看状态码。
    """
    integrity = parsed.get("stream_integrity") or ""
    status = parsed.get("status") or ""
    if integrity:
        return status == "200" and integrity == "valid"
    return status == "200"


def load_supply_stats(rows, *, now: datetime | None = None,
                      window_minutes: int = SUPPLY_WINDOW_MINUTES) -> dict[str, dict]:
    """把已解析的 ACCESS 行按 supply 聚合成窗口统计。

    返回 ``{supply_id: {"n", "fail", "ok_pct", "ttft_ms", "tps", "codes"}}``：

    - ``ttft_ms``：成功流式行 ``first_event_ms`` 的**中位数**（p50，抗思考长尾）；
      窗口内没有可用样本时为 None（非流式请求不产生该字段）。
    - ``tps``：``Σusage_out / Σ(ms - first_event_ms)``，仅统计成功流式且
      ``usage_out > 0`` 的行。**先聚合再相除**——逐请求算 TPS 再取均值会被
      短回答（几十 token）严重虚高。
    - ``codes``：失败行的错误码计数（``final_error`` 优先，回退
      ``http_<status>`` / ``invalid_stream``），用于显示最高频失败原因。
    """
    if now is None:
        now = datetime.now()
    window_start = now - timedelta(minutes=window_minutes)

    agg: dict[str, dict] = {}
    for parsed in rows:
        if parsed["ts"] < window_start or not _is_supply_sample(parsed):
            continue
        sid = parsed.get("supply") or "(none)"
        item = agg.setdefault(sid, {
            "n": 0, "fail": 0, "ttft": [], "usage_out": 0, "gen_seconds": 0.0,
            "codes": {},
        })
        item["n"] += 1
        if not _access_ok(parsed):
            item["fail"] += 1
            code = (parsed.get("final_error")
                    or (f"http_{parsed['status']}" if parsed.get("status") != "200"
                        else "invalid_stream"))
            item["codes"][code] = item["codes"].get(code, 0) + 1
            continue
        try:
            first_event_ms = int(parsed.get("first_event_ms") or 0)
        except (TypeError, ValueError):
            first_event_ms = 0
        if first_event_ms <= 0:
            continue      # 非流式 / 未记录首 token
        item["ttft"].append(first_event_ms)
        try:
            total_ms = int(parsed.get("ms") or 0)
        except (TypeError, ValueError):
            total_ms = 0
        try:
            usage_out = int(parsed.get("usage_out") or 0)
        except (TypeError, ValueError):
            usage_out = 0
        if total_ms > first_event_ms and usage_out > 0:
            item["usage_out"] += usage_out
            item["gen_seconds"] += (total_ms - first_event_ms) / 1000.0

    out: dict[str, dict] = {}
    for sid, item in agg.items():
        n, fail = item["n"], item["fail"]
        ttft = sorted(item["ttft"])
        out[sid] = {
            "n": n,
            "fail": fail,
            "ok_pct": (100.0 * (n - fail) / n) if n else None,
            "ttft_ms": ttft[len(ttft) // 2] if ttft else None,
            "tps": (item["usage_out"] / item["gen_seconds"]
                    if item["gen_seconds"] > 0 else None),
            "codes": item["codes"],
        }
    return out


def _fmt_ttft(ms: int | None) -> str:
    if not ms:
        return "-"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _fmt_tps(tps: float | None) -> str:
    return f"{tps:.0f}" if tps else "-"


CODE_CLIP = 48     # 失败码列的显示宽度上限（按字符数，宽字符已足够近似）


def _clip_code(text: str, limit: int = CODE_CLIP) -> str:
    """把失败码截断到可读宽度。

    失败码来源有两类：短枚举（``http_429``）与整段上游异常文本
    （``net_error:<urlopen error [Errno 8] nodename nor servname ...>``）。
    后者会把整行撑到一百多列，破坏表格可读性；截断后仍能区分到底是哪类问题。
    """
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _top_code(codes: dict) -> str:
    """最高频失败码：去掉下划线便于单行渲染，附次数。"""
    if not codes:
        return ""
    code = max(codes, key=codes.get)
    return _clip_code(f"{code.replace('_', ' ')}×{codes[code]}")


def resolve_supply_state(sid: str, stat: dict | None, *, cooling: dict | None,
                         stream_health: dict | None, window_total: int) -> str:
    """单条供给的状态枚举。优先级 cool > fail > flaky > ok > low > idle。"""
    if cooling and sid in cooling:
        return "cool"
    n = stat["n"] if stat else 0
    if n >= SUPPLY_MIN_SAMPLES:
        if stat["ok_pct"] is not None and stat["ok_pct"] < 100.0 - SUPPLY_FAIL_PCT:
            return "fail"
        consecutive = 0
        supplies = (stream_health or {}).get("supplies") or {}
        entry = supplies.get(sid)
        if isinstance(entry, dict):
            try:
                consecutive = int(entry.get("consecutive") or 0)
            except (TypeError, ValueError):
                consecutive = 0
        if consecutive >= _FLAKY_CONSECUTIVE:
            return "flaky"
        return "ok"
    if n > 0:
        return "low"          # 有样本但不足，成功率显示 "-"
    return "idle"


def _format_supplies(cfg: dict, stats: dict[str, dict], *, cooldown: dict | None,
                     stream_health: dict | None, truncated: bool,
                     log_missing: bool, refs: dict[str, list[str]],
                     forbidden_reference_token: str | None = None) -> list[str]:
    """渲染供给段：只列被 route 引用、且窗口内有请求的供给。

    标题行 = ``supply health (last 30min · ttft=p50 首token · tps=输出tok/s)``。
    标题只留窗口与图例——计数项（cooldown/degraded/overrides）已按使用者要求移除：
    cooldown 与 degraded 在表内分别以 `cool` 行和 `fail` 行呈现，重复列出只会
    产生「数字与行数是否一致」的额外心智负担。

    行格式：``<id>  <state>  <ok%>  <n>  <ttft>  <tps>  [<失败码>]``
    """
    legend = [f"last {SUPPLY_WINDOW_MINUTES}min"]
    if truncated:
        legend.append("tail")
    legend += ["ttft=p50 首token", "tps=输出tok/s"]
    header = "supply health (" + " · ".join(legend) + ")"
    referenced: list[str] = []
    seen: set[str] = set()
    for supply in cfg.get("supplies", []):
        if not isinstance(supply, dict):
            continue
        sid = supply.get("id")
        if isinstance(sid, str) and sid and sid not in seen and sid in refs:
            seen.add(sid)
            referenced.append(sid)

    if log_missing:
        return [header, "  无数据（日志文件缺失）"]

    window_total = sum(stat["n"] for stat in stats.values())
    lines = [header]
    state_order = {"cool": 0, "fail": 1, "flaky": 2, "ok": 3, "low": 4}
    rendered: list[tuple[int, int, str]] = []
    for sid in referenced:
        if forbidden_reference_token is not None and sid == forbidden_reference_token:
            raise StatusReferenceConflict("visible supply identity conflicts with admin token")
        stat = stats.get(sid)
        state = resolve_supply_state(sid, stat, cooling=cooldown,
                                     stream_health=stream_health,
                                     window_total=window_total)
        if state == "idle":
            # 窗口内零请求的供给不占行：它没有任何可评估的健康信息，
            # 列出来只会稀释表格；供给总数看 config 行的 supplies 计数。
            continue
        state_text = state
        if state == "cool":
            info = (cooldown or {}).get(sid) or {}
            remain = info.get("remain", 0) if isinstance(info, dict) else info
            state_text = f"cool {int(remain)}s"
            reason = info.get("reason", "") if isinstance(info, dict) else ""
            if (forbidden_reference_token is not None and reason
                    and str(reason) == forbidden_reference_token):
                raise StatusReferenceConflict("visible cooldown reason conflicts with admin token")
            code = _clip_code((reason or "").replace("_", " "))
        elif state == "fail":
            code = _top_code(stat["codes"]) if stat else ""
        else:
            code = ""
        ok_pct = "-" if (stat is None or state == "low" or state == "idle") else f"{stat['ok_pct']:.0f}%"
        n = stat["n"] if stat else 0
        ttft = _fmt_ttft(stat["ttft_ms"] if stat else None)
        tps = _fmt_tps(stat["tps"] if stat else None)
        rendered.append((state_order[state], -n,
                         f"  {_pad(sid, 26)} {_pad(state_text, 8)} {ok_pct:>4} {n:>5} "
                         f"{ttft:>7} {tps:>5}  {code}".rstrip()))
    for _, _, line in sorted(rendered):
        lines.append(line)
    if not rendered and referenced:
        lines.append(f"  （近 {SUPPLY_WINDOW_MINUTES}min 内被引用的供给均无请求）")
    # 未引用的供给不再列名：它在窗口内没有请求、也没有健康信息可评估，列出来
    # 只会稀释表格（需要时看 config 行的 supplies 计数）。顺带说明：既然不再
    # 渲染这些 id，原先「未引用摘要撞管理 token 则 fail-closed」的守卫也一并
    # 移除——若将来重新加上列名，必须同步恢复该守卫。
    return lines


def _format_active_sessions(result: dict, cfg: dict | None = None,
                            session_names: dict[str, str] | None = None, *,
                            forbidden_reference_token: str | None = None) -> list[str]:
    """渲染活跃 session 段（header + 每 session 一行 + FAIL err 续行）。

    行格式：`[name · ]sid8  state n=N fail=F [fo=K] [client]  HH:MM status  route tier/supply(pct%) ...`
    session_names 为 ~/.claude/sessions 注册快照（{sessionId: name}），命中时
    行首附 name；id 列宽随内容动态放宽（≥8），命名行与未命名行各自对齐。
    链路为窗口内 (tier, supply) 分布按次数降序；route 取窗口内主 route。
    排序：FAIL → warn → ok，同档按最近请求时间倒序。
    """
    sessions = result["sessions"]
    truncated = result["truncated"]
    log_missing = result["log_missing"]

    if log_missing:
        return ["active sessions (30min): 无数据（日志文件缺失）"]
    if not sessions:
        return ["active sessions (30min): 无活跃请求"]

    # 判定状态
    order = {"FAIL": 0, "warn": 1, "ok": 2}
    entries: list[tuple[str, dict, str]] = []
    for sid, agg in sessions.items():
        if agg["builtin_only"]:
            state = "ok"
        elif agg["last_status"] != "200":
            state = "FAIL"
        elif agg["fail"] > 0 or agg["fo"] > 0:
            state = "warn"
        else:
            state = "ok"
        entries.append((sid, agg, state))

    def _sort_key(e):
        sid, agg, st = e
        ts_val = agg["last_ts"].timestamp() if agg["last_ts"] else 0
        return (order[st], -ts_val)

    entries.sort(key=_sort_key)

    # 计数
    counts = {"ok": 0, "warn": 0, "FAIL": 0}
    for _, _, st in entries:
        counts[st] += 1

    # header
    header = f"active sessions (30min): {len(entries)}"
    summary_parts = []
    if counts["ok"]:
        summary_parts.append(f"{counts['ok']} ok")
    if counts["warn"]:
        summary_parts.append(f"{counts['warn']} warn")
    if counts["FAIL"]:
        summary_parts.append(f"{counts['FAIL']} FAIL")
    if summary_parts:
        header += "  (" + " · ".join(summary_parts) + ")"
    if truncated:
        header += "  （窗口数据可能被截断）"

    # 上限 20 行
    max_rows = 20
    shown = entries[:max_rows]

    # 行首 id：注册快照命中时附 name（name · uuid8）；id 列宽随内容动态放宽（≥8）。
    names = session_names or {}
    id_strs = [session_display_id(names, sid) for sid, _, _ in shown]
    id_w = max([8] + [display_width(s) for s in id_strs])
    # name 标注存在的语义提示只在段标题说一次（name 取自本机会话记录——CC 注册表
    # 与 Codex rollout 派生名——可能滞后于 30min 窗口内的历史请求），不逐行重复。
    if any(match_session_name(names, sid) for sid, _, _ in shown):
        header += "  （name 取自本机会话记录）"

    lines = [header]

    for (sid, agg, state), id_str in zip(shown, id_strs):
        fo_part = f" fo={agg['fo']}" if agg["fo"] > 0 else ""
        ts_str = agg["last_ts"].strftime("%H:%M") if agg["last_ts"] else "--:--"

        # 主 route（2026-11-06：去掉 strategy token 括注——行内已有 [token] client
        # 标注，route 后的 (cc,codex) 重复且与 client 标注混淆）
        main_route = ""
        if agg["routes"]:
            main_route = max(agg["routes"].items(), key=lambda kv: kv[1])[0]
        elif agg["last_route"]:
            main_route = agg["last_route"]
        route_part = main_route

        # client 标注：窗口内主 token（cc=ClaudeCode / codex=Codex），多 token 并列
        toks_part = ""
        toks_sorted = []
        if agg.get("tokens"):
            toks_sorted = sorted(agg.get("tokens", {}).items(), key=lambda kv: -kv[1])
            toks_part = "  [" + ",".join(t for t, _ in toks_sorted) + "]"

        if forbidden_reference_token is not None:
            display_sid = (sid or "")[:8]
            display_name = match_session_name(names, sid)
            visible_tokens = (t for t, _ in toks_sorted)
            if (display_sid == forbidden_reference_token or
                    display_name == forbidden_reference_token or
                    str(route_part) == forbidden_reference_token or
                    forbidden_reference_token in visible_tokens):
                raise StatusReferenceConflict("visible session identity conflicts with admin token")
            if not agg["builtin_only"] and any(
                    _short_supply(supply) == forbidden_reference_token
                    for (_, supply) in agg["chains"]):
                raise StatusReferenceConflict("visible session identity conflicts with admin token")

        if agg["builtin_only"]:
            line = (f"  {_pad(id_str, id_w)}  {_pad(state, 4)} n=0 fail=0"
                    f"{toks_part}  {ts_str} 200"
                    f"  {route_part}（仅 $route)")
        else:
            # 窗口内链路分布：tier/supply(pct%)，按次数降序
            chains = sorted(agg["chains"].items(), key=lambda kv: -kv[1])
            chain_parts = []
            for (tier, supply), cnt in chains:
                pct = round(cnt / agg["n"] * 100) if agg["n"] else 0
                chain_parts.append(f"{tier}/{_short_supply(supply)}({pct}%)")
            line = (f"  {_pad(id_str, id_w)}  {_pad(state, 4)}"
                    f" n={agg['n']} fail={agg['fail']}{fo_part}{toks_part}"
                    f"  {ts_str} {agg['last_status']}"
                    f"  {route_part} {' '.join(chain_parts)}")
        lines.append(line.rstrip())

        # FAIL err 续行
        if state == "FAIL" and agg["last_error"]:
            req_short = agg["last_req_id"][:8]
            err_line = f"            err: {agg['last_error']}  req={req_short}"
            lines.append(err_line)

    if len(entries) > max_rows:
        lines.append(f"  ... 另有 {len(entries) - max_rows} 个 session")

    return lines


# ---------------------------------------------------------------------------
# 格式化函数（返回 list[str]，不直接 print）
# ---------------------------------------------------------------------------

def format_supplies(supplies: list[dict], *, preset: str) -> list[str]:
    """动态列宽格式化 supplies。

    preset="MENU": (id, protocol, model, key, rcap, cooldown)，保留带标签样式。
        菜单是交互宽屏场景，不受 80 列约束。

    STATUS preset 已下线（P0），明细全归菜单。
    """
    rows = [normalize_supply(s) for s in supplies]
    if not rows:
        return ["  (无)"]

    if preset == "MENU":
        lines = []
        for r in rows:
            rcap = "Y" if r["has_rcap"] else "-"
            line = (f"  {_pad(r['id'], 24)} protocol={_pad(r['protocol'], 10)}"
                    f" model={_pad(r['model'], 26)} appkey={_pad(r['key_masked'], 8)}"
                    f"  reasoning_capability={rcap}  cooldown={r['cooldown_display']}")
            lines.append(line.rstrip())
        return lines

    raise ValueError(f"unknown preset: {preset}")


def format_routes(routes: list[dict]) -> list[str]:
    """格式化 routes。单行优先；单行展示宽度 >80 时降级竖排。

    竖排格式：
      nation1 (failover=on)
        opus:   id1,id2,...
        sonnet: ...
        haiku:  ...

    单档仍超 80 时按逗号边界折行，续行对齐缩进，绝不在 id 中间断行。
    保证复制后去缩进拼回 == 原逗号串。
    """
    if not routes:
        return ["  (无)"]

    lines = []
    for r in routes:
        rid = r.get("id", "?")
        tiers = r.get("tiers", {})
        failover = r.get("failover", "?")

        # 先尝试单行
        tier_parts = []
        for tn in TIER_NAMES:
            ids = ",".join(tiers.get(tn, []))
            tier_parts.append(f"{tn}=[{ids}]")
        single = f"  {_pad(rid, 12)} {' '.join(tier_parts)} failover={failover}"

        if display_width(single) <= 80:
            lines.append(single)
        else:
            # 竖排
            lines.append(f"  {rid} (failover={failover})")
            for tn in TIER_NAMES:
                ids_str = ",".join(tiers.get(tn, []))
                label = f"{tn}:"
                indent = "    "
                prefix = f"{indent}{_pad(label, 9)} "
                base_line = (prefix + ids_str).rstrip()
                if display_width(base_line) <= 80:
                    lines.append(base_line)
                else:
                    # 按逗号边界折行，续行对齐缩进。
                    # 逗号放在续行行首（而非上一行行末），保证去缩进后直接拼接 == 原逗号串。
                    cont_indent = " " * display_width(prefix)
                    cur = prefix
                    first = True
                    for sid in tiers.get(tn, []):
                        if first:
                            candidate = cur + sid
                            first = False
                        else:
                            candidate = cur + "," + sid
                        if display_width(candidate) <= 80:
                            cur = candidate
                        else:
                            lines.append(cur.rstrip())
                            cur = cont_indent + "," + sid
                    if cur.strip():
                        lines.append(cur.rstrip())
    return lines


def format_strategies(strategies: list[dict], *, style: str) -> list[str]:
    """格式化 strategies。

    style="menu": 保持现有单行 {tok} -> {rid} ({note})。

    style="status" 已下线（P0），明细全归菜单。
    """
    if not strategies:
        return ["  (无)"]

    if style == "menu":
        lines = []
        for st in strategies:
            tok = st.get("client_token", "?")
            rid = strategy_route_desc(st)
            note = st.get("note", "") or ""
            lines.append(f"  {_pad(tok, 16)} -> {_pad(rid, 12)} ({note})")
        return lines

    raise ValueError(f"unknown style: {style}")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _compute_degraded(health: dict[str, dict]) -> list[dict]:
    """从 load_supply_health 结果算 degraded supply 列表（按 fail% 降序）。

    阈值：fail%>DEGRADED_FAIL_PCT 且 requests>=DEGRADED_MIN_REQUESTS，排除 (none)。
    返回 [{"id":..., "requests":..., "fail":..., "fail_pct":...}, ...]
    """
    degraded = []
    for sid, v in health.items():
        if sid == "(none)":
            continue
        r = v.get("requests", 0)
        f = v.get("fail", 0)
        if r < DEGRADED_MIN_REQUESTS:
            continue
        pct = 100.0 * f / r if r > 0 else 0.0
        if pct > DEGRADED_FAIL_PCT:
            degraded.append({"id": sid, "requests": r, "fail": f, "fail_pct": pct})
    degraded.sort(key=lambda x: -x["fail_pct"])
    return degraded


def _format_status_header(port: int, pid: int, started: int | None = None) -> str:
    """Common process header; started is an independently verified Unix time."""
    header = f"service running on port {port}  pid {pid}"
    if type(started) is int:
        elapsed = int(time.time()) - started
        if elapsed >= 0:
            days, seconds = divmod(elapsed, 86400)
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            uptime = (f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}" if days else
                      f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else
                      f"{minutes:02d}:{seconds:02d}")
            header += f"  up {uptime}  (started {time.strftime('%b %d %H:%M', time.localtime(started))})"
    return header


def _format_status_from_json(data: dict, config_path: str, totals_path: str,
                             log_path: str | None = None,
                             cooldown_unknown: bool = False,
                             sessions_dir=None,
                             codex_sessions_dir=None,
                             forbidden_reference_token: str | None = None) -> list[str]:
    """从 server status JSON + config + 日志 格式化 status 输出。

    布局：config 行 → supply health 段 → active sessions 段（在线时恒展示）
    → cooldown 明细段（仅有冷却时）。

    计数项（cooldown/degraded/overrides）不再单独成行：cooldown 与 degraded
    在表内分别以 `cool` / `fail` 行呈现，overrides 只影响会话 pin、不再展示。

    cooldown_unknown=True（离线统一展示路径）：cooldown 是 server 内存态离线拿不到，
    表内不会有 `cool` 行，明细段跳过；supply health 与 active sessions 都基于日志，
    离线照常展示。

    sessions_dir / codex_sessions_dir：会话身份来源目录，分别是 CC 注册表
    （默认 ~/.claude/sessions）与 Codex rollout（默认 ~/.codex/sessions）。CC 注册表
    是权威名，Codex 侧按 rollout 首行派生 `<目录名>-<uuid8 后两位>` 补位。两者都
    只在有 log_path 时扫描一次；显式参数供测试注入临时目录（不读真实 HOME）。

    forbidden_reference_token：可选的本次管理 token。若实际显示的身份或
    读取失败时展示的路径组件与其相同，抛 StatusReferenceConflict；缺省不改变原展示。
    """
    cooldown = data.get("cooldown", {})
    n_supplies = len(data.get("supplies", []))
    n_routes = len(data.get("routes", []))
    n_strategies = len(data.get("strategies", []))

    # config 计数 + mtime 合并行（head 区，紧贴进程态首行之后 bash 打印）。
    # mtime 由 formatter 从 config_path 自行 stat（单一来源，无需 CLI 传参）；
    # stat 失败（权限/竞态删除）时省略括号段而非报错。
    try:
        config_mtime = datetime.fromtimestamp(
            os.path.getmtime(config_path)).strftime("%m-%d %H:%M")
    except OSError:
        config_mtime = ""

    # config 读取（_supply_refs 需要；损坏时容错为空，refs 显示"未被引用"）
    cfg, cfg_err = _load_config_tolerant(config_path)
    if cfg_err and forbidden_reference_token is not None:
        # OSError text may include the config filename. Compare only actual
        # path components, and only when that path is rendered in the error;
        # ordinary error numbers and unrelated text are not identity values.
        rendered_path = os.fsdecode(os.fspath(config_path))
        components = rendered_path.replace("\\", "/").split("/")
        if (forbidden_reference_token in components and
                (rendered_path in cfg_err or repr(rendered_path) in cfg_err)):
            raise StatusReferenceConflict("status config error conflicts with admin token")

    # 供给统计：日志滚窗（与活跃 session 段同一份 tail，只读一次）。
    # 改用日志而非账本的原因：账本只有天粒度、无延迟字段、且失败码不入账；
    # 窗口统计能反映「现在」的状态，坏供给滑出窗口后自动消失。
    tail = tail_access_rows(log_path) if log_path else {"rows": [], "seek_tail": False,
                                                        "log_missing": True}
    supply_stats = load_supply_stats(tail["rows"]) if log_path else {}


    lines = []

    # config 合并行：计数 + mtime 一行（原尾部单行计数上移；config 损坏时
    # server 仍按最近一次成功加载的配置运行，计数照打、提示入行）
    if cfg_err:
        config_text = f"config: （{cfg_err}，server 按最近成功加载的配置运行）"
    else:
        mtime_part = f"（mtime {config_mtime}）" if config_mtime else ""
        config_text = f"config {n_supplies} supplies / {n_routes} routes / {n_strategies} strategies{mtime_part}"
    lines.append(config_text)

    # supply 引用标注（cooldown 行尾标出被哪个 route.tier(strategy) 引用）
    if forbidden_reference_token is None:
        refs = _supply_refs(cfg)
    else:
        visible_supply_ids = set(supply_stats)
        if not cooldown_unknown:
            visible_supply_ids.update(cooldown)
        refs = _supply_refs(cfg, forbidden_reference_token=forbidden_reference_token,
                            visible_supply_ids=visible_supply_ids)

    # 供给段（被 route 引用的全部供给，含健康项；未引用的折成一行）。
    # 即使 config 不可读也照常输出标题行：cooldown/degraded 计数来自进程态，
    # 与配置能否解析无关；丢掉这一行等于把现成的健康信号一起藏了。
    lines.append("")
    lines.extend(_format_supplies(
        cfg, supply_stats, cooldown=cooldown,
        stream_health=data.get("stream_health"),
        truncated=tail["seek_tail"],
        log_missing=(not log_path) or tail["log_missing"],
        refs=refs,
        forbidden_reference_token=forbidden_reference_token))
    if not cfg:
        lines.append("  （配置不可读，供给清单未知）")

    # active sessions 段（在线时恒展示；行首 name 标注取自 session 注册快照）
    if log_path:
        lines.append("")
        session_names = load_merged_session_names(sessions_dir, codex_sessions_dir)
        # 已读到的行直接复用；日志缺失时把 None 交给 load_active_sessions
        # 自行判定（它同样处理缺失，且保留可替换性供测试注入）。
        sessions = load_active_sessions(
            log_path,
            rows=None if tail["log_missing"] else tail["rows"],
            seek_tail=tail["seek_tail"])
        if forbidden_reference_token is None:
            lines.extend(_format_active_sessions(sessions, cfg, session_names))
        else:
            lines.extend(_format_active_sessions(sessions, cfg, session_names,
                                                forbidden_reference_token=forbidden_reference_token))

    # 2026-11-06：跨协议转换链路已系统性补齐（system/strict/encrypted
    # reasoning/工具/终态全实测），protocol conversions 专属展示段整体移除，
    # 不再把跨协议当作需要特别提示的特殊路径。

    if cooldown and not cooldown_unknown:
        lines.append("")
        lines.append("cooldown (剩余秒):")
        for sid, info in sorted(cooldown.items()):
            if isinstance(info, dict):
                remain = info.get("remain", 0)
                reason = info.get("reason", "")
            else:
                remain = info
                reason = ""
            if (forbidden_reference_token is not None and str(sid) == forbidden_reference_token):
                raise StatusReferenceConflict("visible supply identity conflicts with admin token")
            if (forbidden_reference_token is not None and not forbidden_reference_token.isdecimal()
                    and reason and str(reason) == forbidden_reference_token):
                raise StatusReferenceConflict("visible cooldown reason conflicts with admin token")
            ref = ",".join(refs.get(sid, [])) or "未被引用"
            suffix = f" ({reason})" if reason else ""
            lines.append(f"  {_pad(sid, 24)} {int(remain)}s  ← {ref}{suffix}")

    return lines


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("用法: _format_ops.py <status-format> <config_file> <totals_file>\n")
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "status-format":
        if len(sys.argv) < 4:
            sys.stderr.write("status-format 需要 config_file 和 totals_file 参数\n")
            sys.exit(1)
        config_path = sys.argv[2]
        totals_path = sys.argv[3]
        log_path = sys.argv[4] if len(sys.argv) >= 5 else None
        # stdin 读 server JSON，保留容错语义
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
        except Exception:
            # JSON 解析失败：原样透传（与现有内嵌 python 行为一致）
            print(raw, end="")
            sys.exit(0)
        if isinstance(data, dict) and "error" in data:
            print(f"Error: {data['error']}")
            sys.exit(0)
        for line in _format_status_from_json(data, config_path, totals_path, log_path):
            print(line)
        sys.exit(0)

    sys.stderr.write(f"未知子命令: {subcmd}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
