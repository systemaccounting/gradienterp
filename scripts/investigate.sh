#!/usr/bin/env bash
# A person works an alarm task — thin wrapper over scripts/investigate.py (see its docstring).
#
#   bash scripts/investigate.sh <task_id>                                        # the prompt
#   bash scripts/investigate.sh <task_id> --finding F --root-cause R --proposed-fix P   # the write-back
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/investigate.py "$@"
