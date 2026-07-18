#!/bin/bash
# model_proxy_cli.sh
# 手动控制 model_proxy.py（http://127.0.0.1:18889/model_proxy/*）。
# 与 tools/proxy_cli.sh（v1，18888）完全独立，不共用进程/端口/配置。
# 用法：model_proxy_cli.sh <子命令> [参数]

MODEL_PROXY_PORT="${MODEL_PROXY_PORT:-18889}"
MODEL_PROXY_BASE="http://127.0.0.1:${MODEL_PROXY_PORT}"
CONFIG_FILE="$HOME/.claude/model_proxy_config.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/.claude_model_proxy.log"
LOCK_FILE="/tmp/claude_model_proxy.lock"

# ---- 帮助信息 ----
print_help() {
  cat <<EOF
用法: model_proxy_cli.sh <子命令> [参数]

status                            显示运行状态 + supplies/routes/cooldown 概览
reload                            触发配置热重载（POST /model_proxy/reload）
clear-cooldown <id>               清除某个 supply 的冷却（幂等，id 不存在也返回 ok）
supply list                       列出所有 supply（appkey 脱敏尾4位、cooldown）
supply add                        交互式新增 supply（写配置后 reload）
supply rotate-appkey <id> <key>   替换某 supply 的 appkey，reload 并解冷
route list                        列出所有 route
route add                         交互式新增 route（写配置后 reload）
migrate                           选一条 route 的 client_token 写入 ~/.claude/settings.json
on                                启动 model_proxy.py（已在监听则跳过）
off                               停止 model_proxy.py（严格按脚本绝对路径匹配，绝不影响 v1 的 proxy.py）
--help / -h                       显示此帮助
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

# ---- reload 封装 ----
reload_proxy() {
  local out
  out=$(model_proxy_api POST /model_proxy/reload)
  echo "Reloaded: $out"
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
  reload_proxy
}

# ---- clear-cooldown ----
cmd_clear_cooldown() {
  local supply_id="$1"
  if [[ -z "$supply_id" ]]; then
    echo "用法: clear-cooldown <supply_id>"
    return 1
  fi
  local out
  out=$(model_proxy_api POST "/model_proxy/supply/${supply_id}/cooldown/clear")
  echo "$out"
}

# ---- supply ----
cmd_supply() {
  local subcmd="${1:-}"

  case "$subcmd" in
    list)
      if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "Error: config not found: $CONFIG_FILE"
        return 1
      fi
      python3 -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
for s in cfg.get('supplies', []):
    sid = s.get('id', '?')
    proto = s.get('protocol', '?')
    model = s.get('target_model', '?')
    tail4 = str(s.get('appkey', ''))[-4:] or '????'
    reasoning = 'Y' if s.get('reasoning') else 'N'
    if 'cooldown_seconds' in s:
        cd = f\"{s['cooldown_seconds']}s\"
    else:
        cd = '(默认)'
    print(f'  {sid:20} protocol={proto:10} model={model:24} appkey=...{tail4}  reasoning={reasoning}  cooldown={cd}')
" "$CONFIG_FILE"
      ;;
    add)
      echo -n "Supply ID: "; read -r sid
      echo -n "上游 URL: "; read -r surl
      echo -n "协议 [anthropic/chat/responses]: "; read -r sproto
      echo -n "Appkey: "; read -r sappkey
      echo -n "目标模型 target_model: "; read -r smodel
      echo -n "是否推理模型 reasoning [y/N]: "; read -r sreason
      echo -n "冷却时长 cooldown_seconds (回车用全局默认): "; read -r scooldown
      python3 -c "
import json, os, tempfile, sys
sid, surl, sproto, sappkey, smodel, sreason, scooldown = sys.argv[1:8]
FILE = sys.argv[8]
sid = sid.strip()
if not sid:
    print('Error: Supply ID 不能为空', file=sys.stderr); sys.exit(1)
