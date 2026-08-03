#!/bin/bash
# model_proxy_cli.sh
# 手动控制 model_proxy.py（http://127.0.0.1:18889/model_proxy/*）。
# 与 tools/proxy_cli.sh（v1，18888）完全独立，不共用进程/端口/配置。
# 用法：model_proxy_cli.sh <子命令> [参数]

MODEL_PROXY_PORT="${MODEL_PROXY_PORT:-18889}"
MODEL_PROXY_BASE="http://127.0.0.1:${MODEL_PROXY_PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${MODEL_PROXY_CONFIG:-$SCRIPT_DIR/config/model_proxy_config.json}"
LOG_FILE="$SCRIPT_DIR/.claude_model_proxy.log"
TOTALS_FILE="$SCRIPT_DIR/.claude_model_proxy_totals.json"
LOCK_FILE="/tmp/claude_model_proxy.lock"
CONFIG_OPS="$SCRIPT_DIR/_config_ops.py"
INSTALL_OPS="$SCRIPT_DIR/_install_ops.py"

# ---- 帮助信息 ----
print_help() {
  cat <<EOF
用法: model_proxy_cli.sh <子命令> [参数]

status                            显示运行状态 + supplies/routes/cooldown 概览
reload                            触发配置热重载（无条件清空所有 cooldown）

supply                            打印 supply list 后进入交互菜单，可选操作：
                                    [a]dd  交互式新增 supply（同步探测 effort，写配置后 reload）
                                    [e]dit 交互式编辑 supply（含改 appkey、可选重新探测 effort）
                                    [d]el  删除 supply（二次确认，被 route 引用则拒绝）
                                    [t]est 连通性测试；若连通，接着做 effort 探测并可确认写入 reasoning_capability
                                    [q]uit 退出（回车亦可）

route                             打印 route list 后进入交互菜单，可选操作：
                                    [a]dd  交互式新增 route 家族模板（opus/sonnet/haiku 三档 + failover）
                                    [e]dit 交互式编辑 route 的 tiers/failover
                                    [d]el  删除 route（二次确认，被 strategy 引用则拒绝）
                                    [q]uit 退出（回车亦可）

strategy                          打印 strategy list 后进入交互菜单，可选操作：
                                    [a]dd  交互式新增 strategy 绑定（client_token -> route_id，写配置后 reload）
                                    [e]dit 交互式编辑 strategy 的 route_id/note/source 能力
                                    [d]el  删除 strategy（二次确认，无下游引用检查）
                                    [q]uit 退出（回车亦可）

switch <client_token> <route_id>  切换某 token 绑定的 route 家族（改 strategy.route_id 后 reload）
install                           交互式列出四个 SDK + 本机检测状态，选择安装（不选直接回车即为只看状态退出）
on                                启动 model_proxy.py（已在监听则跳过）
off                               停止 model_proxy.py（严格按脚本绝对路径匹配，绝不影响 v1 的 proxy.py）

logs [N]                          显示最近 N 条 ACCESS 访问日志（默认 30 条）
stats [时间] [维度/过滤...]        读独立账本（不受日志截断影响），按 supply/route/strategy
                                    任意维度组合切片。用法示例：
                                      stats                          全历史 total，三维度各投影一段
                                      stats today                    今天（UTC+8）
                                      stats month                    本月（UTC+8）
                                      stats 2026-07-23                指定某天
                                      stats 2026-07                   指定某月
                                      stats today supply              按 supply 投影
                                      stats today supply=<id>         过滤单个 supply
                                      stats today route=claude supply 过滤 route 后按 supply 投影
                                    末尾附一行 max_ms（来自日志，受日志窗口限制，非账本口径）
--help / -h                       显示此帮助

说明: supply/route/strategy/install 的增删改等操作只能通过对应一级入口进入交互菜单执行，
      不再支持带子命令直达（如 supply add）。非交互（stdin 非 TTY）环境下进入这些入口只打印
      一次 list 后直接退出，不进菜单。
EOF
}

# ---- 从 config 读 admin_token ----
get_admin_token() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('admin_token',''))" "$CONFIG_FILE" 2>/dev/null
}

