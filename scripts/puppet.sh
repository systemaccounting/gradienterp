#!/usr/bin/env bash
# puppet — play the OPPOSITE side of a cross-firm integration without a second gerp.
#
# The cross-firm boundary is the addressed event (detail.to on the shared gerp-events bus). A
# puppet has two halves: SEE events addressed to it (an SQS capture queue, tests/puppet), and EMIT
# events as itself (PutEvents). You are the brain between them — read what the tenant sent, make
# the counterparty's move.
#
# Infra (once per environment):
#   bash scripts/puppet.sh --acctid <operator-acct> --apply       # create the capture queue + rule
#   bash scripts/puppet.sh --acctid <operator-acct> --destroy     # tear it down
#
# Runtime (drive an integration test / puppeteer by hand):
#   bash scripts/puppet.sh --acctid <id> --poll --type offer.proposed --timeout 30
#   bash scripts/puppet.sh --acctid <id> --send --as puppet-westwood --to gradienterp \
#        --type offer.accepted --detail '{"thread":"…","terms_hash":"…"}'
#   bash scripts/puppet.sh --acctid <id> --receive                # drain the inbox once
#
# --acctid is the account that OWNS the gerp-events bus (the operator account) — REQUIRED, and
# verified against the caller creds before anything runs, so a wrong profile fails loudly. Other
# devs pass their own --acctid / --profile.
#
# A "puppet" is just a gerp_id starting with the puppet prefix (default "puppet-"): no provisioning.
# The operator dispatcher drops puppet-addressed events (unknown recipient), so they only ever reach
# this queue. A puppet's --send goes through the REAL dispatcher to the tenant's REAL inbox.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TFDIR="$REPO_ROOT/tests/puppet"

# ── defaults ──
PROFILE="operator-org"
REGION="us-east-1"
PREFIX="gerp"
NAMESPACE="puppet-"   # detail.to prefix the capture rule grabs; override for a demo-clean id (e.g. "tanners")
ACCTID=""
ACTION=""
SOURCE="puppet"
AS=""; TO=""; TYPE=""; DETAIL="{}"; TIMEOUT="60"

usage() {
  sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ── parse ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply|--destroy|--send|--receive|--poll) ACTION="${1#--}"; shift;;
    --acctid)  ACCTID="$2"; shift 2;;
    --profile)   PROFILE="$2"; shift 2;;
    --region)    REGION="$2"; shift 2;;
    --prefix)    PREFIX="$2"; shift 2;;
    --namespace) NAMESPACE="$2"; shift 2;;
    --as)      AS="$2"; shift 2;;
    --to)      TO="$2"; shift 2;;
    --type)    TYPE="$2"; shift 2;;
    --detail)  DETAIL="$2"; shift 2;;
    --source)  SOURCE="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    -h|--help) usage 0;;
    *) echo "unknown arg: $1" >&2; usage 1;;
  esac
done

[[ -z "$ACTION" ]] && { echo "no action (--apply/--destroy/--send/--receive/--poll)" >&2; usage 1; }
[[ -z "$ACCTID" ]] && { echo "--acctid <bus-owner account> is required" >&2; exit 1; }

aws() { command aws --no-cli-pager --region "$REGION" --profile "$PROFILE" "$@"; }

# creds must be for --acctid — refuse to touch the wrong account
who="$(aws sts get-caller-identity --query Account --output text)"
[[ "$who" == "$ACCTID" ]] || { echo "creds are for account $who, not --acctid $ACCTID (check --profile)" >&2; exit 1; }

# the hub of this region (config.json HUBS): sends go to its bus, the capture edge is a rule on it
HUB_BUS="$(jq -r --arg r "$REGION" '.HUBS[$r].bus_arn // empty' "$REPO_ROOT/config.json")"
HUB_ACCOUNT="$(jq -r --arg r "$REGION" '.HUBS[$r].account // empty' "$REPO_ROOT/config.json")"
[[ -z "$HUB_BUS" ]] && { echo "config.json HUBS has no hub for $REGION" >&2; exit 1; }
BUS="$HUB_BUS"
QURL="https://sqs.${REGION}.amazonaws.com/${ACCTID}/${PREFIX}-puppet-inbox"
QARN="arn:aws:sqs:${REGION}:${ACCTID}:${PREFIX}-puppet-inbox"
EDGE="${NAMESPACE%-}"

tf() { terraform -chdir="$TFDIR" "$@"; }

