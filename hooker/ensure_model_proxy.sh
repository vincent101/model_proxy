#!/bin/bash
# ensure_model_proxy.sh
# 确保 model_proxy.py 在 18889 运行（SessionStart hook 调用，也可手动调）
# 幂等：已运行则直接退出；未运行则启动并等待就绪
# 注：v1 ensure_proxy.sh 于 2026-07-24 随 proxy.py 下线删除，
# 本脚本为唯一代理启动守卫，PID/锁/日志文件独立命名。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${MODEL_PROXY_PORT:-18889}"
PATHS_FILE="$SCRIPT_DIR/../config/runtime_paths.json"

# ---- 从 runtime_paths.json 加载运行时路径（启动时执行一次）----
load_hooker_paths() {
  local base="$(cd "$SCRIPT_DIR/.." && pwd)"
  eval "$(python3 -c "
import json, sys, os
base = sys.argv[1]
paths_file = sys.argv[2]
try:
    with open(paths_file) as f:
        paths = json.load(f)
except Exception:
    paths = {}
# config key -> hooker.sh 变量名
mapping = {
    'pid': 'PID_FILE',
    'ensure_log': 'ENSURE_LOG',
    'start_lock': 'START_LOCK',
}
defaults = {
    'pid': '/tmp/model_proxy.pid',
    'ensure_log': '/tmp/model_proxy_ensure.log',
    'start_lock': '/tmp/model_proxy_start.lock',
}
for k, var in mapping.items():
    v = paths.get(k, defaults[k])
    if not v.startswith('/'):
        v = os.path.join(base, v)
    print(f'{var}=\"{v}\"')
" "$base" "$PATHS_FILE" 2>/dev/null)"
  # eval 后校验关键变量非空（同 cli.sh，不依赖 || 兜底）
  if [[ -z "$PID_FILE" || -z "$ENSURE_LOG" || -z "$START_LOCK" ]]; then
    PID_FILE="/tmp/model_proxy.pid"
    ENSURE_LOG="/tmp/model_proxy_ensure.log"
    START_LOCK="/tmp/model_proxy_start.lock"
  fi
}
load_hooker_paths

# mkdir 原子锁，防并发启动
if ! mkdir "$START_LOCK" 2>/dev/null; then
  # 另一个实例正在处理，等它完成后检查结果
  sleep 1
  exit 0
fi
trap 'rmdir "$START_LOCK" 2>/dev/null' EXIT

# 检查 PID 文件（PID 存活且端口监听，才认为 model_proxy 在运行）
if [[ -f "$PID_FILE" ]]; then
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null && lsof -i :"$PORT" -sTCP:LISTEN -t &>/dev/null; then
    exit 0  # 进程存活且端口已绑定
  fi
  rm -f "$PID_FILE"
fi

# 检查端口是否在监听（PID 文件缺失但端口已占用时兜底）
if lsof -i :"$PORT" -sTCP:LISTEN -t &>/dev/null; then
  exit 0  # 已在运行
fi

# 启动
echo "[ensure_model_proxy] Starting model_proxy.py on port $PORT..." >&2
nohup python3 "$SCRIPT_DIR/../model_proxy.py" >> "$ENSURE_LOG" 2>&1 &
echo $! > "$PID_FILE"

# 等待就绪（最多5秒，0.5s轮询）
for i in {1..10}; do
  sleep 0.5
  if lsof -i :"$PORT" -sTCP:LISTEN -t &>/dev/null; then
    echo "[ensure_model_proxy] Service ready (${i} * 0.5s)" >&2
    exit 0
  fi
done

echo "[ensure_model_proxy] WARNING: model_proxy failed to start on port $PORT. Last log:" >&2
tail -5 "$ENSURE_LOG" >&2
exit 1
