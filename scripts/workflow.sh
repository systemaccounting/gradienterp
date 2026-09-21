#!/usr/bin/env bash
# workflow.sh — GitHub workflow runs: start one on the working tree, wait on runs, read their logs.
#
#   bash scripts/workflow.sh run <workflow> [--dirs <src-dir>…] [-f key=value …] [--no-wait]
#   bash scripts/workflow.sh wait <run-id>
#   bash scripts/workflow.sh wait --commit [<sha>] [--event push|pull_request]
#   bash scripts/workflow.sh log <run-id> [--failed]
#
# run   A workflow with no `source_version` input (playbooks.yaml) runs on its checkout: nothing is uploaded and
#       `--dirs` is refused. Otherwise, unless `-f source_version=` names an upload, zips the tree (`zip.sh source`, with `--dirs`), uploads
#       it (`upload.sh source`) and dispatches on the version the upload printed, so the run takes this tree
#       even if another upload lands before it starts. A deploy.yaml run naming nothing to deploy — no
#       `--dirs`, no `-f image=` — is refused before anything uploads. Runs the workflow file from the
#       current branch (WORKFLOW_REF for another), prints the new run's id and url, and waits on it
#       unless `--no-wait`.
# wait  One run: its url, then its result as the exit status, and its failing steps on a failure.
#       `--commit` (default HEAD, event push): every workflow whose `on:` names the event with no `paths:`
#       filter (one with a filter runs only when the commit touched a matching path), once each is
#       listed for that commit — a push's runs take seconds to appear — waited on at once, one line per
#       workflow, the failing steps of each that failed.
# log   The run's log, or with `--failed` its failing steps alone.
#
# Start it in the background from a session and hear once, when the run or the commit's runs end.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPTS/.." && pwd)"
# how long a push's or a dispatch's runs get to be listed
APPEAR_SECONDS="${WORKFLOW_APPEAR_SECONDS:-120}"

