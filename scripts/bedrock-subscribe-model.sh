#!/usr/bin/env bash
# Subscribe an account to a Bedrock foundation model.
#
# AWS retired the "Model Access" console page; per-account access now requires
# (1) the account-level Anthropic use-case form — once per account, covers every
# anthropic model, `aws bedrock put-use-case-for-model-access --form-data <json>`
# (read the one on file with get-use-case-for-model-access) — and (2) a per-model
# Marketplace agreement, which this script creates via API. Both are API calls, so
# both belong in provision_customer; this script is the by-hand version.
#
# Run once per (account, model). Polls until the agreement flips to AVAILABLE.
#
# Usage:
#   bash scripts/bedrock-subscribe-model.sh <model-id> <aws-profile>
#
# Examples:
#   bash scripts/bedrock-subscribe-model.sh anthropic.claude-sonnet-4-6 customer-gradienterp
#   bash scripts/bedrock-subscribe-model.sh anthropic.claude-opus-4-7   operator
#
# Env:
#   REGION=us-east-1 (default)

set -euo pipefail

MODEL_ID="${1:?usage: bash scripts/bedrock-subscribe-model.sh <model-id> <aws-profile>}"
PROFILE="${2:?usage: bash scripts/bedrock-subscribe-model.sh <model-id> <aws-profile>}"
REGION="${REGION:-us-east-1}"

ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

# Skip if already AVAILABLE — idempotent.
STATUS=$(aws bedrock get-foundation-model-availability \
  --model-id "$MODEL_ID" --region "$REGION" --profile "$PROFILE" \
  --query 'agreementAvailability.status' --output text 2>/dev/null || echo "UNKNOWN")

if [[ "$STATUS" == "AVAILABLE" ]]; then
  echo "$MODEL_ID already AVAILABLE on $ACCOUNT"
  exit 0
fi

# authorizationStatus must be AUTHORIZED before the agreement step works.
# AUTHORIZED comes from the Anthropic use-case form, once per account:
#   aws bedrock put-use-case-for-model-access --form-data "$(aws bedrock get-use-case-for-model-access --profile <any-account-that-has-it> --query formData --output text | base64 -d)"
# If not AUTHORIZED, surface that and bail.
AUTH=$(aws bedrock get-foundation-model-availability \
  --model-id "$MODEL_ID" --region "$REGION" --profile "$PROFILE" \
  --query 'authorizationStatus' --output text)

if [[ "$AUTH" != "AUTHORIZED" ]]; then
  echo "authorizationStatus = $AUTH on $ACCOUNT — put the Anthropic use-case form first (see header)" >&2
  exit 1
fi

TOKEN=$(aws bedrock list-foundation-model-agreement-offers \
  --model-id "$MODEL_ID" --region "$REGION" --profile "$PROFILE" \
  --query 'offers[0].offerToken' --output text)

aws bedrock create-foundation-model-agreement \
  --model-id "$MODEL_ID" --offer-token "$TOKEN" \
  --region "$REGION" --profile "$PROFILE" >/dev/null

echo "agreement created on $ACCOUNT — waiting for it to register..."

# Phase 1: NOT_AVAILABLE → PENDING (creation registered with backend; usually instant)
until [[ "$(aws bedrock get-foundation-model-availability \
              --model-id "$MODEL_ID" --region "$REGION" --profile "$PROFILE" \
              --query 'agreementAvailability.status' --output text)" != "NOT_AVAILABLE" ]]; do
  sleep 5
done

echo "registered, polling for AVAILABLE..."

# Phase 2: PENDING → AVAILABLE (agreement finalized; can take several minutes)
until [[ "$(aws bedrock get-foundation-model-availability \
              --model-id "$MODEL_ID" --region "$REGION" --profile "$PROFILE" \
              --query 'agreementAvailability.status' --output text)" == "AVAILABLE" ]]; do
  sleep 15
done

echo "$MODEL_ID AVAILABLE on $ACCOUNT"
