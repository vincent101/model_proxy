#!/bin/bash
# model_proxy_cli.sh
# 手动控制 model_proxy.py（http://127.0.0.1:18889/model_proxy/*）。
# 与 tools/proxy_cli.sh（v1，18888）完全独立，不共用进程/端口/配置。
# 用法：model_proxy_cli.sh <子命令> [参数]

MODEL_PROXY_PORT="${MODEL_PROXY_PORT:-18889}"
MODEL_PROXY_BASE="http://127.0.0.1:${MODEL_PROXY_PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${MODEL_PROXY_CONFIG:-$SCRIPT_DIR/model_proxy_config.json}"
LOG_FILE="$SCRIPT_DIR/.claude_model_proxy.log"
LOCK_FILE="/tmp/claude_model_proxy.lock"
CONFIG_OPS="$SCRIPT_DIR/_config_ops.py"
INSTALL_OPS="$SCRIPT_DIR/_install_ops.py"

# ---- 帮助信息 ----
print_help() {
  cat <<EOF
用法: model_proxy_cli.sh <子命令> [参数]

status                            显示运行状态 + supplies/routes/cooldown 概览
reload                            触发配置热重载（无条件清空所有 cooldown）

supply                            不带子命令：打印 supply list 后进入交互菜单
                                    [a]dd / [e]dit / [d]el / [p]robe / [q]uit
supply list                       列出所有 supply（appkey 脱敏尾4位、cooldown）
supply add                        交互式新增 supply（同步探测 effort，写配置后 reload）
supply edit <id>                  交互式编辑 supply（含改 appkey、可选重新探测 effort）
supply del <id>                   删除 supply（二次确认，被 route 引用则拒绝）
supply probe <id>                 只跑 effort 探测，接受则回写 reasoning_capability

route                             不带子命令：打印 route list 后进入交互菜单
                                    [a]dd / [e]dit / [d]el / [q]uit
route list                        列出所有 route（家族模板：opus/sonnet/haiku 三档 + failover）
route add                         交互式新增 route 家族模板（写配置后 reload）
route edit <id>                   交互式编辑 route 的 tiers/failover
route del <id>                    删除 route（二次确认，被 strategy 引用则拒绝）

strategy                          不带子命令：打印 strategy list 后进入交互菜单
                                    [a]dd / [e]dit / [d]el / [q]uit
strategy list                     列出所有 strategy（client_token -> route_id 绑定）
strategy add                      交互式新增 strategy 绑定（写配置后 reload）
strategy edit <token>             交互式编辑 strategy 的 route_id/note
strategy del <token>              删除 strategy（二次确认，无下游引用检查）

switch <client_token> <route_id>  切换某 token 绑定的 route 家族（改 strategy.route_id 后 reload）
install                           交互式列出四个 SDK + 本机检测状态，选择安装
install --list                    只列出四个 SDK 检测状态，不安装
on                                启动 model_proxy.py（已在监听则跳过）
off                               停止 model_proxy.py（严格按脚本绝对路径匹配，绝不影响 v1 的 proxy.py）
--help / -h                       显示此帮助

说明: supply/route/strategy 三者均支持"不带子命令进入交互菜单"，
      带子命令（list/add/edit/del/probe）时兼容旧用法，直接执行不进菜单。
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
  local marker
  marker=$(mktemp)
  CONFIG_OPS_RELOAD_MARKER="$marker" python3 "$CONFIG_OPS" "$@" "$CONFIG_FILE"
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
    rid = st.get('route_id', '?')
    note = st.get('note', '') or ''
    print(f'  {tok:16} -> {rid:12} ({note})')
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
cmd_supply_probe() {
  local id="${1:-}"
  if [[ -z "$id" ]]; then echo "用法: supply probe <id>"; return 1; fi
  run_config_ops supply-probe "$id"
}

# ---- supply：入口。不带子命令 -> 打印 list 后进入交互菜单；带子命令 -> 兼容旧用法直接执行 ----
cmd_supply() {
  local subcmd="${1:-}"
  if [[ -z "$subcmd" ]]; then
    while true; do
      cmd_supply_list
      echo ""
      read -p "操作: [a]dd / [e]dit / [d]el / [p]robe / [q]uit: " op
      case "$op" in
        a) cmd_supply_add ;;
        e) read -p "要编辑的 supply id: " eid; cmd_supply_edit "$eid" ;;
        d) read -p "要删除的 supply id: " did; cmd_supply_del "$did" ;;
        p) read -p "要探测的 supply id: " pid; cmd_supply_probe "$pid" ;;
        q|"") break ;;
        *) echo "未知操作" ;;
      esac
      echo ""
    done
    return 0
  fi
  case "$subcmd" in
    list)   cmd_supply_list ;;
    add)    cmd_supply_add ;;
    edit)   cmd_supply_edit "${2:-}" ;;
    del)    cmd_supply_del "${2:-}" ;;
    probe)  cmd_supply_probe "${2:-}" ;;
    *)
      echo "用法: supply list | supply add | supply edit <id> | supply del <id> | supply probe <id>"
      return 1
      ;;
  esac
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