case "$ACTION" in
  apply)
    # the queue here; the capture edge on the hub, through its door
    AWS_PROFILE="$PROFILE" tf init -input=false >/dev/null
    AWS_PROFILE="$PROFILE" tf apply -input=false -auto-approve \
      -var "region=$REGION" -var "stack_prefix=$PREFIX" -var "puppet_prefix=$NAMESPACE" -var "hub_account=$HUB_ACCOUNT"
    AWS_PROFILE="$PROFILE" bash "$REPO_ROOT/scripts/edge.sh" "$REGION" add capture "$EDGE" "$QARN" \
      "$(jq -nc --arg p "$NAMESPACE" '{detail: {to: [{prefix: $p}]}}')"
    echo "puppet inbox ready: $QURL  (capturing detail.to prefix '$NAMESPACE' on the hub)"
    ;;

  destroy)
    AWS_PROFILE="$PROFILE" bash "$REPO_ROOT/scripts/edge.sh" "$REGION" remove capture "$EDGE" || true
    AWS_PROFILE="$PROFILE" tf destroy -input=false -auto-approve \
      -var "region=$REGION" -var "stack_prefix=$PREFIX" -var "puppet_prefix=$NAMESPACE" -var "hub_account=$HUB_ACCOUNT"
    ;;

  send)
    [[ -z "$TO" || -z "$TYPE" ]] && { echo "--send needs --to <gerp-id> and --type <detail_type> (and usually --as <puppet-id>)" >&2; exit 1; }
    detail="$(jq -c --arg f "$AS" --arg t "$TO" '. + (if $f == "" then {} else {from:$f} end) + {to:$t}' <<<"$DETAIL")"
    entries="$(jq -nc --arg s "$SOURCE" --arg dt "$TYPE" --arg d "$detail" --arg bus "$BUS" \
      '[{Source:$s, DetailType:$dt, Detail:$d, EventBusName:$bus}]')"
    resp="$(aws events put-events --entries "$entries")"
    if [[ "$(jq -r '.FailedEntryCount' <<<"$resp")" != "0" ]]; then
      echo "put-events FAILED: $resp" >&2; exit 1
    fi
    echo "sent $TYPE  ${AS:+from $AS }-> $TO  ($(jq -r '.Entries[0].EventId' <<<"$resp"))"
    ;;

  receive)
    resp="$(aws sqs receive-message --queue-url "$QURL" --max-number-of-messages 10 --wait-time-seconds 2)"
    n="$(jq -r '.Messages | length // 0' <<<"$resp")"
    if [[ "$n" == "0" || "$n" == "null" ]]; then echo "(inbox empty)"; exit 0; fi
    jq -c '.Messages[] | (.Body | fromjson) | {detail_type: .["detail-type"], from: .detail.from, to: .detail.to, detail: .detail}' <<<"$resp"
    jq -r '.Messages[].ReceiptHandle' <<<"$resp" | while read -r rh; do
      aws sqs delete-message --queue-url "$QURL" --receipt-handle "$rh"
    done
    ;;

  poll)
    deadline=$(( $(date +%s) + TIMEOUT ))
    while [[ "$(date +%s)" -lt "$deadline" ]]; do
      resp="$(aws sqs receive-message --queue-url "$QURL" --max-number-of-messages 10 --wait-time-seconds 10)"
      [[ "$(jq -r '.Messages | length // 0' <<<"$resp")" == "0" ]] && continue
      hit=""
      while read -r msg; do
        [[ -z "$msg" ]] && continue
        rh="$(jq -r '.ReceiptHandle' <<<"$msg")"
        ev="$(jq -c '(.Body | fromjson) | {detail_type: .["detail-type"], from: .detail.from, to: .detail.to, detail: .detail}' <<<"$msg")"
        dt="$(jq -r '.detail_type' <<<"$ev")"
        if [[ -z "$TYPE" || "$dt" == "$TYPE" ]]; then
          echo "$ev"
          aws sqs delete-message --queue-url "$QURL" --receipt-handle "$rh"
          hit=1
        else
          # not what we're waiting for — release it immediately for the next reader
          aws sqs change-message-visibility --queue-url "$QURL" --receipt-handle "$rh" --visibility-timeout 0
        fi
      done < <(jq -c '.Messages[]' <<<"$resp")
      [[ -n "$hit" ]] && exit 0
    done
    echo "poll: timed out after ${TIMEOUT}s waiting for ${TYPE:-any event}" >&2
    exit 1
    ;;
esac
