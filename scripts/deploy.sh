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
#   bash scripts/deploy.sh push --all --dirs <src-dir>    # every active gerp, the BFF once
#   bash scripts/deploy.sh push --deploy --dirs <src-dir> # no build: take the artifact upload.sh put
#   bash scripts/deploy.sh image [--gerp <id> | --all] [--no-build]
# the zips are zip.sh's and the puts upload.sh's; a gerp's stack, its stop and its start are apply.sh's.
set -euo pipefail
cd "$(dirname "$0")/.."
# the laptop's .venv; python3 where there is none (a GitHub runner with the requirements installed)
PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python
exec "$PY" scripts/deploy.py "$@"
