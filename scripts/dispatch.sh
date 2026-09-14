#!/usr/bin/env bash
# dispatch.sh — start a workflow run and wait on it; one exit status for the whole run.
#
#   bash scripts/dispatch.sh deploy.yaml [-f gerp=westwood-c40fd8|all] [-f source_version=<v>]
#   bash scripts/dispatch.sh apply.yaml -f stack=per_customer -f gerp=westwood-c40fd8 [-f plan_only=true]
#
# Runs the workflow file from the current branch (DISPATCH_REF for another). Prints the run's url first,
# so a session can start it in the background and hear once, when the run ends; on a failure it prints
# the failing steps' log. `gh workflow run` names no run, and a new run takes a few seconds to be
# listed, so the run is the one of this workflow on this branch that wasn't there before the dispatch.
set -euo pipefail

workflow="${1:-}"
[[ -n "$workflow" ]] || { sed -n '4,5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
shift
ref="${DISPATCH_REF:-$(git -C "$(dirname "$0")" rev-parse --abbrev-ref HEAD)}"

before="$(gh run list --workflow "$workflow" --branch "$ref" --limit 50 --json databaseId --jq '.[].databaseId')"
gh workflow run "$workflow" --ref "$ref" "$@"

id=""
for _ in $(seq 1 60); do
  id="$(gh run list --workflow "$workflow" --branch "$ref" --event workflow_dispatch --limit 10 --json databaseId \
          --jq '.[].databaseId' | grep -vxF -f <(printf '%s\n' "$before" | sed '/^$/d'; echo 0) | head -1 || true)"
  [[ -n "$id" ]] && break
  sleep 2
done
[[ -n "$id" ]] || { echo "dispatch.sh: no new run of $workflow on $ref appeared within two minutes" >&2; exit 1; }

gh run view "$id" --json url --jq .url
if gh run watch "$id" --exit-status --interval 15 > /dev/null; then
  echo "run $id succeeded"
else
  echo "run $id failed:" >&2
  gh run view "$id" --log-failed | tail -120 >&2
  exit 1
fi
