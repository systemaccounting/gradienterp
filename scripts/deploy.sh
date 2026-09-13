#!/usr/bin/env bash
# Artifact deploys — thin wrapper over scripts/deploy.py (see its docstring).
#
#   bash scripts/deploy.sh status
#   bash scripts/deploy.sh status --all
#   bash scripts/deploy.sh push --dirs modules/shipping/lambdas/manage_shipments --notes "..."
#   bash scripts/deploy.sh push            # whole fleet
#   bash scripts/deploy.sh source          # the codebuild source zip (tower-per-customer)
#   bash scripts/deploy.sh stop  --gerp westwood-c40fd8   # export, then destroy the stack; the row reads stopped
#   bash scripts/deploy.sh start --gerp westwood-c40fd8   # apply it back; the row reads active
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/deploy.py "$@"
