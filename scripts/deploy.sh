#!/usr/bin/env bash
# Artifact deploys — thin wrapper over scripts/deploy.py (see its docstring), and the fleet pipe.
#
#   bash scripts/deploy.sh status                         # gradienterp's gerp, through gerp-gradienterp
#   bash scripts/deploy.sh status --gerp westwood-c40fd8 --all
#   bash scripts/deploy.sh push --dirs modules/shipping/lambdas/manage_shipments --notes "..."
#   bash scripts/deploy.sh push --gerp westwood-c40fd8     # whole fleet of one gerp
#   bash scripts/deploy.sh push --deploy --dirs <src-dir> # no build: take the artifact upload.sh put
#   bash scripts/deploy.sh image [--gerp <id>] [--no-build]
#   bash scripts/deploy.sh fleet [--report] [--gerp a,b] [--dirs <src-dir>…]
#
# status and push refuse a profile whose account isn't the gerp row's; `bash scripts/awsacct.sh --all`
# writes the gerp-<gerp_id> profiles.
#
# `fleet` is the laptop's composition of scripts/fleet.py's pieces (issue #57): the snapshot of the
# bucket once, every active gerp's functions ten at a time, the compare, and one update per function
# that differs; `--report` stops after the compare, and the report is .build/fleet.tsv either way.
# The workflow yaml runs the same pipe on a runner (deploy.yaml): keep the two identical in logic —
# the same pieces in the same order with the same flags; a runner thing (`parallel`, `::group::`,
# the step summary) is written in the yaml, never here.
# the zips are zip.sh's and the puts upload.sh's; a gerp's stack, its stop and its start are apply.sh's.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python

if [[ "${1:-}" == fleet ]]; then
  shift
  report=0; only=""; dirs=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --report) report=1; shift ;;
      --gerp) only="$2"; shift 2 ;;
      --dirs) shift; while [[ $# -gt 0 && "$1" != --* ]]; do dirs+=("$1"); shift; done ;;
      *) echo "fleet: unknown argument $1" >&2; exit 2 ;;
    esac
  done
  mkdir -p .build
  snapshot=.build/fleet-snapshot.tsv
  "$PY" scripts/fleet.py listzipversions > "$snapshot"                                  # once, before any account: the run's target
  echo "==> snapshot: $(wc -l < "$snapshot" | tr -d ' ') artifacts" >&2
  "$PY" scripts/fleet.py listgerps | cut -f1 \
    | { if [[ -n "$only" ]]; then grep -Fx -f <(tr ',' '\n' <<< "$only"); else cat; fi; } \
    | xargs -P 10 -I{} "$PY" scripts/fleet.py listzipfnsconf --gerp {} \
    | "$PY" scripts/fleet.py status "$snapshot" ${dirs[@]:+--dirs "${dirs[@]}"} \
    | tee .build/fleet.tsv \
    | { if [[ $report -eq 1 ]]; then cat; else "$PY" scripts/fleet.py update; fi; }
  exit
fi

exec "$PY" scripts/deploy.py "$@"
