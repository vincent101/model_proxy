#!/usr/bin/env python3
"""model_proxy status 格式化单一来源（单源化，消除 CLI status 与菜单 list 双实现漂移）。

由 model_proxy_cli.sh 的 cmd_status 调用，也可被 _config_ops.py import。

两个 CLI 入口：
    python3 _format_ops.py status-format <config> <totals>   # stdin 读 server JSON
    python3 _format_ops.py status-offline <config> <totals>  # 直读 config + sidecar

格式化函数返回 list[str]（不直接 print），可被 unittest 覆盖。

约束：本模块只允许 import stdlib + core.commands.SessionOverridesSidecar（status-offline
取覆盖数用）。core.commands 必须保持纯 stdlib import 链——若未来 commands.py 引入重依赖，
会拖慢每次 CLI status 调用（与当前 fork 一个 python3 的开销同级）。
"""

import json
import os
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# core.commands 必须保持纯 stdlib import 链（已核实）。若未来引入重依赖会拖慢 CLI status。
from core.commands import SessionOverridesSidecar

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

    与 cmd_stats 的 parse_combo_key（model_proxy_cli.sh:483-488）同逻辑；
    heredoc 内嵌 python 无法 import，此为有注释互指的小重复。
    """
    dims = {}
    for part in key.split("|"):
        k, v = part.split("=", 1)
        dims[k] = v
    return dims


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


def _supply_refs(cfg: dict) -> dict[str, list[str]]:
    """supply_id → 引用它的 `route.tier(token,...)` 列表。

    用于在 degraded/cooldown 行尾标注该 supply 被哪个 route 的哪档、哪个 strategy 引用。
    """
    # route → 引用它的 strategy tokens（route_id 单值 + route_pool 两种写法）
    route_tokens: dict[str, list[str]] = {}
    for st in cfg.get("strategies", []):
        tok = st.get("client_token", "?")
        if st.get("route_id"):
            route_tokens.setdefault(st["route_id"], []).append(tok)
        for item in st.get("route_pool") or []:
            if isinstance(item, dict) and item.get("route_id"):
                route_tokens.setdefault(item["route_id"], []).append(tok)
    refs: dict[str, list[str]] = {}
    for r in cfg.get("routes", []):
        rid = r.get("id", "?")
        toks = route_tokens.get(rid, [])
        toks_str = f"({','.join(toks)})" if toks else ""
        for tn in TIER_NAMES:
            for sid in r.get("tiers", {}).get(tn, []):
                refs.setdefault(sid, []).append(f"{rid}.{tn}{toks_str}")
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
        "route": fields.get("route", ""),
        "tier": fields.get("tier", ""),
        "supply": fields.get("supply", ""),
        "failover": fields.get("failover", ""),
        "session": fields.get("session", ""),
        "route_failover": fields.get("route_failover", ""),
        "builtin": fields.get("builtin", ""),
        "final_error": fields.get("final_error", ""),
    }


def load_active_sessions(log_path: str, *, now: datetime | None = None,
                         window_minutes: int = 30,
                         tail_bytes: int = 2 * 1024 * 1024) -> dict:
    """tail 读日志末 tail_bytes，过滤 30min 窗口内 ACCESS 行，按 session 分组聚合。

    返回 ``{"sessions": {...}, "truncated": bool, "log_missing": bool}``。

    - builtin=route 行计入活跃、不计入统计（n/fail/fo）
    - 空串 session 聚成 ``(none)`` 桶
    - 截断检测：文件 > tail_bytes 且 buffer 内首条可解析行 ts 已在窗口内
    """
    if not os.path.isfile(log_path):
        return {"sessions": {}, "truncated": False, "log_missing": True}
    if now is None:
        now = datetime.now()
    window_start = now - timedelta(minutes=window_minutes)

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
        return {"sessions": {}, "truncated": False, "log_missing": True}

    lines_decode = raw.decode("utf-8", errors="replace").splitlines()
    sessions: dict[str, dict] = {}
    first_ts_in_buffer: datetime | None = None

    for line in lines_decode:
        parsed = parse_access_line(line)
        if parsed is None:
            continue
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
        })
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
            if parsed["status"] != "200":
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
            if agg["last_ts"] is None or parsed["ts"] > agg["last_ts"]:
                agg["last_ts"] = parsed["ts"]
                agg["last_status"] = parsed["status"]
                agg["last_route"] = parsed["route"]
                agg["last_tier"] = parsed["tier"]
                agg["last_supply"] = parsed["supply"]
                agg["last_error"] = parsed["final_error"]
                agg["last_req_id"] = parsed["req_id"]

    # 截断判定：buffer 首条可解析行已在窗口内 → 窗口起点可能在 buffer 外
    truncated = bool(
        seek_tail and first_ts_in_buffer
        and first_ts_in_buffer >= window_start
    )

    return {"sessions": sessions, "truncated": truncated, "log_missing": False}


def _format_active_sessions(result: dict) -> list[str]:
    """渲染活跃 session 段（header + 每 session 一行 + FAIL err 续行）。

    排序：FAIL → warn → ok，同档按最近请求时间倒序。行 ≤80 列。
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

    lines = [header]

    # 上限 20 行
    max_rows = 20
    shown = entries[:max_rows]

    for sid, agg, state in shown:
        id_str = "(none)" if sid == "(none)" else sid[:8]
        fo_part = f" fo={agg['fo']}" if agg["fo"] > 0 else ""
        ts_str = agg["last_ts"].strftime("%H:%M") if agg["last_ts"] else "--:--"

        if agg["builtin_only"]:
            line = (f"  {_pad(id_str, 8)}  {_pad(state, 4)} n=0 fail=0"
                    f"  {ts_str} 200"
                    f"  {agg['last_route']}/{agg['last_tier']}/{agg['last_supply']}"
                    f"  （仅 $route)")
        else:
            line = (f"  {_pad(id_str, 8)}  {_pad(state, 4)}"
                    f" n={agg['n']} fail={agg['fail']}{fo_part}"
                    f"  {ts_str} {agg['last_status']}"
                    f"  {agg['last_route']}/{agg['last_tier']}/{agg['last_supply']}")
        lines.append(line)

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


