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
route list                        列出所有 route（家族模板：opus/sonnet/haiku 三档 + failover）
route add                         交互式新增 route 家族模板（写配置后 reload）
strategy list                     列出所有 strategy（client_token -> route_id 绑定）
strategy add                      交互式新增 strategy 绑定（写配置后 reload）
switch <client_token> <route_id>  切换某 token 绑定的 route 家族（改 strategy.route_id 后 reload）
migrate                           选一个 strategy 的 client_token 写入 ~/.claude/settings.json
probe-effort <supply_id>          向该 supply 发一个非法 effort 值，探测其真实支持的 effort 枚举
                                  （仅辅助人工审阅，不写入 config；解析不保证准确）
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
    rmap = 'Y' if s.get('reasoning_map') else '-'
    if 'cooldown_seconds' in s:
        cd = f\"{s['cooldown_seconds']}s\"
    else:
        cd = '(默认)'
    print(f'  {sid:20} protocol={proto:10} model={model:24} appkey=...{tail4}  reasoning_map={rmap}  cooldown={cd}')
" "$CONFIG_FILE"
      ;;
    add)
      echo -n "Supply ID: "; read -r sid
      echo -n "上游 URL: "; read -r surl
      echo -n "协议 [anthropic/chat/responses]: "; read -r sproto
      echo -n "Appkey: "; read -r sappkey
      echo -n "目标模型 target_model: "; read -r smodel
      echo -n "冷却时长 cooldown_seconds (回车用全局默认): "; read -r scooldown
      python3 -c "
import json, os, tempfile, sys
sid, surl, sproto, sappkey, smodel, scooldown = sys.argv[1:7]
FILE = sys.argv[7]
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
print(f'Added supply: {sid}（如需 effort 覆盖，可后续手动编辑该 supply 的 reasoning_map 字段，参考 probe-effort）')
" "$sid" "$surl" "$sproto" "$sappkey" "$smodel" "$scooldown" "$CONFIG_FILE" || return 1
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
for r in cfg.get('routes', []):
    rid = r.get('id', '?')
    tiers = r.get('tiers', {})
    opus = ','.join(tiers.get('opus', []))
    sonnet = ','.join(tiers.get('sonnet', []))
    haiku = ','.join(tiers.get('haiku', []))
    failover = r.get('failover', '?')
    print(f'  {rid:12} opus=[{opus}] sonnet=[{sonnet}] haiku=[{haiku}] failover={failover}')
" "$CONFIG_FILE"
      ;;
    add)
      echo -n "Route ID: "; read -r rid
      echo -n "Opus 档 supplies (空格分隔, 按优先级排序): "; read -r ropus
      echo -n "Sonnet 档 supplies (空格分隔, 按优先级排序): "; read -r rsonnet
      echo -n "Haiku 档 supplies (空格分隔, 按优先级排序): "; read -r rhaiku
      echo -n "Failover [on/off]: "; read -r rfailover
      python3 -c "
import json, os, tempfile, sys
rid, ropus, rsonnet, rhaiku, rfailover = sys.argv[1:6]
FILE = sys.argv[6]
rid = rid.strip()
if not rid:
    print('Error: Route ID 不能为空', file=sys.stderr); sys.exit(1)
if rfailover not in ('on', 'off'):
    print(f'Error: failover 非法: {rfailover!r}（须为 on/off）', file=sys.stderr); sys.exit(1)
opus = ropus.split(); sonnet = rsonnet.split(); haiku = rhaiku.split()
cfg = json.load(open(FILE))
if any(r.get('id') == rid for r in cfg.get('routes', [])):
    print(f'Error: route id 已存在: {rid}', file=sys.stderr); sys.exit(1)
known = {s.get('id') for s in cfg.get('supplies', [])}
bad = [x for x in (opus + sonnet + haiku) if x not in known]
if bad:
    print(f'Error: 以下 supply id 不存在: {sorted(set(bad))}', file=sys.stderr); sys.exit(1)
