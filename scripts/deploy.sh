#!/usr/bin/env bash
# Artifact deploys — thin wrapper over scripts/deploy.py (see its docstring).
#
#   bash scripts/deploy.sh status                         # gradienterp's gerp, through gerp-gradienterp
#   bash scripts/deploy.sh status --gerp westwood-c40fd8 --all
#   bash scripts/deploy.sh push --dirs modules/shipping/lambdas/manage_shipments --notes "..."
#   bash scripts/deploy.sh push --gerp westwood-c40fd8     # whole fleet of one gerp
#
# status and push refuse a profile whose account isn't the gerp row's; `bash scripts/awsacct.sh --all`
# writes the gerp-<gerp_id> profiles.
#   bash scripts/deploy.sh source          # the codebuild source zip (tower-per-customer)
#   bash scripts/deploy.sh stop  --gerp westwood-c40fd8   # export, then destroy the stack; the row reads stopped
#   bash scripts/deploy.sh start --gerp westwood-c40fd8   # apply it back; the row reads active
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/deploy.py "$@"