def format_strategies(strategies: list[dict], *, style: str,
                      override_counts: dict[str, int] | None = None) -> list[str]:
    """格式化 strategies。

    style="menu": 保持现有单行 {tok} -> {rid} ({note})，不传 override_counts。
        override_counts 参数保留以兼容调用方签名，menu 分支不使用。

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


def _format_status_from_json(data: dict, config_path: str, totals_path: str,
                             log_path: str | None = None) -> list[str]:
    """从 server status JSON + config + 账本 格式化 status 输出。

    布局：health 行 → active sessions 段（在线时恒展示）→ 异常清单
    （degraded/unmatched/cooldown/damaged/config notices）→ config 计数行。
    全 0 时 health 行即"系统健康"，异常段不打印。
    """
    cooldown = data.get("cooldown", {})
    n_supplies = len(data.get("supplies", []))
    n_routes = len(data.get("routes", []))
    n_strategies = len(data.get("strategies", []))
    default_cd = data.get("default_cooldown_seconds", "?")

    # overrides 求和
    overrides_count = sum(
        st.get("sidecar_overrides_count", 0) or 0
        for st in data.get("strategies", [])
    )

    # config 读取（find_damaged_routes 需要）
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # supply health
    health = load_supply_health(totals_path)
    degraded = _compute_degraded(health)
    n_degraded = len(degraded)

    # (none) unmatched
    none_health = health.get("(none)")
    none_fail = none_health.get("fail", 0) if none_health else 0

    n_cooldown = len(cooldown)

    lines = []

    # health 行
    parts = [f"cooldown {n_cooldown}/{n_supplies}"]
    if n_degraded > 0:
        parts.append(f"degraded {n_degraded}")
    else:
        parts.append("degraded 0")
    parts.append(f"overrides {overrides_count}")
    lines.append("health: " + " · ".join(parts))

    # active sessions 段（在线时恒展示）
    if log_path:
        lines.append("")
        lines.extend(_format_active_sessions(load_active_sessions(log_path)))

    # supply 引用标注（degraded/cooldown 行尾标出被哪个 route.tier(strategy) 引用）
    refs = _supply_refs(cfg)

    # 异常清单（只列问题）
    if degraded:
        lines.append("")
        lines.append(f"degraded supplies (today fail%>{DEGRADED_FAIL_PCT:.0f}%, n>={DEGRADED_MIN_REQUESTS}):")
        for d in degraded:
            ref = ",".join(refs.get(d["id"], [])) or "未被引用"
            lines.append(f"  {_pad(d['id'], 24)} fail {d['fail_pct']:.1f}% ({d['fail']}/{d['requests']})  ← {ref}")

    if none_fail > 0:
        none_req = none_health.get("requests", 0)
        lines.append("")
        lines.append(f"unmatched: {none_req} req 今日全失败（supply=(none)，未匹配 strategy/route，多为 401）")

    if cooldown:
        lines.append("")
        lines.append("cooldown (剩余秒):")
        for sid, remain in sorted(cooldown.items()):
            ref = ",".join(refs.get(sid, [])) or "未被引用"
            lines.append(f"  {_pad(sid, 24)} {int(remain)}s  ← {ref}")

    # config 计数行
    lines.append("")
    lines.append(f"config: {n_supplies} supplies / {n_routes} routes / {n_strategies} strategies")
    lines.append("       （明细: supply / route / strategy 菜单 list；今日明细: stats today supply）")

    return lines


def _format_status_offline(config_path: str, totals_path: str) -> list[str]:
    """代理未运行时的降级展示。

    cooldown/degraded 显 (代理未运行) 且不读账本（避免历史值误导为当前态）；
    overrides/orphan/config 计数静态照常；退出码 1 由 cmd_status 保持。
    """
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    n_supplies = len(cfg.get("supplies", []))
    n_routes = len(cfg.get("routes", []))
    n_strategies = len(cfg.get("strategies", []))
    default_cd = cfg.get("default_cooldown_seconds", "?")

    # overrides: sidecar 静态可读
    sidecar_path = Path(config_path).parent / "session_overrides.json"
    sidecar = SessionOverridesSidecar(sidecar_path)
    overrides_count = sum(
        sidecar.count_overrides_for(st.get("client_token", ""))
        for st in cfg.get("strategies", [])
    )

    lines = []
    parts = ["cooldown (代理未运行)", "degraded (代理未运行)"]
    parts.append(f"overrides {overrides_count}")
    lines.append("health: " + " · ".join(parts))

    # config 计数行
    lines.append("")
    lines.append(f"config: {n_supplies} supplies / {n_routes} routes / {n_strategies} strategies")
    lines.append("       （明细: supply / route / strategy 菜单 list）")

    return lines


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("用法: _format_ops.py <status-format|status-offline> <config_file> <totals_file>\n")
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

    if subcmd == "status-offline":
        if len(sys.argv) < 4:
            sys.stderr.write("status-offline 需要 config_file 和 totals_file 参数\n")
            sys.exit(1)
        config_path = sys.argv[2]
        totals_path = sys.argv[3]
        if not os.path.isfile(config_path):
            sys.stderr.write(f"Error: config not found: {config_path}\n")
            sys.exit(1)
        for line in _format_status_offline(config_path, totals_path):
            print(line)
        sys.exit(0)

    sys.stderr.write(f"未知子命令: {subcmd}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
