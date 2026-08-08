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


def find_damaged_routes(cfg: dict, bad_supplies: set[str], cooldown: dict) -> list[str]:
    """tier 内含 degraded∪cooling supply 的 route，输出描述行。

    bad_supplies: degraded supply id 集合（不含 (none)）
    cooldown: server JSON cooldown dict {supply_id: 剩余秒}
    """
    bad_set = bad_supplies | set(cooldown.keys())
    if not bad_set:
        return []

    results = []
    for r in cfg.get("routes", []):
        rid = r.get("id", "?")
        tiers = r.get("tiers", {})
        hits: list[str] = []
        for tn in TIER_NAMES:
            ids = tiers.get(tn, [])
            bad_in_tier = [sid for sid in ids if sid in bad_set]
            if bad_in_tier:
                # 区分 degraded / cooling
                parts = []
                for sid in bad_in_tier:
                    if sid in cooldown:
                        parts.append(f"{sid} cooling({int(cooldown[sid])}s)")
                    elif sid in bad_supplies:
                        parts.append(f"{sid} degraded")
                    else:
                        parts.append(f"{sid} abnormal")
                hits.append(f"{tn} 档 {', '.join(parts)}")
        if hits:
            results.append(f"  {rid}   {'; '.join(hits)}")
    return results


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


def _format_status_from_json(data: dict, config_path: str, totals_path: str) -> list[str]:
    """从 server status JSON + config + 账本 格式化 status 输出。

    布局：health 行 → 异常清单（degraded/unmatched/cooldown/damaged/config notices）
    → config 计数行。全 0 时 health 行即"系统健康"，异常段不打印。
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

    # 异常清单（只列问题）
    if degraded:
        lines.append("")
        lines.append(f"degraded supplies (today fail%>{DEGRADED_FAIL_PCT:.0f}%, n>={DEGRADED_MIN_REQUESTS}):")
        for d in degraded:
            lines.append(f"  {_pad(d['id'], 24)} fail {d['fail_pct']:.1f}% ({d['fail']}/{d['requests']})")

    if none_fail > 0:
        none_req = none_health.get("requests", 0)
        lines.append("")
        lines.append(f"unmatched: {none_req} req 今日全失败（supply=(none)，未匹配 strategy/route，多为 401）")

    if cooldown:
        lines.append("")
        lines.append("cooldown (剩余秒):")
        for sid, remain in sorted(cooldown.items()):
            lines.append(f"  {_pad(sid, 24)} {int(remain)}s")

    # damaged routes
    degraded_ids = {d["id"] for d in degraded}
    damaged = find_damaged_routes(cfg, degraded_ids, cooldown)
    if damaged:
        lines.append("")
        lines.append("damaged routes:")
        lines.extend(damaged)

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
        for line in _format_status_from_json(data, config_path, totals_path):
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
