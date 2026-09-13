#!/usr/bin/env bash
# Minimal chat client for an AgentCore Runtime.
#
# Usage:
#   bash scripts/chat.sh "<prompt>"                       # uses last-used profile
#   bash scripts/chat.sh <profile> "<prompt>"             # sets + uses <profile>
#   bash scripts/chat.sh [<profile>] --new "<prompt>"     # forget prior session
#
# State (under ~/.gradienterp/):
#   default-profile        — last profile used; used when only a prompt is given
#   runtimes/<profile>     — cached agent runtime ARN per profile
#   sessions/<profile>-<runtime>  — session-id for multi-turn continuity
#
# Multi-turn carries the agent's memory across calls; `--new` resets the session.
#
# Examples:
#   bash scripts/chat.sh customer-gradienterp "what's my cash balance?"   # first time
#   bash scripts/chat.sh "list my journal entries"                        # reuses profile
#   bash scripts/chat.sh --new "fresh session"                            # same profile
#
# Env:
#   REGION=us-east-1 (default)

set -euo pipefail

REGION="${REGION:-us-east-1}"
STATE_BASE="${HOME}/.gradienterp"
mkdir -p "$STATE_BASE/runtimes" "$STATE_BASE/sessions"
DEFAULT_PROFILE_FILE="$STATE_BASE/default-profile"

# Strip --new from anywhere in the args.
NEW=false
ARGS=()
for a in "$@"; do
  if [[ "$a" == "--new" ]]; then NEW=true; else ARGS+=("$a"); fi
done

case ${#ARGS[@]} in
  1)
    PROMPT="${ARGS[0]}"
    if [[ -f "$DEFAULT_PROFILE_FILE" ]]; then
      PROFILE=$(<"$DEFAULT_PROFILE_FILE")
    else
      echo "no cached profile — first call: bash scripts/chat.sh <profile> \"<prompt>\"" >&2
      exit 1
    fi
    ;;
  2)
    PROFILE="${ARGS[0]}"
    PROMPT="${ARGS[1]}"
    echo "$PROFILE" > "$DEFAULT_PROFILE_FILE"
    ;;
  *)
    echo "usage: bash scripts/chat.sh [<profile>] [--new] <prompt>" >&2
    exit 1
    ;;
esac

# Resolve runtime ARN — cached per profile.
RUNTIME_FILE="${STATE_BASE}/runtimes/${PROFILE}"
if [[ -f "$RUNTIME_FILE" ]]; then
  RUNTIME_ARN=$(<"$RUNTIME_FILE")
else
  RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes \
    --region "$REGION" --profile "$PROFILE" --no-cli-pager \
    --query 'agentRuntimes[0].agentRuntimeArn' --output text)
  if [[ -z "$RUNTIME_ARN" || "$RUNTIME_ARN" == "None" ]]; then
    echo "no agent runtime found in account behind profile '$PROFILE'" >&2
    exit 1
  fi
  echo "$RUNTIME_ARN" > "$RUNTIME_FILE"
fi

# Session-id state: one file per (profile, runtime-id) tuple.
RUNTIME_ID="${RUNTIME_ARN##*/}"
STATE_FILE="${STATE_BASE}/sessions/${PROFILE}-${RUNTIME_ID}"

if [[ "$NEW" == "true" || ! -f "$STATE_FILE" ]]; then
  # AgentCore requires session-id ≥33 chars from [A-Za-z0-9-].
  # `uuidgen | tr -d '-'` is 32 chars; append one to clear the floor.
  SID="$(uuidgen | tr -d '-')a"
  echo "$SID" > "$STATE_FILE"
else
  SID=$(<"$STATE_FILE")
fi

# JSON-encode the prompt (handles quotes, newlines, etc.) then base64 the wrapper
PAYLOAD=$(printf '{"prompt":%s}' "$(printf '%s' "$PROMPT" | jq -Rs .)" | base64)

OUT=$(mktemp -t chat-XXXXXX.json)
ERR=$(mktemp -t chat-err-XXXXXX.txt)

invoke() {
  aws bedrock-agentcore invoke-agent-runtime \
    --agent-runtime-arn "$RUNTIME_ARN" \
    --runtime-session-id "$SID" \
    --payload "$PAYLOAD" \
    --content-type application/json \
    --region "$REGION" \
    --profile "$PROFILE" \
    --cli-read-timeout 300 \
    --no-cli-pager \
    "$OUT" >/dev/null 2>"$ERR"
}

if ! invoke; then
  # A recreated runtime gets a new id, stranding the cached ARN. On a
  # not-found, drop the cache, re-resolve, start a fresh session, retry once.
  if grep -q 'ResourceNotFound' "$ERR"; then
    echo "cached runtime stale — refreshing…" >&2
    rm -f "$RUNTIME_FILE"
    RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes \
      --region "$REGION" --profile "$PROFILE" --no-cli-pager \
      --query 'agentRuntimes[0].agentRuntimeArn' --output text)
    echo "$RUNTIME_ARN" > "$RUNTIME_FILE"
    SID="$(uuidgen | tr -d '-')a"
    echo "$SID" > "${STATE_BASE}/sessions/${PROFILE}-${RUNTIME_ARN##*/}"
    invoke || { cat "$ERR" >&2; rm -f "$OUT" "$ERR"; exit 1; }
  else
    cat "$ERR" >&2; rm -f "$OUT" "$ERR"; exit 1
  fi
fi
rm -f "$ERR"

# Pretty-print: the agent returns {response, session_id} on success
# or {error, ...} on failure. Show whichever's present.
if jq -e '.response' "$OUT" >/dev/null 2>&1; then
  jq -r '.response' "$OUT"
else
  jq '.' "$OUT"
fi

rm -f "$OUT"
