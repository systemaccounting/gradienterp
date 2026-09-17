#!/usr/bin/env bash
# zip.sh — every zip a deploy makes. It builds and touches no AWS; putting a zip anywhere is the
# upload's job.
#
#   bash scripts/zip.sh source [--dirs <src-dir>…] [--out <path>]   # .build/source.zip: the working tree, for CodeBuild and the workflows
#   bash scripts/zip.sh lambda <src-dir>…            # .build/lambdas/<src-dir>.zip: the bytes deploy.sh pushes
#   bash scripts/zip.sh bff                          # .build/bff.zip: the owner web app's bundle
#
# Zips that aren't built here: the operator stacks' lambdas and the agent's chat lambda (terraform's
# archive_file, during the apply, from the tree the apply runs in), the agent image
# (`docker.sh --build`), and the providers CI mirrors (downloaded by tf-provider-mirror.sh).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO_ROOT/.build"
PY=python3
[[ -x "$REPO_ROOT/.venv/bin/python" ]] && PY="$REPO_ROOT/.venv/bin/python"

# CodeBuild applies these roots and resolves their providers fresh each build, so their lock files
# stay out: a lock pinning an older provider than the one that last wrote a gerp's state can refuse
# that state. The operator stacks' lock files go in, since those stacks apply against them.
CODEBUILD_LOCKS="prod/per_customer/.terraform.lock.hcl prod/init_customer/.terraform.lock.hcl prod/hub/.terraform.lock.hcl"

usage() { sed -n '4,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# the working tree as git sees it: tracked and untracked files, nothing gitignored (.env, *.tfstate,
# *.tfvars, tmp/, node_modules), plus source.json and deploy-dirs.txt at the zip's root
zip_source() {
  local dirs=() out="$BUILD/source.zip" stage commit dirty=false
  while (( $# )); do
    case "$1" in
      --dirs) shift; while (( $# )) && [[ "$1" != --* ]]; do dirs+=("$1"); shift; done ;;
      --out) out="$2"; shift 2 ;;   # elsewhere than .build: what upload.sh rebuilds to compare against
      *) usage ;;
    esac
  done
  git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "zip.sh source: $REPO_ROOT is not a git work tree, and git's file list is what the zip holds (an unzipped source.zip has no .git)" >&2
    exit 1
  }
  for d in ${dirs[@]+"${dirs[@]}"}; do
    [[ -d "$REPO_ROOT/$d" ]] || { echo "zip.sh source: --dirs $d is not a directory in $REPO_ROOT" >&2; exit 1; }
  done

  cd "$REPO_ROOT"
  commit="$(git rev-parse HEAD 2>/dev/null || echo "")"
  [[ -n "$(git status --porcelain)" ]] && dirty=true
  mkdir -p "$BUILD" "$(dirname "$out")"
  rm -f "$out"
  stage="$(mktemp -d)"
  "$PY" -c 'import json, sys; print(json.dumps({"commit": sys.argv[1], "dirty": sys.argv[2] == "true", "dirs": sys.argv[3:]}, indent=1))' \
    "$commit" "$dirty" ${dirs[@]+"${dirs[@]}"} > "$stage/source.json"
  : > "$stage/deploy-dirs.txt"
  for d in ${dirs[@]+"${dirs[@]}"}; do echo "$d" >> "$stage/deploy-dirs.txt"; done
  # a fixed time on the two written here, so an unchanged tree zips to the same bytes
  touch -t 198001010000 "$stage/source.json" "$stage/deploy-dirs.txt"

  # a tracked file deleted from the tree is still in git's list, and zip stops on it; symlinks go in
  # as links (-y); -X leaves out the owner and extended-time fields, which vary by machine
  git ls-files -co --exclude-standard \
    | awk 'NR == FNR { skip[$0] = 1; next } !($0 in skip)' <(git ls-files -d; printf '%s\n' $CODEBUILD_LOCKS) - \
    | zip -q -y -X "$out" -@
  (cd "$stage" && zip -q -X "$out" source.json deploy-dirs.txt)
  rm -rf "$stage"

  echo "==> ${out#"$REPO_ROOT/"}: $(du -h "$out" | cut -f1), $(( $(unzip -Z1 "$out" | wc -l) )) files, commit ${commit:0:12}$([[ $dirty == true ]] && echo ", uncommitted changes")"
}

case "${1:-}" in
  source) shift; zip_source "$@" ;;
  lambda) shift; (( $# )) || usage; exec "$PY" "$REPO_ROOT/scripts/deploy.py" build lambda "$@" ;;
  bff)    shift; (( $# == 0 )) || usage; exec "$PY" "$REPO_ROOT/scripts/deploy.py" build bff ;;
  *)      usage ;;
esac
