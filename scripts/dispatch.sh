#!/usr/bin/env bash
# dispatch.sh — upload the working tree, start a workflow run on it, and wait; one exit status for the run.
#
#   bash scripts/dispatch.sh deploy.yaml --dirs <src-dir>… [-f gerp=<id>|<id>,<id>|all]
#   bash scripts/dispatch.sh deploy.yaml -f image=build|no-build [-f gerp=…] [--dirs <src-dir>…]
#   bash scripts/dispatch.sh apply.yaml -f stack=per_customer -f gerp=westwood-c40fd8 [-f plan_only=true]
#
# Unless a `-f source_version=` names one, it zips the tree (`zip.sh source`, with `--dirs`), uploads it
# (`upload.sh source`) and dispatches with the version the upload printed, so the run takes this tree even
# if another upload lands before it starts. A deploy.yaml dispatch with no `--dirs` and no image would
# deploy nothing, and is refused before anything uploads.
#
# Runs the workflow file from the current branch (DISPATCH_REF for another). Prints the run's url first,
# so a session can start it in the background and hear once, when the run ends; on a failure it prints
# the failing steps' log. `gh workflow run` names no run, and a new run takes a few seconds to be
# listed, so the run is the one of this workflow on this branch that wasn't there before the dispatch.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

workflow="${1:-}"
[[ -n "$workflow" && "$workflow" != -* ]] || { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
shift

dirs=() fields=() version="" image=""
while (( $# )); do
  case "$1" in
    --dirs) shift; while (( $# )) && [[ "$1" != -* ]]; do dirs+=("$1"); shift; done ;;
    -f)     fields+=(-f "$2")
            case "$2" in source_version=*) version="${2#source_version=}" ;; image=*) image="${2#image=}" ;; esac
            shift 2 ;;
    *)      echo "dispatch.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$version" ]]; then
  if [[ "$workflow" == deploy.yaml && ${#dirs[@]} -eq 0 && ( -z "$image" || "$image" == none ) ]]; then
    echo "dispatch.sh: deploy.yaml with no --dirs and no -f image=… would deploy nothing" >&2
    exit 2
  fi
  bash "$SCRIPTS/zip.sh" source ${dirs[@]+--dirs "${dirs[@]}"}
  uploaded="$(bash "$SCRIPTS/upload.sh" source | tee /dev/stderr)"
  version="$(printf '%s\n' "$uploaded" | sed -n 's/^source\.zip: .*version \([^ ]*\)$/\1/p' | tail -1)"
  [[ -n "$version" ]] || { echo "dispatch.sh: upload.sh printed no version" >&2; exit 1; }
  fields+=(-f "source_version=$version")
elif (( ${#dirs[@]} )); then
  echo "dispatch.sh: --dirs name what a new zip carries; source_version=$version already names a zip" >&2
  exit 2
fi

ref="${DISPATCH_REF:-$(git -C "$SCRIPTS" rev-parse --abbrev-ref HEAD)}"
before="$(gh run list --workflow "$workflow" --branch "$ref" --limit 50 --json databaseId --jq '.[].databaseId')"
gh workflow run "$workflow" --ref "$ref" "${fields[@]}"

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
