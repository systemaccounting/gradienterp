#!/usr/bin/env bash
# per-customer-apply.sh — provision a customer end-to-end.
#
# Bash equivalent of tower's provision_customer lambda. Useful for debug
# (skip the cognito signup path) and for ad-hoc onboarding from a workstation.
#
# Usage:
#   bash .github/workflows/per-customer-apply.sh <customer-id> <owner-email> [<business-name>]
#
# Example:
#   bash .github/workflows/per-customer-apply.sh ken-cafe ken@example.com "Ken's Cafe"
#
# Requires:
#   - aws cli (default credentials with sts:AssumeRole into operator)
#   - jq, terraform >= 1.6
#
# Override defaults via env:
#   MANAGEMENT_ACCOUNT_ID, OPERATOR_ACCOUNT_ID, AF_PRODUCT_ID, AF_PATH_ID,
#   CUSTOMERS_OU_MANAGED_NAME, SENDER_EMAIL, CHAT_BASE_URL, BUSINESS_CATEGORY,
#   OPENLY_OPERATED, REPORTING_SCHEDULE, TENANT_AWS_EMAIL, OWNER_FIRST_NAME,
#   OWNER_LAST_NAME

set -euo pipefail

CUSTOMER_ID="${1:?usage: bash per-customer-apply.sh <customer-id> <owner-email> [<business-name>]}"
OWNER_EMAIL="${2:?usage: bash per-customer-apply.sh <customer-id> <owner-email> [<business-name>]}"
BUSINESS_NAME="${3:-$CUSTOMER_ID}"

MANAGEMENT_ACCOUNT_ID="${MANAGEMENT_ACCOUNT_ID:-335667362239}"
OPERATOR_ACCOUNT_ID="${OPERATOR_ACCOUNT_ID:-185369506315}"
AF_PRODUCT_ID="${AF_PRODUCT_ID:-prod-zgoj6xklupj4s}"
AF_PATH_ID="${AF_PATH_ID:-lpv3-63cyoeboo5tey}"
CUSTOMERS_OU_MANAGED_NAME="${CUSTOMERS_OU_MANAGED_NAME:-customers (ou-8290-vdmjfh4n)}"
SENDER_EMAIL="${SENDER_EMAIL:-ops+sender@gradienterp.cloud}"
CHAT_BASE_URL="${CHAT_BASE_URL:-https://gradienterp.cloud/chat}"
BUSINESS_CATEGORY="${BUSINESS_CATEGORY:-test}"
OPENLY_OPERATED="${OPENLY_OPERATED:-true}"
REPORTING_SCHEDULE="${REPORTING_SCHEDULE:-cron(0 9 1 * ? *)}"
TENANT_AWS_EMAIL="${TENANT_AWS_EMAIL:-ops+${CUSTOMER_ID}@gradienterp.cloud}"
OWNER_FIRST_NAME="${OWNER_FIRST_NAME:-$CUSTOMER_ID}"
OWNER_LAST_NAME="${OWNER_LAST_NAME:-Customer}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOWER_PROVISIONING_ROLE="arn:aws:iam::${MANAGEMENT_ACCOUNT_ID}:role/TowerProvisioning"

# ---------- step 1: assume into operator → assume TowerProvisioning ----------
echo "==> [1/5] assuming TowerProvisioning role (via operator)"
op_creds=$(aws sts assume-role --no-cli-pager \
  --role-arn "arn:aws:iam::${OPERATOR_ACCOUNT_ID}:role/OrganizationAccountAccessRole" \
  --role-session-name "per-customer-apply-op-${RANDOM}")
OP_AKID=$(echo "$op_creds" | jq -r .Credentials.AccessKeyId)
OP_SKEY=$(echo "$op_creds" | jq -r .Credentials.SecretAccessKey)
OP_TOKEN=$(echo "$op_creds" | jq -r .Credentials.SessionToken)

tp_creds=$(AWS_ACCESS_KEY_ID=$OP_AKID AWS_SECRET_ACCESS_KEY=$OP_SKEY AWS_SESSION_TOKEN=$OP_TOKEN \
  aws sts assume-role --no-cli-pager \
  --role-arn "$TOWER_PROVISIONING_ROLE" \
  --role-session-name "per-customer-apply-tp-${RANDOM}")
TP_AKID=$(echo "$tp_creds" | jq -r .Credentials.AccessKeyId)
TP_SKEY=$(echo "$tp_creds" | jq -r .Credentials.SecretAccessKey)
TP_TOKEN=$(echo "$tp_creds" | jq -r .Credentials.SessionToken)

# ---------- step 2: SC ProvisionProduct against CT's AF product ----------
echo "==> [2/5] servicecatalog:ProvisionProduct '$BUSINESS_NAME' ($TENANT_AWS_EMAIL)"
artifact_id=$(AWS_ACCESS_KEY_ID=$TP_AKID AWS_SECRET_ACCESS_KEY=$TP_SKEY AWS_SESSION_TOKEN=$TP_TOKEN \
  aws servicecatalog list-provisioning-artifacts --no-cli-pager \
  --product-id "$AF_PRODUCT_ID" \
  --query 'ProvisioningArtifactDetails[?Active].Id | [0]' --output text)
echo "    active artifact: $artifact_id"

