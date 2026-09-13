#!/usr/bin/env bash
# Ingest the repo's playbook docs into a customer's Bedrock Knowledge Base via
# INLINE documents on a CUSTOM data source — the "Design B" direct-ingest path
# (no S3 staging bucket). See kb-playbooks-migration.md.
#
# Every modules/**/kb.md is a natural-language agent playbook. Each
# becomes ONE inline document whose stable id is its repo-relative path
# (e.g. modules/payments/stripe/kb.md), so re-running this UPSERTS
# rather than duplicating.
#
# Usage:
#   bash scripts/sync_playbooks.sh [--dry-run] <gerp_id> <kb_id> <data_source_id> [profile] [region]
#
# Args:
#   gerp_id          tenant id (e.g. gradienterp) — also derives the default profile
#   kb_id            the customer KB id          (terraform output: playbook_kb_id)
#   data_source_id   the CUSTOM data source id   (terraform output: playbook_data_source_id)
#   profile          AWS named profile; default = customer-<gerp_id>-via-org; `env` = the ambient
#                    credentials (CodeBuild, after assuming OperatorOrchestration in the account)
#   region           default = us-east-1
#   --dry-run        print what would be ingested and pruned; call nothing that writes
#
# Profile / creds:
#   The default profile customer-<gerp_id>-via-org chains
#     default (management) -> OrganizationAccountAccessRole@operator
#                          -> OperatorOrchestration@customer
#   i.e. it lands IN the customer's sub-account — the SAME assume chain the
#   canonical-schema reseed uses — so these bedrock-agent calls hit the
#   customer's own KB. (operator-org would stop in the operator account; it is
#   only used as AWS_PROFILE for terraform, whose provider does its own
#   OperatorOrchestration assume. A raw CLI call has no such hop, so it needs
#   the -via-org profile.)
#
# Notes:
#   - CUSTOM data source + IN_LINE content: ingest-knowledge-base-documents
#     ingests directly. NO separate start-ingestion-job is required.
#   - inlineContent.type = TEXT (plain UTF-8 markdown carried in
#     textContent.data; BYTE is for base64 byteContent + mimeType).
#   - One document per call — gives per-file success/failure and sidesteps any
#     per-request document cap. With a handful of playbooks this is cheap.
#   - Idempotent: the stable customDocumentIdentifier.id makes re-runs upsert.
#   - Upsert alone is not a sync: a playbook DELETED from the repo would stay
#     indexed and retrievable by search_guides forever. So the second half of
#     this script prunes — it lists the indexed documents and deletes any whose
#     file is gone. Because the ids are the repo paths, that diff is a set
#     difference, and because nothing else ingests into this data source, a
#     document with no file behind it is stale by definition.

set -euo pipefail

usage() {
  echo "usage: bash scripts/sync_playbooks.sh [--dry-run] <gerp_id> <kb_id> <data_source_id> [profile] [region]" >&2
  exit 1
}

DRY_RUN=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    -*)        echo "error: unknown flag $a" >&2; usage ;;
    *)         ARGS+=("$a") ;;
  esac
done
if [ "${#ARGS[@]}" -gt 0 ]; then set -- "${ARGS[@]}"; else set --; fi

GERP_ID="${1:-}"; [ -n "$GERP_ID" ] || usage
KB_ID="${2:-}";   [ -n "$KB_ID" ]   || usage
DS_ID="${3:-}";   [ -n "$DS_ID" ]   || usage
PROFILE="${4:-customer-${GERP_ID}-via-org}"
# `env` = the ambient credentials (CodeBuild after an assume-role into the customer account); any
# other value is a named profile
if [ "$PROFILE" = "env" ]; then PROFILE_ARGS=(); else PROFILE_ARGS=(--profile "$PROFILE"); fi
REGION="${5:-us-east-1}"

