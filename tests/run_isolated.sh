#!/bin/sh
# Orchestration only. All application execution occurs in the temporary copy.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
exec "${MP_TEST_PYTHON:-python3}" -I -S -B "$ROOT/tests/worktree_regression.py" "$@"
