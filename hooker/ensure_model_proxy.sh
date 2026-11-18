#!/bin/bash
# SessionStart hook: lifecycle, initialization and locking belong to the CLI.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || exit $?
exec bash "$SCRIPT_DIR/../model_proxy_cli.sh" on
