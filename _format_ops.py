#!/usr/bin/env python3
"""model_proxy status 格式化单一来源（单源化，消除 CLI status 与菜单 list 双实现漂移）。

由 model_proxy_cli.sh 的 cmd_status 调用，也可被 _config_ops.py import。

两个 CLI 入口：
    python3 _format_ops.py status-format           # stdin 读 server JSON，打印五段
    python3 _format_ops.py status-offline <config>  # 直读 config + sidecar，打印静态三段

格式化函数返回 list[str]（不直接 print），可被 unittest 覆盖。

约束：本模块只允许 import stdlib + core.commands.SessionOverridesSidecar（status-offline
取覆盖数用）。core.commands 必须保持纯 stdlib import 链——若未来 commands.py 引入重依赖，
会拖慢每次 CLI status 调用（与当前 fork 一个 python3 的开销同级）。
"""

import json
import os
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# core.commands 必须保持纯 stdlib import 链（已核实）。若未来引入重依赖会拖慢 CLI status。
from core.commands import SessionOverridesSidecar

TIER_NAMES = ("opus", "sonnet", "haiku")


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
# 格式化函数（返回 list[str]，不直接 print）
# ---------------------------------------------------------------------------

def format_supplies(supplies: list[dict], *, preset: str) -> list[str]:
    """动态列宽格式化 supplies。

    preset="STATUS": (id, protocol, model, key)，裸值无标签（仅 key 带 key= 前缀）。
        实测最坏 71 列 <=80。
    preset="MENU": (id, protocol, model, key, rcap, cooldown)，保留带标签样式。
        菜单是交互宽屏场景，不受 80 列约束。
    """
    rows = [normalize_supply(s) for s in supplies]
    if not rows:
        return ["  (无)"]

    if preset == "STATUS":
        cols = ["id", "protocol", "model", "key_masked"]
        labels = ["", "", "", "key="]
        # 动态列宽：每列取最大展示宽度（含 label 前缀）
        col_widths = []
        for i, col in enumerate(cols):
            mw = max(display_width(labels[i] + r[col]) for r in rows)
            col_widths.append(mw)
        lines = []
        for r in rows:
            parts = []
            for i, col in enumerate(cols):
                val = labels[i] + r[col]
                parts.append(_pad(val, col_widths[i]))
            line = "  " + "  ".join(parts).rstrip()
            lines.append(line)
        return lines

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

    style="status": 每个 strategy 两行——route 归属 + 覆盖/note 行。
        覆盖行无条件打印（S3）：0 时 (无)，>0 时含来源标注。
        override_counts: {client_token: count}，从 sidecar 或 server JSON 取。
    style="menu": 保持现有单行 {tok} -> {rid} ({note})，不传 override_counts。
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

    if style == "status":
        lines = []
        for st in strategies:
            tok = st.get("client_token", "?")
            rid = strategy_route_desc(st)
            note = st.get("note", "") or ""
            count = 0
            if override_counts:
                count = override_counts.get(tok, 0) or 0
            # S3: 覆盖行无条件打印（S4: 拼接在 route_id/pool 分支之外）
            if count > 0:
                cov = (f"覆盖: {count}个session"
                       f"（来源: sidecar，由会话内 $route 指令产生）")
            else:
                cov = "覆盖: (无)"
            lines.append(f"  {_pad(tok, 16)} -> {rid}")
            lines.append(f"      {cov}   note: {note}")
        return lines

    raise ValueError(f"unknown style: {style}")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _format_status_from_json(data: dict) -> list[str]:
    """从 server status JSON 格式化五段输出。"""
    lines = []
    lines.append("supplies:")
    lines.extend(format_supplies(data.get("supplies", []), preset="STATUS"))

    lines.append("routes (家族模板):")
    lines.extend(format_routes(data.get("routes", [])))

    lines.append("strategies (token 绑定):")
    # server JSON 里 strategies 已含 sidecar_overrides_count
    override_counts = {}
    for st in data.get("strategies", []):
        tok = st.get("client_token", "")
        count = st.get("sidecar_overrides_count", 0) or 0
        override_counts[tok] = count
    lines.extend(format_strategies(data.get("strategies", []),
                                  style="status", override_counts=override_counts))

    cooldown = data.get("cooldown", {})
    if cooldown:
        lines.append("cooldown (剩余秒):")
        for sid, remain in cooldown.items():
            lines.append(f"  {_pad(sid, 20)} {remain}s")
    else:
        lines.append("cooldown: (无)")
    lines.append(f"default_cooldown_seconds: {data.get('default_cooldown_seconds', '?')}")

    return lines


def _format_status_offline(config_path: str) -> list[str]:
    """直读 config + sidecar 格式化静态三段（S10: 代理未运行时的降级展示）。"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    lines = []
    lines.append("supplies:")
    lines.extend(format_supplies(cfg.get("supplies", []), preset="STATUS"))

    lines.append("routes (家族模板):")
    lines.extend(format_routes(cfg.get("routes", [])))

    lines.append("strategies (token 绑定):")
    # 经 SessionOverridesSidecar 取覆盖数（复用类的文件缺失/损坏语义）
    sidecar_path = Path(config_path).parent / "session_overrides.json"
    sidecar = SessionOverridesSidecar(sidecar_path)
    override_counts = {}
    for st in cfg.get("strategies", []):
        tok = st.get("client_token", "")
        override_counts[tok] = sidecar.count_overrides_for(tok)
    lines.extend(format_strategies(cfg.get("strategies", []),
                                  style="status", override_counts=override_counts))

    lines.append("cooldown: (代理未运行)")
    lines.append(f"default_cooldown_seconds: {cfg.get('default_cooldown_seconds', '?')}")

    return lines


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("用法: _format_ops.py <status-format|status-offline> [config_file]\n")
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "status-format":
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
        for line in _format_status_from_json(data):
            print(line)
        sys.exit(0)

    if subcmd == "status-offline":
        if len(sys.argv) < 3:
            sys.stderr.write("status-offline 需要 config_file 参数\n")
            sys.exit(1)
        config_path = sys.argv[2]
        if not os.path.isfile(config_path):
            sys.stderr.write(f"Error: config not found: {config_path}\n")
            sys.exit(1)
        for line in _format_status_offline(config_path):
            print(line)
        sys.exit(0)

    sys.stderr.write(f"未知子命令: {subcmd}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