# ---- 带鉴权的 curl 封装 ----
model_proxy_api() {
  local method="$1" path="$2" data="$3"
  local token
  token=$(get_admin_token)
  local args=(-s -X "$method" -H "X-Proxy-Admin-Token: $token" "$MODEL_PROXY_BASE$path")
  [[ -n "$data" ]] && args+=(-H "Content-Type: application/json" -d "$data")
  curl "${args[@]}"
}

# ---- reload 封装（手动 reload：无条件清空所有 cooldown，见 _handle_reload） ----
reload_proxy() {
  local out
  out=$(model_proxy_api POST /model_proxy/reload)
  echo "Reloaded (cooldown 已清空): $out"
}

# ---- 调用 _config_ops.py 的通用封装 ----
# 用临时文件传递 __RELOAD__ 标记（而非 stdout，避免破坏 python 侧 input() 的交互实时性）。
run_config_ops() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi
  local subcmd="$1"
  shift
  local marker
  marker=$(mktemp)
  CONFIG_OPS_RELOAD_MARKER="$marker" python3 "$CONFIG_OPS" "$subcmd" "$CONFIG_FILE" "$@"
  local rc=$?
  if [[ -f "$marker" ]] && [[ "$(cat "$marker")" == "yes" ]]; then
    reload_proxy
  fi
  rm -f "$marker"
  return $rc
}

# ---- status ----
cmd_status() {
  if lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN -t &>/dev/null; then
    echo "model_proxy: running on port $MODEL_PROXY_PORT"
  else
    echo "model_proxy: NOT running on port $MODEL_PROXY_PORT"
    return 1
  fi

  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi

  local out
  out=$(model_proxy_api GET /model_proxy/status)
  if [[ -z "$out" ]]; then
    echo "Error: model_proxy not responding"
    return 1
  fi
  echo "$out" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(sys.stdin.read())
    sys.exit(0)
if 'error' in data:
    print(f\"Error: {data['error']}\")
    sys.exit(0)
print('supplies:')
for s in data.get('supplies', []):
    sid = s.get('id', '?')
    proto = s.get('protocol', '?')
    tail4 = s.get('appkey_tail4', '????')
    model = s.get('target_model', '?')
    print(f'  {sid:20} protocol={proto:10} model={model:20} appkey=...{tail4}')
print('routes (家族模板):')
for r in data.get('routes', []):
    rid = r.get('id', '?')
    tiers = r.get('tiers', {})
    opus = ','.join(tiers.get('opus', []))
    sonnet = ','.join(tiers.get('sonnet', []))
    haiku = ','.join(tiers.get('haiku', []))
    failover = r.get('failover', '?')
    print(f'  {rid:12} opus=[{opus}] sonnet=[{sonnet}] haiku=[{haiku}] failover={failover}')
print('strategies (token 绑定):')
for st in data.get('strategies', []):
    tok = st.get('client_token', '?')
    route_id = st.get('route_id')
    route_pool = st.get('route_pool')
    if route_id:
        rid_desc = route_id
    elif route_pool:
        pool_desc = ','.join(
            f\"{p.get('route_id', '?')}:{p.get('weight', 1)}\" for p in route_pool
        )
        overrides = ((st.get('dispatch') or {}).get('session_overrides')) or {}
        rid_desc = f'pool[{pool_desc}]'
        if overrides:
            rid_desc += f' +{len(overrides)}个session覆盖'
    else:
        rid_desc = '?'
    print(f'  {tok:16} -> {rid_desc:12}')
cooldown = data.get('cooldown', {})
if cooldown:
    print('cooldown (剩余秒):')
    for sid, remain in cooldown.items():
        print(f'  {sid:20} {remain}s')
else:
    print('cooldown: (无)')
print(f\"default_cooldown_seconds: {data.get('default_cooldown_seconds', '?')}\")
"
}

# ---- reload ----
cmd_reload() {
  reload_proxy
}

# ---- supply：单动作函数（供旧子命令分发与新交互菜单共同复用） ----
cmd_supply_list()  { run_config_ops supply-list; }
cmd_supply_add()   { run_config_ops supply-add; }
cmd_supply_edit()  {
  local id="${1:-}"
  if [[ -z "$id" ]]; then echo "用法: supply edit <id>"; return 1; fi
  run_config_ops supply-edit "$id"
}
cmd_supply_del()   {
  local id="${1:-}"
  if [[ -z "$id" ]]; then echo "用法: supply del <id>"; return 1; fi
  run_config_ops supply-del "$id"
}
cmd_supply_check() {
  local id="${1:-}"
  if [[ -z "$id" ]]; then echo "用法: supply test <id>"; return 1; fi
  run_config_ops supply-check "$id"
}

# ---- supply：入口。进入交互菜单（先打印 list，再循环选操作）。 ----
cmd_supply() {
  cmd_supply_list
  if [[ ! -t 0 ]]; then
    echo ""
    echo "当前非交互环境（stdin 非 TTY），不支持交互操作，退出。"
    return 0
  fi
  echo ""
  echo "可选操作: a=新增 supply / e=编辑 supply / d=删除 supply / t=连通性测试+effort探测 / q=退出"
  while true; do
    echo ""
    read -p "操作: [a]dd / [e]dit / [d]el / [t]est / [q]uit: " op
    case "$op" in
      a) cmd_supply_add ;;
      e) read -p "要编辑的 supply id: " eid; cmd_supply_edit "$eid" ;;
      d) read -p "要删除的 supply id: " did; cmd_supply_del "$did" ;;
      t) read -p "要测试的 supply id: " tid; cmd_supply_check "$tid" ;;
      q|"") break ;;
      *) echo "未知操作" ;;
    esac
    # 130 == 128+SIGINT：子进程内 Ctrl-C 被 python 捕获后约定用这个退出码，
    # 这里识别到就整体退出菜单（回到 shell），而不是继续问下一轮操作。
    if [[ $? -eq 130 ]]; then break; fi
    echo ""
    cmd_supply_list
  done
}

