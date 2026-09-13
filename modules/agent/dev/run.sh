#!/usr/bin/env bash
# Launch the local dev agent against a scratch dir under out/agent-dev/.
# Bootstraps a project-local venv at .venv/ on first run.
# Requires: export ANTHROPIC_API_KEY=...
#
# Usage:
#   bash modules/agent/dev/run.sh
#   BUSINESS_NAME="Maria's Cafe" bash modules/agent/dev/run.sh
#   AGENT_MODEL=claude-opus-4-7 bash modules/agent/dev/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
VENV="$REPO_ROOT/.venv"
SCRATCH="$REPO_ROOT/out/agent-dev"
LOGS="$REPO_ROOT/logs/agent-dev"

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

mkdir -p "$SCRATCH" "$LOGS"

export LOCAL_LEDGER="$SCRATCH/ledger.jsonl"
export LOCAL_PENDING="$SCRATCH/pending.jsonl"
export LOCAL_BALANCES="$SCRATCH/balances.jsonl"
export LOCAL_CLASSIFICATIONS="$SCRATCH/classifications.jsonl"
export LOCAL_S3="$SCRATCH/reports"
export LOCAL_LOGS="$LOGS"

# ensure lambdas take the local path
unset AWS_LAMBDA_FUNCTION_NAME

python "$REPO_ROOT/modules/agent/dev/agent.py"