command -v jq  >/dev/null 2>&1 || { echo "error: jq is required"  >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "error: aws is required" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> sync playbooks -> KB=$KB_ID  data_source=$DS_ID"
echo "    gerp_id=$GERP_ID  profile=$PROFILE  region=$REGION"
echo "    repo=$REPO_ROOT"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "    DRY RUN — nothing will be ingested or deleted"
fi

# Confirm we actually landed in the customer account (fail fast on a bad chain).
ACCOUNT="$(aws sts get-caller-identity \
  "${PROFILE_ARGS[@]}" --region "$REGION" --no-cli-pager \
  --query Account --output text)"
echo "    caller account: $ACCOUNT"

# Collect playbooks as repo-relative paths (the stable document ids), sorted for
# deterministic ordering. -print0/read -d '' is robust and bash-3.2 safe.
FILES=()
while IFS= read -r -d '' f; do
  FILES+=("$f")
done < <(cd "$REPO_ROOT" && find modules -type f -name kb.md -print0 | sort -z)

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "error: no modules/**/kb.md playbooks found under $REPO_ROOT" >&2
  exit 1
fi
echo "==> found ${#FILES[@]} playbook(s)"

OK=0
FAIL=0
for REL in "${FILES[@]}"; do
  ABS="$REPO_ROOT/$REL"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "--> would ingest: $REL"
    continue
  fi
  echo "--> ingesting: $REL"

  # Build the request body with jq so the markdown is embedded as a proper JSON
  # string (--rawfile slurps the whole file -> JSON string; no hand-escaping).
  PAYLOAD="$(jq -n \
    --arg kb   "$KB_ID" \
    --arg ds   "$DS_ID" \
    --arg id   "$REL" \
    --rawfile  data "$ABS" \
    '{
       knowledgeBaseId: $kb,
       dataSourceId:    $ds,
       documents: [
         {
           content: {
             dataSourceType: "CUSTOM",
             custom: {
               sourceType: "IN_LINE",
               customDocumentIdentifier: { id: $id },
               inlineContent: {
                 type: "TEXT",
                 textContent: { data: $data }
               }
             }
           }
         }
       ]
     }')"

  if RESP="$(aws bedrock-agent ingest-knowledge-base-documents \
        --cli-input-json "$PAYLOAD" \
        "${PROFILE_ARGS[@]}" --region "$REGION" --no-cli-pager 2>&1)"; then
    # documentDetails[].status is e.g. STARTING / IN_PROGRESS / INDEXED.
    STATUS="$(printf '%s' "$RESP" | jq -r '.documentDetails[0].status // "ACCEPTED"' 2>/dev/null || echo ACCEPTED)"
    echo "    ok ($STATUS)"
    OK=$((OK + 1))
  else
    echo "    FAILED:" >&2
    printf '%s\n' "$RESP" | sed 's/^/      /' >&2
    FAIL=$((FAIL + 1))
  fi
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> would ingest ${#FILES[@]} playbook(s)"
else
  echo "==> ingested: $OK ok, $FAIL failed (of ${#FILES[@]})"
fi
# Status lands directly for CUSTOM/inline docs; no start-ingestion-job needed.

# ---------------------------------------------------------------------------
# prune: delete indexed documents whose file is no longer in the repo.
#
# The document id IS the repo-relative path, so this is a set difference. The
# FILES-empty check above is the safety rail: a bad repo root aborts before it
# can conclude that every playbook is stale.
# ---------------------------------------------------------------------------
echo "==> prune: listing indexed documents"

INDEXED=()
NEXT=""
while :; do
  if [ -n "$NEXT" ]; then
    PAGE="$(aws bedrock-agent list-knowledge-base-documents \
      --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
      --max-results 100 --next-token "$NEXT" \
      "${PROFILE_ARGS[@]}" --region "$REGION" --no-cli-pager)"
  else
    PAGE="$(aws bedrock-agent list-knowledge-base-documents \
      --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
      --max-results 100 \
      "${PROFILE_ARGS[@]}" --region "$REGION" --no-cli-pager)"
  fi

  while IFS= read -r ID; do
    if [ -n "$ID" ]; then
      INDEXED+=("$ID")
    fi
  done < <(printf '%s' "$PAGE" | jq -r '.documentDetails[].identifier.custom.id // empty')

  NEXT="$(printf '%s' "$PAGE" | jq -r '.nextToken // empty')"
  if [ -z "$NEXT" ]; then
    break
  fi
done

# Empty-array expansion under `set -u` is an error in bash 3.2, hence the counts.
STALE=()
if [ "${#INDEXED[@]}" -gt 0 ]; then
  for ID in "${INDEXED[@]}"; do
    ON_DISK=0
    for REL in "${FILES[@]}"; do
      if [ "$ID" = "$REL" ]; then
        ON_DISK=1
        break
      fi
    done
    if [ "$ON_DISK" -eq 0 ]; then
      STALE+=("$ID")
    fi
  done
fi

echo "    ${#INDEXED[@]} indexed, ${#FILES[@]} on disk, ${#STALE[@]} stale"

PRUNED=0
if [ "${#STALE[@]}" -gt 0 ]; then
  for ID in "${STALE[@]}"; do
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "--> would delete: $ID"
      continue
    fi
    echo "--> deleting: $ID"

    IDENTS="$(jq -nc --arg id "$ID" '[{ dataSourceType: "CUSTOM", custom: { id: $id } }]')"
    if RESP="$(aws bedrock-agent delete-knowledge-base-documents \
          --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
          --document-identifiers "$IDENTS" \
          "${PROFILE_ARGS[@]}" --region "$REGION" --no-cli-pager 2>&1)"; then
      STATUS="$(printf '%s' "$RESP" | jq -r '.documentDetails[0].status // "DELETING"' 2>/dev/null || echo DELETING)"
      echo "    ok ($STATUS)"
      PRUNED=$((PRUNED + 1))
    else
      echo "    FAILED:" >&2
      printf '%s\n' "$RESP" | sed 's/^/      /' >&2
      FAIL=$((FAIL + 1))
    fi
  done
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> dry run: ${#FILES[@]} to ingest, ${#STALE[@]} to prune. Nothing was written."
else
  echo "==> done: $OK ingested, $PRUNED pruned, $FAIL failed"
fi
# To inspect later:
#   aws bedrock-agent list-knowledge-base-documents \
#     --knowledge-base-id $KB_ID --data-source-id $DS_ID \
#     --profile $PROFILE --region $REGION --no-cli-pager
[ "$FAIL" -eq 0 ]
