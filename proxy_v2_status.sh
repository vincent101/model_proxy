#!/bin/bash
# proxy_v2_status.sh
# 手动控制 proxy_v2.py（http://127.0.0.1:18889/proxy_v2/*）。
# 与 tools/proxy_cli.sh（v1，18888）完全独立，不共用进程/端口/配置。
# 用法：proxy_v2_status.sh <子命令> [参数]

PROXY_V2_PORT="${PROXY_V2_PORT:-18889}"
PROXY_V2_BASE="http://127.0.0.1:${PROXY_V2_PORT}"
CONFIG_FILE="$HOME/.claude/proxy_v2_config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/.claude_proxy_v2.log"
LOCK_FILE="/tmp/claude_proxy_v2.lock"

# ---- 帮助信息 ----
print_help() {
  cat <<EOF
用法: proxy_v2_status.sh <子命令> [参数]

status                   显示运行状态 + supplies/routes/cooldown 概览
reload                   触发配置热重载（POST /proxy_v2/reload）
clear-cooldown <id>      清除某个 supply 的冷却（幂等，id 不存在也返回 ok）
on                       启动 proxy_v2.py（已在监听则跳过）
off                      停止 proxy_v2.py（严格按脚本绝对路径匹配，绝不影响 v1 的 proxy.py）
--help / -h              显示此帮助
EOF
}

# ---- 从 config 读 admin_token ----
get_admin_token() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('admin_token',''))" "$CONFIG_FILE" 2>/dev/null
}

# ---- 带鉴权的 curl 封装 ----
proxy_v2_api() {
  local method="$1" path="$2" data="$3"
  local token
  token=$(get_admin_token)
  local args=(-s -X "$method" -H "X-Proxy-Admin-Token: $token" "$PROXY_V2_BASE$path")
  [[ -n "$data" ]] && args+=(-H "Content-Type: application/json" -d "$data")
  curl "${args[@]}"
}

# ---- status ----
cmd_status() {
  if lsof -i :"$PROXY_V2_PORT" -sTCP:LISTEN -t &>/dev/null; then
    echo "proxy_v2: running on port $PROXY_V2_PORT"
  else
    echo "proxy_v2: NOT running on port $PROXY_V2_PORT"
    return 1
  fi

  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi

  local out
  out=$(proxy_v2_api GET /proxy_v2/status)
  if [[ -z "$out" ]]; then
    echo "Error: proxy_v2 not responding"
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
print('routes:')
for r in data.get('routes', []):
    match = r.get('match', {})
    print(f\"  match={match}  supplies={r.get('supplies', [])}  failover={r.get('failover', '?')}\")
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
  local out
  out=$(proxy_v2_api POST /proxy_v2/reload)
  echo "Reloaded: $out"
}

# ---- clear-cooldown ----
cmd_clear_cooldown() {
  local supply_id="$1"
  if [[ -z "$supply_id" ]]; then
    echo "用法: clear-cooldown <supply_id>"
    return 1
  fi
  local out
  out=$(proxy_v2_api POST "/proxy_v2/supply/${supply_id}/cooldown/clear")
  echo "$out"
}

# ---- on ----
cmd_on() {
  if lsof -i :"$PROXY_V2_PORT" -sTCP:LISTEN -t &>/dev/null; then
    echo "proxy_v2 already running on port $PROXY_V2_PORT"
    return 0
  fi
  echo "Starting proxy_v2.py on port $PROXY_V2_PORT..."
  PROXY_V2_PORT="$PROXY_V2_PORT" nohup python3 "$SCRIPT_DIR/proxy_v2.py" >> "$LOG_FILE" 2>&1 &
  local pid=$!

  for i in {1..10}; do
    sleep 0.5
    if lsof -i :"$PROXY_V2_PORT" -sTCP:LISTEN -t &>/dev/null; then
      echo "proxy_v2 started (pid $pid), ready (${i} * 0.5s)"
      return 0
    fi
  done

  echo "WARNING: proxy_v2 failed to start on port $PROXY_V2_PORT. Last log:"
  tail -5 "$LOG_FILE"
  return 1
}

# ---- off ----
# 安全约束：只匹配本脚本同目录下 proxy_v2.py 的绝对路径，绝不使用宽泛的
# "proxy_v2.py" 或 "proxy.py" 之类的模式，避免误杀 v1 的 tools/proxy.py（18888 生产进程）。
cmd_off() {
  local target="$SCRIPT_DIR/proxy_v2.py"
  local pids
  pids=$(pgrep -f "$target" 2>/dev/null)
  if [[ -z "$pids" ]]; then
    echo "proxy_v2 not running (no process matching: $target)"
    return 0
  fi
  echo "$pids" | while read -r pid; do
    kill "$pid" 2>/dev/null && echo "Stopped proxy_v2 (pid $pid)" || echo "Failed to kill pid $pid"
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
  clear-cooldown)
    cmd_clear_cooldown "${2:-}"
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