if sproto not in ('anthropic', 'chat', 'responses'):
    print(f'Error: 协议非法: {sproto!r}（须为 anthropic/chat/responses）', file=sys.stderr); sys.exit(1)
cfg = json.load(open(FILE))
supplies = cfg.setdefault('supplies', [])
if any(s.get('id') == sid for s in supplies):
    print(f'Error: supply id 已存在: {sid}', file=sys.stderr); sys.exit(1)
entry = {
    'id': sid,
    'url': surl,
    'protocol': sproto,
    'appkey': sappkey,
    'target_model': smodel,
    'reasoning': sreason.strip().lower() == 'y',
}
scooldown = scooldown.strip()
if scooldown:
    if not scooldown.isdigit() or int(scooldown) <= 0:
        print(f'Error: cooldown_seconds 须为正整数: {scooldown!r}', file=sys.stderr); sys.exit(1)
    entry['cooldown_seconds'] = int(scooldown)
supplies.append(entry)
_dir = os.path.dirname(FILE)
fd, tmp = tempfile.mkstemp(dir=_dir, suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)
except Exception:
    os.unlink(tmp); raise
print(f'Added supply: {sid}')
" "$sid" "$surl" "$sproto" "$sappkey" "$smodel" "$sreason" "$scooldown" "$CONFIG_FILE" || return 1
      reload_proxy
      ;;
    rotate-appkey)
      local rid="$2" rkey="$3"
      if [[ -z "$rid" || -z "$rkey" ]]; then
        echo "用法: supply rotate-appkey <id> <new_appkey>"
        return 1
      fi
      python3 -c "
import json, os, tempfile, sys
rid, rkey, FILE = sys.argv[1:4]
cfg = json.load(open(FILE))
target = None
for s in cfg.get('supplies', []):
    if s.get('id') == rid:
        target = s; break
if target is None:
    print(f'Error: supply id 不存在: {rid}', file=sys.stderr); sys.exit(1)
target['appkey'] = rkey
_dir = os.path.dirname(FILE)
fd, tmp = tempfile.mkstemp(dir=_dir, suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)
except Exception:
    os.unlink(tmp); raise
print(f'Rotated appkey for supply {rid}: ...{rkey[-4:]}')
" "$rid" "$rkey" "$CONFIG_FILE" || return 1
      reload_proxy
      cmd_clear_cooldown "$rid"
      ;;
    *)
      echo "用法: supply list | supply add | supply rotate-appkey <id> <new_appkey>"
      ;;
  esac
}

# ---- route ----
cmd_route() {
  local subcmd="${1:-}"

  case "$subcmd" in
    list)
      if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "Error: config not found: $CONFIG_FILE"
        return 1
      fi
      python3 -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
for i, r in enumerate(cfg.get('routes', [])):
    match = r.get('match', {})
    token = match.get('client_token', '?')
    tier = match.get('model_tier') or '(任意)'
    supplies = ', '.join(r.get('supplies', []))
    failover = r.get('failover', '?')
    print(f'  [{i}] client_token={token:16} tier={tier:10} supplies=[{supplies}]  failover={failover}')
" "$CONFIG_FILE"
      ;;
    add)
      echo -n "Client token (客户端 Authorization Bearer): "; read -r rtoken
      echo -n "Model tier [opus/sonnet/haiku/default, 留空=任意]: "; read -r rtier
      echo -n "Supplies (空格分隔, 按优先级排序, 需为已存在 supply id): "; read -r rsupplies_raw
      echo -n "Failover [on/off]: "; read -r rfailover
      python3 -c "
import json, os, tempfile, sys
rtoken, rtier, rsupplies_raw, rfailover = sys.argv[1:5]
FILE = sys.argv[5]
rtoken = rtoken.strip()
if not rtoken:
    print('Error: client_token 不能为空', file=sys.stderr); sys.exit(1)