# ---- route：单动作函数 ----
cmd_route_list() { run_config_ops route-list; }
cmd_route_add()  { run_config_ops route-add; }
cmd_route_edit() {
  local id="${1:-}"
  if [[ -z "$id" ]]; then echo "用法: route edit <id>"; return 1; fi
  run_config_ops route-edit "$id"
}
cmd_route_del()  {
  local id="${1:-}"
  if [[ -z "$id" ]]; then echo "用法: route del <id>"; return 1; fi
  run_config_ops route-del "$id"
}

# ---- route：入口。进入交互菜单（先打印 list，再循环选操作）。 ----
cmd_route() {
  cmd_route_list
  if [[ ! -t 0 ]]; then
    echo ""
    echo "当前非交互环境（stdin 非 TTY），不支持交互操作，退出。"
    return 0
  fi
  echo ""
  echo "可选操作: a=新增 route 家族 / e=编辑 route / d=删除 route / q=退出"
  while true; do
    echo ""
    read -p "操作: [a]dd / [e]dit / [d]el / [q]uit: " op
    case "$op" in
      a) cmd_route_add ;;
      e) read -p "要编辑的 route id: " eid; cmd_route_edit "$eid" ;;
      d) read -p "要删除的 route id: " did; cmd_route_del "$did" ;;
      q|"") break ;;
      *) echo "未知操作" ;;
    esac
    if [[ $? -eq 130 ]]; then break; fi
    echo ""
    cmd_route_list
  done
}

# ---- strategy：单动作函数 ----
cmd_strategy_list() { run_config_ops strategy-list; }
cmd_strategy_add()  { run_config_ops strategy-add; }
cmd_strategy_edit() {
  local token="${1:-}"
  if [[ -z "$token" ]]; then echo "用法: strategy edit <token>"; return 1; fi
  run_config_ops strategy-edit "$token"
}
cmd_strategy_del()  {
  local token="${1:-}"
  if [[ -z "$token" ]]; then echo "用法: strategy del <token>"; return 1; fi
  run_config_ops strategy-del "$token"
}