pp_name="customer-${CUSTOMER_ID}-$(date +%s)"
record_id=$(AWS_ACCESS_KEY_ID=$TP_AKID AWS_SECRET_ACCESS_KEY=$TP_SKEY AWS_SESSION_TOKEN=$TP_TOKEN \
  aws servicecatalog provision-product --no-cli-pager \
  --product-id "$AF_PRODUCT_ID" \
  --provisioning-artifact-id "$artifact_id" \
  --path-id "$AF_PATH_ID" \
  --provisioned-product-name "$pp_name" \
  --provisioning-parameters \
    "Key=AccountName,Value=$BUSINESS_NAME" \
    "Key=AccountEmail,Value=$TENANT_AWS_EMAIL" \
    "Key=SSOUserEmail,Value=$OWNER_EMAIL" \
    "Key=SSOUserFirstName,Value=$OWNER_FIRST_NAME" \
    "Key=SSOUserLastName,Value=$OWNER_LAST_NAME" \
    "Key=ManagedOrganizationalUnit,Value=$CUSTOMERS_OU_MANAGED_NAME" \
  --query 'RecordDetail.RecordId' --output text)
echo "    record: $record_id (polling for account)"

# ---------- step 3: poll for the new account by name ----------
# SC AF often returns describe-record FAILED while CT internally completes;
# truth is whether an ACTIVE account named $BUSINESS_NAME shows up.
account_id=""
deadline=$(( $(date +%s) + 900 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  account_id=$(AWS_ACCESS_KEY_ID=$TP_AKID AWS_SECRET_ACCESS_KEY=$TP_SKEY AWS_SESSION_TOKEN=$TP_TOKEN \
    aws organizations list-accounts --no-cli-pager \
    --query "Accounts[?Name=='$BUSINESS_NAME' && Status=='ACTIVE'].Id | [0]" --output text 2>/dev/null)
  if [ -n "$account_id" ] && [ "$account_id" != "None" ]; then
    echo "    account: $account_id"
    break
  fi
  rec_status=$(AWS_ACCESS_KEY_ID=$TP_AKID AWS_SECRET_ACCESS_KEY=$TP_SKEY AWS_SESSION_TOKEN=$TP_TOKEN \
    aws servicecatalog describe-record --no-cli-pager \
    --id "$record_id" \
    --query 'RecordDetail.Status' --output text 2>/dev/null)
  echo "    sc_status: $rec_status, account not yet visible..."
  sleep 15
done
if [ -z "$account_id" ] || [ "$account_id" = "None" ]; then
  echo "==> ProvisionProduct timed out after 15min (no ACTIVE account named '$BUSINESS_NAME')"
  exit 1
fi

# ---------- step 4: seed SSM via OperatorOrchestration on new account ----------
echo "==> [3/5] seed SSM /gradienterp/customers/${CUSTOMER_ID}"
new_role_arn="arn:aws:iam::${account_id}:role/OperatorOrchestration"
new_creds=$(AWS_ACCESS_KEY_ID=$OP_AKID AWS_SECRET_ACCESS_KEY=$OP_SKEY AWS_SESSION_TOKEN=$OP_TOKEN \
  aws sts assume-role --no-cli-pager \
  --role-arn "$new_role_arn" \
  --role-session-name "per-customer-apply-new-${RANDOM}")
NEW_AKID=$(echo "$new_creds" | jq -r .Credentials.AccessKeyId)
NEW_SKEY=$(echo "$new_creds" | jq -r .Credentials.SecretAccessKey)
NEW_TOKEN=$(echo "$new_creds" | jq -r .Credentials.SessionToken)

tenant_blob=$(jq -nc \
  --arg bn "$BUSINESS_NAME" \
  --arg bc "$BUSINESS_CATEGORY" \
  --arg oe "$OWNER_EMAIL" \
  --arg rs "$REPORTING_SCHEDULE" \
  --argjson oo "$OPENLY_OPERATED" \
  '{business_name:$bn, business_category:$bc, owner_email:$oe, reporting_schedule:$rs, openly_operated:$oo}')

AWS_ACCESS_KEY_ID=$NEW_AKID AWS_SECRET_ACCESS_KEY=$NEW_SKEY AWS_SESSION_TOKEN=$NEW_TOKEN \
  aws ssm put-parameter --no-cli-pager \
  --name "/gradienterp/customers/${CUSTOMER_ID}" \
  --type String \
  --value "$tenant_blob" \
  --overwrite >/dev/null

# ---------- step 5: terraform apply prod/per_customer/ ----------
echo "==> [4/5] terraform apply prod/per_customer/ (key=${CUSTOMER_ID}/terraform.tfstate)"
cd "$REPO_ROOT/prod/per_customer"

# per_customer/versions.tf assumes OperatorOrchestration into the customer account.
# Caller must run as an operator-account principal. Re-export operator creds.
export AWS_ACCESS_KEY_ID=$OP_AKID
export AWS_SECRET_ACCESS_KEY=$OP_SKEY
export AWS_SESSION_TOKEN=$OP_TOKEN

terraform init \
  -backend-config="key=${CUSTOMER_ID}/terraform.tfstate" \
  -reconfigure >/dev/null

terraform apply -auto-approve \
  -var "gerp_id=${CUSTOMER_ID}" \
  -var "aws_account_id=${account_id}" \
  -var "sender_email=${SENDER_EMAIL}" \
  -var "chat_base_url=${CHAT_BASE_URL}"

echo
echo "==> success: customer '${CUSTOMER_ID}' provisioned in account ${account_id}"
echo "    state: s3://gradienterp-tfstate-${OPERATOR_ACCOUNT_ID}/${CUSTOMER_ID}/terraform.tfstate"