usage() { sed -n '4,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

log_run() {
  local id="$1" flag="--log"
  [[ "${2:-}" == --failed ]] && flag="--log-failed"
  gh run view "$id" "$flag"
}

wait_run() {
  local id="$1"
  gh run view "$id" --json url --jq .url
  if gh run watch "$id" --exit-status --interval 15 > /dev/null; then
    echo "run $id succeeded"
    return 0
  fi
  echo "run $id $(gh run view "$id" --json conclusion --jq .conclusion):" >&2
  log_run "$id" --failed | tail -120 >&2
  return 1
}

# the events a workflow file's top-level `on:` names, one per line
triggers() {
  # the events the file's `on:` names, less any that carries a `paths:` filter: whether such a
  # trigger starts a run depends on what the commit touched, so a wait cannot expect it
  awk '/^on:/ { inon = 1; next }
       inon && /^[^ ]/ { inon = 0 }
       inon && /^  [a-z_]+:/ { k = $0; sub(/^  /, "", k); sub(/:.*/, "", k); keys[++n] = k; next }
       inon && /^    paths:/ { paths[k] = 1 }
       END { for (i = 1; i <= n; i++) if (!(keys[i] in paths)) print keys[i] }' "$1"
}

workflow_name() {
  local name
  name="$(sed -n 's/^name: *//p' "$1" | head -1)"
  echo "${name:-$(basename "$1" .yaml)}"
}

wait_commit() {
  local sha="$1" event="$2" expected=() listed missing ids=() pids=() failed=() deadline
  for f in "$REPO"/.github/workflows/*.yaml; do
    if triggers "$f" | grep -qx "$event"; then expected+=("$(workflow_name "$f")"); fi
  done
  if (( ${#expected[@]} == 0 )); then echo "no workflow runs on $event"; return 0; fi

  deadline=$(( $(date +%s) + APPEAR_SECONDS ))
  while :; do
    listed="$(gh run list --commit "$sha" --event "$event" --limit 50 --json databaseId,workflowName \
                --jq '.[] | "\(.workflowName)\t\(.databaseId)"')"
    missing=()
    for w in "${expected[@]}"; do
      grep -q "^$w	" <<< "$listed" || missing+=("$w")
    done
    (( ${#missing[@]} == 0 )) && break
    if (( $(date +%s) >= deadline )); then
      echo "workflow.sh: no $event run of ${missing[*]} for ${sha:0:12} was listed within ${APPEAR_SECONDS}s" >&2
      return 1
    fi
    sleep 3
  done

  # the newest of each workflow's runs: a re-run lists the commit twice
  for w in "${expected[@]}"; do ids+=("$(grep "^$w	" <<< "$listed" | head -1 | cut -f2)"); done
  for id in "${ids[@]}"; do
    gh run watch "$id" --exit-status --interval 15 > /dev/null 2>&1 &
    pids+=("$!")
  done
  for i in "${!ids[@]}"; do
    wait "${pids[$i]}" || failed+=("$i")
    echo "${expected[$i]} $(gh run view "${ids[$i]}" --json conclusion --jq .conclusion) (run ${ids[$i]})"
  done
  for i in ${failed[@]+"${failed[@]}"}; do
    echo "── ${expected[$i]}, run ${ids[$i]}:" >&2
    log_run "${ids[$i]}" --failed | tail -80 >&2
  done
  (( ${#failed[@]} == 0 ))
}

run_workflow() {
  local workflow="${1:-}" dirs=() fields=() version="" image="" no_wait="" uploaded ref before id=""
  [[ -n "$workflow" && "$workflow" != -* ]] || usage
  shift
  while (( $# )); do
    case "$1" in
      # a value that arrived as one word holding several ("a b c", zsh's unsplit $var) is split here,
      # so a caller from any shell means the same thing
      "--dirs "*|"-f "*) set -- $1 "${@:2}" ;;
      --dirs)    shift; while (( $# )) && [[ "$1" != -* ]]; do for w in $1; do dirs+=("$w"); done; shift; done ;;
      --no-wait) no_wait=1; shift ;;
      -f)        fields+=(-f "$2")
                 case "$2" in source_version=*) version="${2#source_version=}" ;; image=*) image="${2#image=}" ;; esac
                 shift 2 ;;
      *)         echo "workflow.sh run: unknown argument $1" >&2; exit 2 ;;
    esac
  done

  # a workflow with no `source_version` input runs on its checkout: nothing to zip or upload, and
  # no --dirs to name
  if ! grep -Eq '^\s+source_version:' "$REPO/.github/workflows/$workflow"; then
    (( ${#dirs[@]} == 0 )) || { echo "workflow.sh run: $workflow runs on its checkout and takes no --dirs" >&2; exit 2; }
    [[ -z "$version" ]] || { echo "workflow.sh run: $workflow runs on its checkout and takes no source_version" >&2; exit 2; }
  elif [[ -z "$version" ]]; then
    if [[ "$workflow" == deploy.yaml && ${#dirs[@]} -eq 0 && ( -z "$image" || "$image" == none ) ]]; then
      echo "workflow.sh run: deploy.yaml with no --dirs and no -f image=… would deploy nothing" >&2
      exit 2
    fi
    bash "$SCRIPTS/zip.sh" source ${dirs[@]+--dirs "${dirs[@]}"}
    uploaded="$(bash "$SCRIPTS/upload.sh" source | tee /dev/stderr)"
    version="$(printf '%s\n' "$uploaded" | sed -n 's/^source\.zip: .*version \([^ ]*\)$/\1/p' | tail -1)"
    [[ -n "$version" ]] || { echo "workflow.sh run: upload.sh printed no version" >&2; exit 1; }
    fields+=(-f "source_version=$version")
  elif (( ${#dirs[@]} )); then
    echo "workflow.sh run: --dirs name what a new zip carries; source_version=$version already names a zip" >&2
    exit 2
  fi

  ref="${WORKFLOW_REF:-$(git -C "$REPO" rev-parse --abbrev-ref HEAD)}"
  before="$(gh run list --workflow "$workflow" --branch "$ref" --limit 50 --json databaseId --jq '.[].databaseId')"
  gh workflow run "$workflow" --ref "$ref" "${fields[@]}"

  # `gh workflow run` names no run: the run is the one of this workflow on this branch that wasn't
  # listed before the dispatch
  local deadline=$(( $(date +%s) + APPEAR_SECONDS ))
  while :; do
    id="$(gh run list --workflow "$workflow" --branch "$ref" --event workflow_dispatch --limit 10 --json databaseId \
            --jq '.[].databaseId' | grep -vxF -f <(printf '%s\n' "$before" | sed '/^$/d'; echo 0) | head -1 || true)"
    [[ -n "$id" ]] && break
    (( $(date +%s) < deadline )) || { echo "workflow.sh run: no new run of $workflow on $ref was listed within ${APPEAR_SECONDS}s" >&2; exit 1; }
    sleep 2
  done

  echo "run $id"
  if [[ -n "$no_wait" ]]; then
    gh run view "$id" --json url --jq .url
    return 0
  fi
  wait_run "$id"
}

verb="${1:-}"
(( $# )) && shift
case "$verb" in
  run)  run_workflow "$@" ;;
  wait)
    if [[ "${1:-}" == --commit ]]; then
      shift
      sha="HEAD" event="push"
      if (( $# )) && [[ "$1" != --* ]]; then sha="$1"; shift; fi
      while (( $# )); do
        case "$1" in
          --event) event="${2:?--event takes push or pull_request}"; shift 2 ;;
          *) usage ;;
        esac
      done
      [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || sha="$(git -C "$REPO" rev-parse "$sha")"
      wait_commit "$sha" "$event"
    else
      [[ "${1:-}" =~ ^[0-9]+$ ]] || usage
      wait_run "$1"
    fi ;;
  log)
    [[ "${1:-}" =~ ^[0-9]+$ ]] || usage
    log_run "$@" ;;
  *) usage ;;
esac
