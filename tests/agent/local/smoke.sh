#!/usr/bin/env bash
# Run the live agent smoke test. Bootstraps .venv on first run.
# Hits the real Anthropic API — costs cents per run.
# Requires: export ANTHROPIC_API_KEY=...
#
# Usage:
#   bash tests/agent/local/smoke.sh                              # bookkeeper mode, prompts.jsonc
#   bash tests/agent/local/smoke.sh --category post,query        # filter (bookkeeper mode)
#   BUSINESS_NAME="Maria's Cafe" bash tests/agent/local/smoke.sh
#   AGENT_MODEL=claude-opus-4-7 bash tests/agent/local/smoke.sh

set -euo pipefail

MODE="bookkeeper"
PASS_THROUGH=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)  MODE="$2"; shift 2;;
        *)       PASS_THROUGH+=("$1"); shift;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
VENV="$REPO_ROOT/.venv"
SCRATCH="$REPO_ROOT/out/agent-smoke"
LOGS="$REPO_ROOT/logs/agent-smoke"

# fresh scratch per run
rm -rf "$SCRATCH" "$LOGS"
mkdir -p "$SCRATCH" "$LOGS"

# bootstrap venv + deps on first run
if [[ ! -d "$VENV" ]]; then
    echo "creating venv at $VENV" >&2
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
if ! python -c "import anthropic" 2>/dev/null; then
    echo "installing anthropic into $VENV" >&2
    pip install --quiet anthropic
fi

export LOCAL_LEDGER="$SCRATCH/ledger.jsonl"
export LOCAL_PENDING="$SCRATCH/pending.jsonl"
export LOCAL_BALANCES="$SCRATCH/balances.jsonl"
export LOCAL_CLASSIFICATIONS="$SCRATCH/classifications.jsonl"
export LOCAL_SECRETS="$SCRATCH/secrets.jsonl"
export LOCAL_COA_REQUESTS="$SCRATCH/coa-requests.jsonl"
export LOCAL_CONFIG="$SCRATCH/config.jsonl"
export LOCAL_S3="$SCRATCH/reports"
export LOCAL_LOGS="$LOGS"
unset AWS_LAMBDA_FUNCTION_NAME

export AGENT_MODE="$MODE"

case "$MODE" in
    bookkeeper)  PROMPTS="$REPO_ROOT/tests/agent/local/prompts.jsonc";;
    *) echo "unknown --mode: $MODE (expected: bookkeeper)" >&2; exit 1;;
esac

python "$REPO_ROOT/tests/agent/local/smoke.py" --prompts "$PROMPTS" "${PASS_THROUGH[@]}" 2>&1 | tee "$LOGS/transcript.txt"
