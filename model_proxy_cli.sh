#!/bin/sh
# Bootstrap only: select an interpreter before running the command exactly once.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for python in python3 python /opt/homebrew/bin/python3 /opt/homebrew/anaconda3/bin/python3 /usr/local/bin/python3; do
    if command -v "$python" >/dev/null 2>&1 &&
       "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        exec "$python" "$script_dir/core/cli/main.py" "$@"
    fi
done
printf '%s\n' 'Error: no Python >= 3.10 interpreter found; actual platform support still requires validation.' >&2
exit 1
