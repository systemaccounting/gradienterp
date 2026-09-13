#!/usr/bin/env bash
# edge.sh — a person's door to a hub's edges (prod/hub manage_edges).
#
#   bash scripts/edge.sh <hub> list [kind]
#   bash scripts/edge.sh <hub> add spoke <gerp_id> <gerp_account_id>
#   bash scripts/edge.sh <hub> add capture <label> <queue-arn> '<event pattern json>'
#   bash scripts/edge.sh <hub> remove <kind> <to>
#
# <hub> is a key of config.json HUBS (a region). Runs as the operator account (AWS_PROFILE,
# default operator-org): the hub's door admits the operator account.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUB="${1:?hub (a key of config.json HUBS)}"; OP="${2:?list | add | remove}"; shift 2
export AWS_PROFILE="${AWS_PROFILE:-operator-org}"
DOOR=$(jq -r --arg h "$HUB" '.HUBS[$h].manage_edges_arn // empty' "$REPO_ROOT/config.json")
test -n "$DOOR" || { echo "config.json HUBS has no $HUB (or no manage_edges_arn)"; exit 1; }
# a function is invoked through its own region's endpoint
HUB_REGION=$(jq -r --arg h "$HUB" '.HUBS[$h].region // "us-east-1"' "$REPO_ROOT/config.json")

case "$OP" in
  list)   PAYLOAD=$(jq -n --arg k "${1:-}" '{op: "list"} + (if $k != "" then {kind: $k} else {} end)') ;;
  remove) PAYLOAD=$(jq -n --arg k "${1:?kind}" --arg t "${2:?to}" '{op: "remove", kind: $k, to: $t}') ;;
  add)
    KIND="${1:?kind}"; TO="${2:?to}"
    case "$KIND" in
      spoke)   PAYLOAD=$(jq -n --arg t "$TO" --arg a "${3:?the gerp account id}" '{op: "add", kind: "spoke", to: $t, account_id: $a}') ;;
      capture) PAYLOAD=$(jq -n --arg t "$TO" --arg a "${3:?the queue arn}" --argjson p "${4:?the event pattern json}" '{op: "add", kind: "capture", to: $t, target: $a, pattern: $p}') ;;
      *) echo "kind must be spoke or capture"; exit 1 ;;
    esac ;;
  *) echo "op must be list, add or remove"; exit 1 ;;
esac

OUT=$(mktemp)
aws lambda invoke --region "$HUB_REGION" --function-name "$DOOR" --cli-binary-format raw-in-base64-out \
  --payload "$PAYLOAD" --no-cli-pager "$OUT" >/dev/null
jq -r '.statusCode as $s | (.body | fromjson) | . + {statusCode: $s}' "$OUT"
rm -f "$OUT"
