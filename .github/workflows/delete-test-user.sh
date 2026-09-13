#!/usr/bin/env bash
# delete-test-user.sh — clean up cognito users left behind by smoke tests.
#
# Usage:
#   bash .github/workflows/delete-test-user.sh <email>
#   bash .github/workflows/delete-test-user.sh --all-test
#
# --all-test deletes any user whose username matches common test patterns
# (smoke-*, pass*-test-*, *@example.invalid). Real test users (real emails)
# are left alone.
#
# Override defaults via env: USER_POOL_ID, OPERATOR_ACCOUNT_ID

set -euo pipefail

OPERATOR_ACCOUNT_ID="${OPERATOR_ACCOUNT_ID:-185369506315}"
USER_POOL_ID="${USER_POOL_ID:-us-east-1_1803uNTZU}"

ARG="${1:?usage: bash delete-test-user.sh <email>  |  --all-test}"

echo "==> assuming OrganizationAccountAccessRole into operator"
creds=$(aws sts assume-role --no-cli-pager \
  --role-arn "arn:aws:iam::${OPERATOR_ACCOUNT_ID}:role/OrganizationAccountAccessRole" \
  --role-session-name "delete-test-user-${RANDOM}")
export AWS_ACCESS_KEY_ID=$(echo "$creds" | jq -r .Credentials.AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | jq -r .Credentials.SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$creds" | jq -r .Credentials.SessionToken)

if [ "$ARG" = "--all-test" ]; then
  echo "==> deleting all test-pattern users"
  mapfile -t victims < <(aws cognito-idp list-users --no-cli-pager \
    --user-pool-id "$USER_POOL_ID" \
    --query 'Users[?Username && (contains(Username, `@example.invalid`) || starts_with(Username, `smoke-`) || starts_with(Username, `pass`))].Username' \
    --output text | tr '\t' '\n' | sed '/^$/d')
  if [ "${#victims[@]}" -eq 0 ]; then
    echo "    no test-pattern users found"
  else
    for u in "${victims[@]}"; do
      echo "    deleting: $u"
      aws cognito-idp admin-delete-user --no-cli-pager \
        --user-pool-id "$USER_POOL_ID" --username "$u"
    done
  fi
else
  echo "==> deleting: $ARG"
  aws cognito-idp admin-delete-user --no-cli-pager \
    --user-pool-id "$USER_POOL_ID" --username "$ARG"
fi

count=$(aws cognito-idp list-users --no-cli-pager \
  --user-pool-id "$USER_POOL_ID" --query 'length(Users)' --output text)
echo "==> remaining users: $count"
