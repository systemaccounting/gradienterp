#!/usr/bin/env bash
# Reset a dev tenant's DATA to a clean state, keeping config — thin wrapper over
# scripts/reset_dev.py (see its docstring). Resources self-describe via the `gerp:layer`
# tag; the target account is hardcoded in reset_dev.py (reassign to a dev account later).
#
#   bash scripts/reset-dev.sh                 # clear books+operational+session (confirm prompt)
#   bash scripts/reset-dev.sh --yes           # no prompt
#   bash scripts/reset-dev.sh --layer session # just chat/memory/uploads
#   bash scripts/reset-dev.sh --dry-run       # show the plan, touch nothing
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/reset_dev.py "$@"