# ---- route：入口。不带子命令 -> 打印 list 后进入交互菜单；带子命令 -> 兼容旧用法直接执行 ----
cmd_route() {
  local subcmd="${1:-}"
  if [[ -z "$subcmd" ]]; then
    while true; do
      cmd_route_list
      echo ""
      read -p "操作: [a]dd / [e]dit / [d]el / [q]uit: " op
      case "$op" in
        a) cmd_route_add ;;
        e) read -p "要编辑的 route id: " eid; cmd_route_edit "$eid" ;;
        d) read -p "要删除的 route id: " did; cmd_route_del "$did" ;;
        q|"") break ;;
        *) echo "未知操作" ;;
      esac
      echo ""
    done
    return 0
  fi
  case "$subcmd" in
    list)   cmd_route_list ;;
    add)    cmd_route_add ;;
    edit)   cmd_route_edit "${2:-}" ;;
    del)    cmd_route_del "${2:-}" ;;
    *)
      echo "用法: route list | route add | route edit <id> | route del <id>"
      return 1
      ;;
  esac
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

# ---- strategy：入口。不带子命令 -> 打印 list 后进入交互菜单；带子命令 -> 兼容旧用法直接执行 ----
cmd_strategy() {
  local subcmd="${1:-}"
  if [[ -z "$subcmd" ]]; then
    while true; do
      cmd_strategy_list
      echo ""
      read -p "操作: [a]dd / [e]dit / [d]el / [q]uit: " op
      case "$op" in
        a) cmd_strategy_add ;;
        e) read -p "要编辑的 strategy token: " etok; cmd_strategy_edit "$etok" ;;
        d) read -p "要删除的 strategy token: " dtok; cmd_strategy_del "$dtok" ;;
        q|"") break ;;
        *) echo "未知操作" ;;
      esac
      echo ""
    done
    return 0
  fi
  case "$subcmd" in
    list)   cmd_strategy_list ;;
    add)    cmd_strategy_add ;;
    edit)   cmd_strategy_edit "${2:-}" ;;
    del)    cmd_strategy_del "${2:-}" ;;
    *)
      echo "用法: strategy list | strategy add | strategy edit <token> | strategy del <token>"
      return 1
      ;;
  esac
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
  if [[ "${1:-}" == "--list" ]]; then
    python3 "$INSTALL_OPS" list "$CONFIG_FILE" "$MODEL_PROXY_PORT"
    return $?
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
    cmd_supply "${2:-}" "${3:-}"
    ;;
  route)
    cmd_route "${2:-}" "${3:-}"
    ;;
  strategy)
    cmd_strategy "${2:-}" "${3:-}"
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
  "")
    print_help
    ;;
  *)
    echo "Unknown command: $1. Use --help."
    exit 1
    ;;
esac