cfg.setdefault('routes', []).append({
    'id': rid,
    'tiers': {'opus': opus, 'sonnet': sonnet, 'haiku': haiku},
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
print(f'Added route: id={rid} opus={opus} sonnet={sonnet} haiku={haiku} failover={rfailover}')
" "$rid" "$ropus" "$rsonnet" "$rhaiku" "$rfailover" "$CONFIG_FILE" || return 1
      reload_proxy
      ;;
    *)
      echo "用法: route list | route add"
      ;;
  esac
}

# ---- strategy ----
cmd_strategy() {
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
for st in cfg.get('strategies', []):
    tok = st.get('client_token', '?')
    rid = st.get('route_id', '?')
    note = st.get('note', '') or ''
    print(f'  {tok:16} -> {rid:12} ({note})')
" "$CONFIG_FILE"
      ;;
    add)
      echo -n "Client token: "; read -r stoken
      echo -n "Route ID (需为已存在的 route id): "; read -r srid
      echo -n "Note (可选备注): "; read -r snote
      python3 -c "
import json, os, tempfile, sys
stoken, srid, snote = sys.argv[1:4]
FILE = sys.argv[4]
stoken = stoken.strip(); srid = srid.strip()
if not stoken:
    print('Error: client_token 不能为空', file=sys.stderr); sys.exit(1)
if not srid:
    print('Error: route_id 不能为空', file=sys.stderr); sys.exit(1)
cfg = json.load(open(FILE))
if any(s.get('client_token') == stoken for s in cfg.get('strategies', [])):
    print(f'Error: client_token 已存在 strategy 绑定: {stoken}', file=sys.stderr); sys.exit(1)
if not any(r.get('id') == srid for r in cfg.get('routes', [])):
    print(f'Error: route id 不存在: {srid}', file=sys.stderr); sys.exit(1)
cfg.setdefault('strategies', []).append({
    'client_token': stoken,
    'route_id': srid,
    'note': snote,
})
_dir = os.path.dirname(FILE)
fd, tmp = tempfile.mkstemp(dir=_dir, suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)
except Exception:
    os.unlink(tmp); raise
print(f'Added strategy: {stoken} -> {srid}')
" "$stoken" "$srid" "$snote" "$CONFIG_FILE" || return 1
      reload_proxy
      ;;
    *)
      echo "用法: strategy list | strategy add"
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
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi
  python3 -c "
import json, os, tempfile, sys
stoken, srid, FILE = sys.argv[1:4]
cfg = json.load(open(FILE))
target = None
for s in cfg.get('strategies', []):
    if s.get('client_token') == stoken:
        target = s; break
if target is None:
    print(f'Error: 未找到该 token 的 strategy 绑定: {stoken}，请先用 strategy add 新增', file=sys.stderr); sys.exit(1)
if not any(r.get('id') == srid for r in cfg.get('routes', [])):
    print(f'Error: route id 不存在: {srid}', file=sys.stderr); sys.exit(1)
old = target.get('route_id')
target['route_id'] = srid
_dir = os.path.dirname(FILE)
fd, tmp = tempfile.mkstemp(dir=_dir, suffix='.tmp')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)
except Exception:
    os.unlink(tmp); raise
