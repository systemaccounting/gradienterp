#!/usr/bin/env bash
# upload.sh — every put a deploy makes, from what zip.sh or docker.sh --build made. Operator
# credentials (operator-org, or --profile).
#
#   bash scripts/upload.sh source [--release]            # .build/source.zip → source.zip, refused when it is not this tree; --release, a committed tree → release/source.zip
#   bash scripts/upload.sh lambda <src-dir>… [--notes …] # .build/lambdas/<src-dir>.zip → the artifact bucket, when it changed
#   bash scripts/upload.sh bff [--notes …]               # .build/bff.zip → prod/gradienterp_cloud/bff.zip
#   bash scripts/upload.sh assets                        # prod/gradienterp_cloud/assets/ → the assets bucket, then a CloudFront invalidation
#   bash scripts/upload.sh image                         # the local agent image → ECR agentcore, under the next vNN tag
#
# release/source.zip is the CodeBuild projects' own location: a signup, a hub vend and a closure build
# from it. source.zip is every other upload, which `apply.sh` and the workflows take.
# Puts that aren't made here: openlyoperated.biz's files (its stack's apply), a gerp's playbooks
# (sync_playbooks.sh, in the per-customer build), the canonical schemas (the tower apply).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=python3
[[ -x "$REPO_ROOT/.venv/bin/python" ]] && PY="$REPO_ROOT/.venv/bin/python"

case "${1:-}" in
  source|lambda|bff|assets|image) exec "$PY" "$REPO_ROOT/scripts/deploy.py" upload "$@" ;;
  *) sed -n '5,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
