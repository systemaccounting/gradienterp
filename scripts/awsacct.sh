#!/usr/bin/env bash
# The AWS CLI profiles for each account — thin wrapper over scripts/awsacct.py (see its docstring).
#
#   bash scripts/awsacct.sh <target>   # [profile current] → management | operator | hub:<region> | <gerp_id>
#   bash scripts/awsacct.sh --list     # the targets, with account and region
#   bash scripts/awsacct.sh --all      # a named profile per target: operator-org, hub-<region>, gerp-<gerp_id>
#
# Standard library only, so it runs before `scripts/local-dev.sh --install` has made .venv.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/awsacct.py "$@"