print(f'已切换: {stoken} -> route_id={srid}（原 route_id={old}）')
" "$stoken" "$srid" "$CONFIG_FILE" || return 1
  reload_proxy
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

  # 列出 strategies 里所有 client_token 供选择（清单打到 stderr，token 列表打到 stdout）
  local tokens
  tokens=$(python3 -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
strategies = cfg.get('strategies', [])
if not strategies:
    sys.exit(2)
print('可用的 client_token（来自 strategies）:', file=sys.stderr)
for i, s in enumerate(strategies):
    tok = s.get('client_token', '?')
    rid = s.get('route_id', '?')
    note = s.get('note', '') or ''
    print(f'  [{i}] {tok:16} (route_id={rid}, note={note})', file=sys.stderr)
    print(tok)
" "$CONFIG_FILE")
  local rc=$?
  if [[ $rc -eq 2 ]]; then
    echo "Error: strategies 为空，请先 strategy add"
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
  if ! cp "$settings" "$backup"; then
    echo "Error: backup failed (cannot write $backup), abort without touching $settings"
    return 1
  fi
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

# ---- probe-effort ----
# 向指定 supply 的上游直接发一个已知非法的 effort 值，尝试从报错响应里解析真实支持的
# effort 枚举。仅辅助人工审阅，不自动写入 config（供应商报错格式差异大、可能被截断）。
cmd_probe_effort() {
  local supply_id="$1"
  if [[ -z "$supply_id" ]]; then
    echo "用法: probe-effort <supply_id>"
    return 1
  fi
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: config not found: $CONFIG_FILE"
    return 1
  fi
  python3 -c "
import json, sys, re, urllib.request, urllib.error

SUPPLY_ID, FILE = sys.argv[1:3]
PROBE = '__probe_invalid__'

cfg = json.load(open(FILE))
supply = None
for s in cfg.get('supplies', []):
    if s.get('id') == SUPPLY_ID:
        supply = s; break
if supply is None:
    print(f'Error: supply id 不存在: {SUPPLY_ID}', file=sys.stderr); sys.exit(1)

url = (supply.get('url') or '').rstrip('/')
proto = supply.get('protocol')
appkey = supply.get('appkey', '')
model = supply.get('target_model', '')

if proto == 'anthropic':
    target = url + '/v1/messages'
    body = {'model': model, 'max_tokens': 16,
            'thinking': {'type': 'adaptive'},
            'output_config': {'effort': PROBE},
            'messages': [{'role': 'user', 'content': 'probe'}]}
elif proto == 'chat':
    target = url + '/chat/completions'
    body = {'model': model, 'max_tokens': 16,
            'reasoning_effort': PROBE,
            'messages': [{'role': 'user', 'content': 'probe'}]}
elif proto == 'responses':
    target = url  # url 已配到完整 /v1/responses 层级
    body = {'model': model, 'max_output_tokens': 16,
            'reasoning': {'effort': PROBE},
            'input': 'probe'}
else:
    print(f'Error: 未知 protocol: {proto!r}', file=sys.stderr); sys.exit(1)

data = json.dumps(body).encode('utf-8')
headers = {'Content-Type': 'application/json',
           'Authorization': f'Bearer {appkey}', 'x-api-key': appkey}
req = urllib.request.Request(target, data=data, headers=headers, method='POST')

status = None
raw = ''
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        raw = resp.read().decode('utf-8', 'replace')
except urllib.error.HTTPError as e:
    status = e.code
    raw = e.read().decode('utf-8', 'replace')
except Exception as e:
    print(f'Error: 请求失败: {e}', file=sys.stderr); sys.exit(1)

print(f'supply={SUPPLY_ID} protocol={proto} model={model}')
print(f'endpoint={target}')
print(f'HTTP status={status}')
print('-' * 60)

# 宽松解析 'Supported values are: xxx, yyy, zzz' 之类措辞（引号包裹词组，英文逗号分隔）
m = re.search(r'[Ss]upported values (?:are)?\s*[:：]?\s*(.+)', raw)
enums = None
if m:
    tail = m.group(1)
    # 提取引号包裹的词（单/双引号），退化到逗号分隔的裸词
    quoted = re.findall(r\"['\\\"]([^'\\\"]+)['\\\"]\", tail)
    cands = quoted if quoted else [
        w.strip() for w in re.split(r'[,，]', re.split(r'[.。\n]', tail)[0]) if w.strip()]
    # 只保留开头连续的合法 effort 标识符，遇到第一个非标识符（多为后续 JSON 噪音）即停
    ident = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*\$')
    enums = []
    for w in cands:
        if ident.match(w):
            enums.append(w)
        else:
            break
    enums = enums or None

if enums:
    print(f'疑似支持的枚举: {enums}')
else:
    print('无法自动解析，请查看原始响应自行判断:')
    print(raw[:500] + ('...(truncated)' if len(raw) > 500 else ''))

print('-' * 60)
print('提示: 这是探测辅助结果，不保证准确。如需生效请手动编辑 config 的 reasoning_map 字段。')
" "$supply_id" "$CONFIG_FILE"
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
  strategy)
    cmd_strategy "${2:-}"
    ;;
  switch)
    cmd_switch "${2:-}" "${3:-}"
    ;;
  migrate)
    cmd_migrate
    ;;
  probe-effort)
    cmd_probe_effort "${2:-}"
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