# ---- strategy：入口。进入交互菜单（先打印 list，再循环选操作）。 ----
cmd_strategy() {
  cmd_strategy_list
  if [[ ! -t 0 ]]; then
    echo ""
    echo "当前非交互环境（stdin 非 TTY），不支持交互操作，退出。"
    return 0
  fi
  echo ""
  echo "可选操作: a=新增 strategy 绑定 / e=编辑 strategy / d=删除 strategy / q=退出"
  while true; do
    echo ""
    read -p "操作: [a]dd / [e]dit / [d]el / [q]uit: " op
    case "$op" in
      a) cmd_strategy_add ;;
      e) read -p "要编辑的 strategy token: " etok; cmd_strategy_edit "$etok" ;;
      d) read -p "要删除的 strategy token: " dtok; cmd_strategy_del "$dtok" ;;
      q|"") break ;;
      *) echo "未知操作" ;;
    esac
    if [[ $? -eq 130 ]]; then break; fi
    echo ""
    cmd_strategy_list
  done
}

# ---- switch ----
cmd_switch() {
  local stoken="$1" srid="$2"
  if [[ -z "$stoken" || -z "$srid" ]]; then
    echo "用法: switch <client_token> <route_id>"
    return 1
  fi
  run_config_ops switch "$stoken" "$srid"
}

# ---- install ----
cmd_install() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi
  python3 "$INSTALL_OPS" install "$CONFIG_FILE" "$MODEL_PROXY_PORT"
}

# ---- on ----
cmd_on() {
  if lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN -t &>/dev/null; then
    echo "model_proxy already running on port $MODEL_PROXY_PORT"
    return 0
  fi
  echo "Starting model_proxy.py on port $MODEL_PROXY_PORT..."
  MODEL_PROXY_PORT="$MODEL_PROXY_PORT" nohup python3 "$SCRIPT_DIR/model_proxy.py" >> "$LOG_FILE" 2>&1 &
  local pid=$!

  for i in {1..10}; do
    sleep 0.5
    if lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN -t &>/dev/null; then
      echo "model_proxy started (pid $pid), ready (${i} * 0.5s)"
      return 0
    fi
  done

  echo "WARNING: model_proxy failed to start on port $MODEL_PROXY_PORT. Last log:"
  tail -5 "$LOG_FILE"
  return 1
}

# ---- off ----
# 安全约束：只匹配本脚本同目录下 model_proxy.py 的绝对路径，绝不使用宽泛的
# "model_proxy.py" 或 "proxy.py" 之类的模式，避免误杀 v1 的 tools/proxy.py（18888 生产进程）。
#
# 兜底（P1-3）：若 model_proxy.py 是以相对路径启动的（不经本脚本的 on），命令行里不含
# $SCRIPT_DIR 绝对路径，pgrep -f "$target" 匹配不到，会导致 off 误报"未运行"而
# 实际进程仍占用 $MODEL_PROXY_PORT。这里额外反查监听该端口的 PID，但必须校验其命令行
# 确实包含 "model_proxy.py"（文件名级校验，仍不会匹配 v1 的 proxy.py）才纳入 kill 范围。
cmd_off() {
  local target="$SCRIPT_DIR/model_proxy.py"
  local pids
  pids=$(pgrep -f "$target" 2>/dev/null)

  local port_pid cmdline
  for port_pid in $(lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN -t 2>/dev/null); do
    cmdline=$(ps -p "$port_pid" -o command= 2>/dev/null)
    if [[ "$cmdline" == *"model_proxy.py"* ]]; then
      pids=$(printf '%s\n%s' "$pids" "$port_pid")
    fi
  done
  pids=$(printf '%s\n' "$pids" | grep -v '^$' | sort -u)

  if [[ -z "$pids" ]]; then
    echo "model_proxy not running (no process matching: $target, no model_proxy.py listener on port $MODEL_PROXY_PORT)"
    return 0
  fi
  echo "$pids" | while read -r pid; do
    kill "$pid" 2>/dev/null && echo "Stopped model_proxy (pid $pid)" || echo "Failed to kill pid $pid"
  done
}

# ---- logs：最近 N 条 ACCESS 记录（默认 30） ----
cmd_logs() {
  local n="${1:-30}"
  grep ' ACCESS ' "$LOG_FILE" | tail -n "$n"
}

