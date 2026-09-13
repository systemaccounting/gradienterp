#!/usr/bin/env bash
# create-account.sh — headlessly create + confirm a cognito user via CLI auth.
#
# Smoke test for the operator-account cognito user pool. No browser, no docker
# — pure aws cli. Convertible to .github/workflows/*.yaml once the repo is
# published; each major step (assume-role, admin-create-user, initiate-auth,
# respond-to-auth-challenge, verify) maps cleanly to a YAML job step.
#
# Usage:
#   bash .github/workflows/create-account.sh <email> [<new_password>]
#
# Requires:
#   - aws cli (default credentials with sts:AssumeRole into operator account)
#   - jq, openssl
#
# Override defaults via env:
#   USER_POOL_ID, SMOKE_CLIENT_ID, OPERATOR_ACCOUNT_ID

set -euo pipefail

EMAIL="${1:?usage: bash create-account.sh <email> [<new_password>]}"
NEW_PASSWORD="${2:-NewPass-$(openssl rand -hex 8)!}"

OPERATOR_ACCOUNT_ID="${OPERATOR_ACCOUNT_ID:-185369506315}"
USER_POOL_ID="${USER_POOL_ID:-us-east-1_1803uNTZU}"
SMOKE_CLIENT_ID="${SMOKE_CLIENT_ID:-2logb8cvg84457a2frjvk44njs}"

TEMP_PASSWORD="TempPass-$(openssl rand -hex 8)!"

echo "==> assuming OrganizationAccountAccessRole in operator account"
creds=$(aws sts assume-role --no-cli-pager \
  --role-arn "arn:aws:iam::${OPERATOR_ACCOUNT_ID}:role/OrganizationAccountAccessRole" \
  --role-session-name "create-account-${RANDOM}")
export AWS_ACCESS_KEY_ID=$(echo "$creds" | jq -r .Credentials.AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | jq -r .Credentials.SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$creds" | jq -r .Credentials.SessionToken)

echo "==> admin-create-user $EMAIL (SUPPRESS — no real email sent)"
aws cognito-idp admin-create-user --no-cli-pager \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --user-attributes "Name=email,Value=$EMAIL" "Name=email_verified,Value=true" \
  --temporary-password "$TEMP_PASSWORD" \
  --message-action SUPPRESS >/dev/null

echo "==> initiate-auth USER_PASSWORD_AUTH (expect NEW_PASSWORD_REQUIRED challenge)"
auth=$(aws cognito-idp initiate-auth --no-cli-pager \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "$SMOKE_CLIENT_ID" \
  --auth-parameters "USERNAME=$EMAIL,PASSWORD=$TEMP_PASSWORD")

challenge=$(echo "$auth" | jq -r '.ChallengeName')
session=$(echo "$auth" | jq -r '.Session')

if [ "$challenge" != "NEW_PASSWORD_REQUIRED" ]; then
  echo "==> failure: expected NEW_PASSWORD_REQUIRED challenge, got $challenge"
  exit 1
fi

echo "==> respond-to-auth-challenge with new password"
aws cognito-idp respond-to-auth-challenge --no-cli-pager \
  --client-id "$SMOKE_CLIENT_ID" \
  --challenge-name NEW_PASSWORD_REQUIRED \
  --session "$session" \
  --challenge-responses "USERNAME=$EMAIL,NEW_PASSWORD=$NEW_PASSWORD" >/dev/null

echo "==> verifying user is CONFIRMED"
status=$(aws cognito-idp admin-get-user --no-cli-pager \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --query 'UserStatus' --output text)

echo "user status: $status"
if [ "$status" = "CONFIRMED" ]; then
  echo "==> success: $EMAIL created and confirmed (password: $NEW_PASSWORD)"
  exit 0
else
  echo "==> failure: expected CONFIRMED, got $status"
  exit 1
fi
