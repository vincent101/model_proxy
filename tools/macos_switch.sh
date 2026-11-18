#!/bin/sh
# 用户手动执行的切换入口；不会启停服务。
set -eu
script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)
for python in python3 python /opt/homebrew/bin/python3 /opt/homebrew/anaconda3/bin/python3 /usr/local/bin/python3; do
    if command -v "$python" >/dev/null 2>&1 &&
        "$python" -I -S -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        exec "$python" -I -S "$script_dir/macos_switch_impl.py" switch "$@"
    fi
done
printf '%s\n' '错误：找不到 Python 3.10 或更高版本；未执行任何迁移。' >&2
exit 1
