#!/usr/bin/env bash
# apply.sh — every terraform apply the operator starts, the same on the laptop or a GitHub runner.
#
#   bash scripts/apply.sh --stack per_customer --gerp <gerp_id>|all [--action apply|stop] [--source-version V | --build] [--plan]
#   bash scripts/apply.sh --stack hub --region <region> [--source-version V | --build] [--plan]
#   bash scripts/apply.sh --stack <api_openlyoperated|dns|email|gradienterp_cloud|openlyoperated_biz|optimizer|platform/operator|tower|platform/management> [--plan]
#
# per_customer and hub build in CodeBuild from the latest `upload.sh source` (--source-version for
# another; --build zips and uploads this tree first); the operator stacks run terraform in place, as
# the default profile. See scripts/apply.py.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python
exec "$PY" scripts/apply.py "$@"
