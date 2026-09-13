#!/usr/bin/env bash
# Submit the Anthropic use-case form ORG-WIDE via API.
#
# AWS Bedrock requires Anthropic's first-time-use (FTU) form before any
# account can invoke Claude. Submitted via the Bedrock console, it covers
# only ONE account. Submitted via the PutUseCaseForModelAccess API from
# the AWS Organizations MANAGEMENT account, it cascades to every member
# account (current + future). API-only — console submission does not cascade.
#
# Run this ONCE per org. Subsequent customer sub-accounts vended via
# Control Tower / Account Factory inherit the FTU approval automatically.
#
# Usage:
#   bash scripts/bedrock-authorize-anthropic-org.sh
#
# Env (override defaults if your business has changed):
#   COMPANY_NAME     (default: "gradientERP")
#   COMPANY_WEBSITE  (default: "https://gradienterp.cloud")
#   INTENDED_USERS   (default: "Internal employees and customers of an AI-first ERP platform")
#   INDUSTRY_OPTION  (default: "Software & Internet")
#   USE_CASES        (default below — describes the platform's agent use case)
#   AWS_PROFILE      (default: not set; must be authenticated as org management account)
#   REGION           (default: us-east-1)

set -euo pipefail

# INTENDED_USERS is a code: "0"=Internal, "1"=External, "2"=Internal_and_External.
# Per-tenant agents operate on behalf of external business customers; the operator
# (us) is an internal user. So 2 = both.
_DEFAULT_INTENDED_USERS="2"
_DEFAULT_USE_CASES="Per-tenant business operations agent. The agent runs on AWS Bedrock AgentCore Runtime, invokes Claude models for conversational interactions with business owners over chat, SMS, and email, and orchestrates serverless ERP modules: accounting, inventory, contacts, calendar. Use cases include classifying transactions, generating financial statements, scheduling reports, coordinating purchasing, and answering owner questions about their books. No model output is presented to consumers without business-owner review."

COMPANY_NAME="${COMPANY_NAME:-gradientERP}"
COMPANY_WEBSITE="${COMPANY_WEBSITE:-https://gradienterp.cloud}"
INTENDED_USERS="${INTENDED_USERS:-$_DEFAULT_INTENDED_USERS}"
INDUSTRY_OPTION="${INDUSTRY_OPTION:-Software & Internet}"
USE_CASES="${USE_CASES:-$_DEFAULT_USE_CASES}"
REGION="${REGION:-us-east-1}"

# Verify caller is in the org management account.
ACCOUNT=$(aws sts get-caller-identity --no-cli-pager --query Account --output text)
MGMT=$(aws organizations describe-organization --no-cli-pager --query 'Organization.MasterAccountId' --output text 2>/dev/null || echo "ERROR")
if [[ "$MGMT" == "ERROR" ]]; then
    echo "couldn't describe-organization — are you authenticated against an org member account?" >&2
    exit 1
fi
if [[ "$ACCOUNT" != "$MGMT" ]]; then
    echo "must run as the org management account (got $ACCOUNT, expected $MGMT)" >&2
    echo "the form's cascade-to-members only works from mgmt; running from a member submits per-account only" >&2
    exit 1
fi

# Build the form payload as documented in the PutUseCaseForModelAccess API ref.
FORM=$(jq -nc \
    --arg cn "$COMPANY_NAME" \
    --arg cw "$COMPANY_WEBSITE" \
    --arg iu "$INTENDED_USERS" \
    --arg io "$INDUSTRY_OPTION" \
    --arg uc "$USE_CASES" \
    '{companyName: $cn, companyWebsite: $cw, intendedUsers: $iu, industryOption: $io, otherIndustryOption: "", useCases: $uc}')

echo "==> submitting use-case form for org $MGMT (region $REGION)"
echo "    payload: $FORM"

# fileb:// would base64-encode for us, but jq+printf is simpler.
ENCODED=$(printf '%s' "$FORM" | base64)
aws bedrock put-use-case-for-model-access \
    --region "$REGION" \
    --form-data "$ENCODED" \
    --no-cli-pager

echo "==> submitted. cascade inherits to all member accounts in commercial regions."
echo "    opt-in regions (e.g. ap-east-1) require separate submission."