# ---- stats：读独立账本（$TOTALS_FILE），组合键投影+过滤聚合；末尾补日志窗口内 max ms ----
# 用法见 print_help；核心逻辑：选时间桶 -> 按 字段=值 过滤 -> 按裸维度名投影聚合 -> 打印。
cmd_stats() {
  if [[ ! -f "$TOTALS_FILE" ]]; then
    echo "no stats yet (账本文件不存在: $TOTALS_FILE)"
    return 0
  fi

  python3 - "$TOTALS_FILE" "$@" <<'PYEOF'
import json
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
DIMS = ("supply", "route", "strategy")
VAL_FIELDS = ("requests", "ok", "fail", "usage_in", "usage_out")


def zero_bucket():
    return {"requests": 0, "ok": 0, "fail": 0, "sum_ms": 0, "combos": {}}


def zero_group():
    return {k: 0 for k in VAL_FIELDS}


def cst_today_str():
    return datetime.now(CST).strftime("%Y-%m-%d")


def merge_bucket_into(dst, src):
    dst["requests"] += src.get("requests", 0)
    dst["ok"] += src.get("ok", 0)
    dst["fail"] += src.get("fail", 0)
    dst["sum_ms"] += src.get("sum_ms", 0)
    for key, v in src.get("combos", {}).items():
        combo = dst["combos"].setdefault(key, zero_group())
        for f in VAL_FIELDS:
            combo[f] += v.get(f, 0)


def get_month_bucket(data, month_key):
    """无条件合并 months_archive[月] 与 days 里剩余同月天桶。
    二者互斥不重叠（归档时天桶已从 days 删除，见 core/server.py _archive_if_needed），
    合并后即为完整月度数据，不会重复计算。"""
    merged = zero_bucket()
    archived = data.get("months_archive", {}).get(month_key)
    if archived is not None:
        merge_bucket_into(merged, archived)
    for day_key, day_bucket in data.get("days", {}).items():
        if day_key[:7] == month_key:
            merge_bucket_into(merged, day_bucket)
    return merged


def select_bucket(data, time_sel):
    """返回 (bucket_dict, label)。bucket_dict 含 requests/ok/fail/sum_ms/combos。"""
    if time_sel is None or time_sel == "total":
        return data.get("total", zero_bucket()), "total (全历史)"
    if time_sel == "today":
        day_key = cst_today_str()
        return data.get("days", {}).get(day_key, zero_bucket()), f"{day_key} (today, UTC+8)"
    if time_sel == "month":
        month_key = cst_today_str()[:7]
        bucket = get_month_bucket(data, month_key)
        return bucket, f"{month_key} (month, UTC+8)"
    if len(time_sel) == 10 and time_sel.count("-") == 2:
        return data.get("days", {}).get(time_sel, zero_bucket()), f"{time_sel} (UTC+8)"
    if len(time_sel) == 7 and time_sel.count("-") == 1:
        bucket = get_month_bucket(data, time_sel)
        return bucket, f"{time_sel} (UTC+8)"
    print(f"Error: 无法识别的时间选择器: {time_sel!r}", file=sys.stderr)
    sys.exit(1)


def parse_combo_key(key):
    dims = {}
    for part in key.split("|"):
        k, v = part.split("=", 1)
        dims[k] = v
    return dims


def fmt_k(n):
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def main():
    argv = sys.argv[1:]
    totals_file = argv[0]
    args = argv[1:]

    with open(totals_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 第 1 个非时间参数如果匹配时间选择器格式则消费为时间选择器，其余全部是维度/过滤参数。
    time_sel = None
    dim_args = args
    if args:
        first = args[0]
        is_time_like = (
            first in ("today", "month", "total")
            or (len(first) == 10 and first.count("-") == 2)
            or (len(first) == 7 and first.count("-") == 1)
        )
        if is_time_like:
            time_sel = first
            dim_args = args[1:]

    bucket, label = select_bucket(data, time_sel)

    filters = {}
    proj = None
    for tok in dim_args:
        if "=" in tok:
            field, val = tok.split("=", 1)
            if field not in DIMS:
                print(f"Error: 未知过滤字段: {field!r}（应为 supply/route/strategy）", file=sys.stderr)
                sys.exit(1)
            filters[field] = val
        elif tok in DIMS:
            proj = tok
        else:
            print(f"Error: 无法识别的参数: {tok!r}", file=sys.stderr)
            sys.exit(1)

    combos = bucket.get("combos", {})

    def aggregate(proj_dim):
        groups = {}
        for key, v in combos.items():
            dims = parse_combo_key(key)
            if any(dims.get(f) != val for f, val in filters.items()):
                continue
            gkey = dims.get(proj_dim) if proj_dim else "(all)"
            g = groups.setdefault(gkey, zero_group())
            for f in VAL_FIELDS:
                g[f] += v.get(f, 0)
        return groups

    # period 总计行：无过滤用桶顶层（含 avg_ms）；有过滤则由过滤后的组合求和（无 sum_ms/avg_ms）。
    if filters:
        all_groups = aggregate(None)
        filtered_total = all_groups.get("(all)", zero_group())
        period_requests = filtered_total["requests"]
        period_ok = filtered_total["ok"]
        period_fail = filtered_total["fail"]
        avg_ms_str = "n/a（有过滤条件，账本不存组合键粒度 sum_ms）"
        usage_in, usage_out = (
            filtered_total["usage_in"], filtered_total["usage_out"])
    else:
        period_requests = bucket.get("requests", 0)
        period_ok = bucket.get("ok", 0)
        period_fail = bucket.get("fail", 0)
        sum_ms = bucket.get("sum_ms", 0)
        avg_ms_str = f"{sum_ms / period_requests:.1f}" if period_requests else "0"
        all_groups = aggregate(None)
        filtered_total = all_groups.get("(all)", zero_group())
        usage_in, usage_out = (
            filtered_total["usage_in"], filtered_total["usage_out"])

    print(f"period: {label}   requests={period_requests}  ok={period_ok}  fail={period_fail}  "
          f"avg_ms={avg_ms_str}  usage_in={fmt_k(usage_in)} usage_out={fmt_k(usage_out)}")
    if filters:
        filt_desc = " ".join(f"{k}={v}" for k, v in filters.items())
        print(f"filters: {filt_desc}")

    def print_groups(proj_dim):
        groups = aggregate(proj_dim)
        if not groups:
            print("  (no data)")
            return
        for gkey, g in sorted(groups.items(), key=lambda kv: -kv[1]["requests"]):
            print(f"  {gkey:32} requests={g['requests']:<6} ok={g['ok']:<6} fail={g['fail']:<4} "
                  f"in={fmt_k(g['usage_in'])} out={fmt_k(g['usage_out'])}")

    if proj:
        print(f"by {proj}:")
        print_groups(proj)
    else:
        # 都省略：默认对三个维度各做一次投影，各列一段
        for d in DIMS:
            print(f"by {d}:")
            print_groups(d)


main()
PYEOF

  echo "$(grep ' ACCESS ' "$LOG_FILE" 2>/dev/null | awk '
  {
    ms = 0
    for (i = 1; i <= NF; i++) {
      if ($i ~ /^ms=/) { split($i, a, "="); ms = a[2] }
    }
    if (ms > max_ms) max_ms = ms
  }
  END { printf "max_ms=%d", max_ms + 0 }
  ')  (近日志窗口内，非账本口径)"
}

# ---- 主逻辑 ----
case "${1:-}" in
  --help|-h)
    print_help
    ;;
  status)
    cmd_status
    ;;
  reload)
    cmd_reload
    ;;
  supply)
    cmd_supply
    ;;
  route)
    cmd_route
    ;;
  strategy)
    cmd_strategy
    ;;
  switch)
    cmd_switch "${2:-}" "${3:-}"
    ;;
  install)
    cmd_install "${2:-}"
    ;;
  on)
    cmd_on
    ;;
  off)
    cmd_off
    ;;
  logs)
    cmd_logs "${2:-30}"
    ;;
  stats)
    cmd_stats "${@:2}"
    ;;
  "")
    print_help
    ;;
  *)
    echo "Unknown command: $1. Use --help."
    exit 1
    ;;
esac