rtier = rtier.strip()
if rtier and rtier not in ('opus', 'sonnet', 'haiku', 'default'):
    print(f'Error: model_tier 非法: {rtier!r}（须为 opus/sonnet/haiku/default 或留空）', file=sys.stderr); sys.exit(1)
if rfailover not in ('on', 'off'):
    print(f'Error: failover 非法: {rfailover!r}（须为 on/off）', file=sys.stderr); sys.exit(1)
supplies = rsupplies_raw.split()
if not supplies:
    print('Error: supplies 不能为空', file=sys.stderr); sys.exit(1)
cfg = json.load(open(FILE))
known = {s.get('id') for s in cfg.get('supplies', [])}
bad = [x for x in supplies if x not in known]
if bad:
    print(f'Error: 以下 supply id 不存在: {bad}', file=sys.stderr); sys.exit(1)
match = {'client_token': rtoken}
if rtier:
    match['model_tier'] = rtier
cfg.setdefault('routes', []).append({
    'match': match,
    'supplies': supplies,
    'failover': rfailover,
})
_dir = os.path.dirname(FILE)
fd, tmp = tempfile.mkstemp(dir=_dir, suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)
except Exception:
    os.unlink(tmp); raise
print(f'Added route: client_token={rtoken} supplies={supplies}')
" "$rtoken" "$rtier" "$rsupplies_raw" "$rfailover" "$CONFIG_FILE" || return 1
      reload_proxy
      ;;
    *)
      echo "用法: route list | route add"
      ;;
  esac
}

# ---- migrate ----
cmd_migrate() {
  local settings="$HOME/.claude/settings.json"
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi
  if [[ ! -f "$settings" ]]; then
    echo "Error: $settings not found"
    return 1
  fi

  # 列出 routes 里所有 client_token 供选择（清单打到 stderr，token 列表打到 stdout）
  local tokens
  tokens=$(python3 -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
routes = cfg.get('routes', [])
if not routes:
    sys.exit(2)
print('可用的 client_token（来自 routes）:', file=sys.stderr)
for i, r in enumerate(routes):
    m = r.get('match', {})
    tok = m.get('client_token', '?')
    tier = m.get('model_tier') or '任意'
    supplies = ', '.join(r.get('supplies', []))
    print(f'  [{i}] {tok:16} (tier={tier}, supplies=[{supplies}])', file=sys.stderr)
    print(tok)
" "$CONFIG_FILE")
  local rc=$?
  if [[ $rc -eq 2 ]]; then
    echo "Error: routes 为空，请先 route add"
    return 1
  fi

  echo -n "选择要写入 settings.json 的 client_token 序号: "; read -r idx
  local chosen
  chosen=$(echo "$tokens" | sed -n "$((idx + 1))p")
  if [[ -z "$chosen" ]]; then
    echo "Error: 无效序号: $idx"
    return 1
  fi

  local backup="${settings}.bak.$(date +%Y%m%d%H%M%S)"
  cp "$settings" "$backup"
  echo "Backup: $backup"

  python3 -c "
import json, sys
settings_file, base_url, token = sys.argv[1:4]
cfg = json.load(open(settings_file))
env = cfg.setdefault('env', {})
env['ANTHROPIC_BASE_URL'] = base_url
env['ANTHROPIC_AUTH_TOKEN'] = token
# model 字段保留不动
json.dump(cfg, open(settings_file, 'w'), indent=2, ensure_ascii=False)
print('settings.json migrated:')
print(f'  ANTHROPIC_BASE_URL   -> {base_url}')
print(f'  ANTHROPIC_AUTH_TOKEN -> {token}')
" "$settings" "http://localhost:${MODEL_PROXY_PORT}/" "$chosen"

  echo ""
  echo "请重启 Claude Code 生效。"
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
  clear-cooldown)
    cmd_clear_cooldown "${2:-}"
    ;;
  supply)
    cmd_supply "${2:-}" "${3:-}" "${4:-}"
    ;;
  route)
    cmd_route "${2:-}"
    ;;
  migrate)
    cmd_migrate
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
