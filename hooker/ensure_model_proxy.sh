#!/bin/bash
# ensure_model_proxy.sh
# 确保 model_proxy 运行（SessionStart hook 调用，也可手动调）；幂等：已运行则直接退出。
# 监听地址判定与启动/就绪等待全部委托 model_proxy_cli.sh on（单一实现，
# 端口解析共用 core/listen_config.py），本脚本不再自行拼端口判断；
# 仅保留 mkdir 原子锁防并发拉起（cli.sh 本身无锁，server 侧 flock 兜底双启）。
# 注：v1 ensure_proxy.sh 于 2026-07-24 随 proxy.py 下线删除，
# 本脚本为唯一代理启动守卫；PID/ensure 日志由 CLI 的 LOG_FILE 承担，不再单独维护。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$SCRIPT_DIR/../model_proxy_cli.sh"
PATHS_FILE="$SCRIPT_DIR/../config/runtime_paths.json"

# ---- 从 runtime_paths.json 读 start_lock（缺失/corrupt 回退默认值）----
START_LOCK=$(python3 -c "
import json, sys, os
try:
    with open(sys.argv[1]) as f:
        v = json.load(f).get('start_lock', '')
except Exception:
    v = ''
if v and not v.startswith('/'):
    v = os.path.join(sys.argv[2], v)
print(v)
" "$PATHS_FILE" "$(cd "$SCRIPT_DIR/.." && pwd)" 2>/dev/null)
[[ -z "$START_LOCK" ]] && START_LOCK="/tmp/model_proxy_start.lock"

# mkdir 原子锁，防并发启动
if ! mkdir "$START_LOCK" 2>/dev/null; then
  # 另一个实例正在处理，等它完成后检查结果
  sleep 1
  exit 0
fi
trap 'rmdir "$START_LOCK" 2>/dev/null' EXIT

# 拉起/幂等检查/就绪等待全部委托 CLI（不能用 exec：会跳过上面的 EXIT trap 清锁）
bash "$CLI" on
exit $?
