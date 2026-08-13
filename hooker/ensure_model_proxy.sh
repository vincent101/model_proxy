#!/bin/bash
# ensure_model_proxy.sh
# 确保 model_proxy.py 在 18889 运行（SessionStart hook 调用，也可手动调）
# 幂等：已运行则直接退出；未运行则启动并等待就绪
# 注：v1 ensure_proxy.sh 于 2026-07-24 随 proxy.py 下线删除，
# 本脚本为唯一代理启动守卫，PID/锁/日志文件独立命名。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/model_proxy.pid"
LOG="/tmp/model_proxy_ensure.log"
PORT="${MODEL_PROXY_PORT:-18889}"

# mkdir 原子锁，防并发启动
LOCKDIR="/tmp/model_proxy_start.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # 另一个实例正在处理，等它完成后检查结果
  sleep 1
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

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
nohup python3 "$SCRIPT_DIR/../model_proxy.py" >> "$LOG" 2>&1 &
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
tail -5 "$LOG" >&2
exit 1
