#!/usr/bin/env bash
# Apply the per-customer module chain into a customer's AWS sub-account.
# Order: agent (creates gateway, writes SSM exports) → accounting → inventory.
# Domain modules read the gateway via SSM, so agent goes first.
#
# Uses `terraform workspace` per customer so each customer's state stays
# isolated under the same module dirs.
#
# Until prod/per_customer/ + tower lands, this script is the bring-up path for
# any new openly-operated (or private) customer.
#
# Usage:
#   bash scripts/customer_apply.sh <customer-id> <aws-profile> [image-tag]
#
# Example:
#   bash scripts/customer_apply.sh gradienterp customer-gradienterp v1
#
# Prereqs (one-time per customer account):
#   1. AWS profile configured for the customer account (aws configure sso)
#   2. Bedrock model agreement: bash scripts/bedrock-subscribe-model.sh anthropic.claude-sonnet-4-6 <profile>
#   3. SSM tenant metadata seeded at /gradienterp/customers/<customer-id>
#
# Env:
#   REGION=us-east-1 (default)

set -euo pipefail

CUSTOMER_ID="${1:?usage: bash scripts/customer_apply.sh <customer-id> <aws-profile> [image-tag]}"
PROFILE="${2:?usage: bash scripts/customer_apply.sh <customer-id> <aws-profile> [image-tag]}"
TAG="${3:-v1}"

export AWS_PROFILE="$PROFILE"
REGION="${REGION:-us-east-1}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text --no-cli-pager)
echo "==> applying customer '$CUSTOMER_ID' into account $ACCOUNT (profile=$PROFILE, tag=$TAG)"

select_or_create_workspace() {
  terraform workspace select "$CUSTOMER_ID" 2>/dev/null || terraform workspace new "$CUSTOMER_ID"
}

###############################################
# 1. agent ECR — needed before image push
###############################################
echo "==> [1/5] modules/agent — ECR repo (targeted)"
cd "$REPO_ROOT/modules/agent/infra"
terraform init -input=false >/dev/null
select_or_create_workspace
terraform apply -auto-approve -target=aws_ecr_repository.agent \
  -var "customer_id=$CUSTOMER_ID" \
  -var "agent_image_tag=$TAG"

ECR_URL=$(terraform output -raw ecr_repository_url)
echo "    ecr: $ECR_URL"

###############################################
# 2. build + push container image
###############################################
echo "==> [2/5] container build + push"
cd "$REPO_ROOT"
bash scripts/docker.sh --build
bash scripts/docker.sh --push "$ECR_URL:$TAG"

###############################################
# 3. agent full apply — creates gateway + writes SSM exports.
#     Domain modules read these in steps 4-5.
###############################################
echo "==> [3/5] modules/agent — full apply"
cd "$REPO_ROOT/modules/agent/infra"
terraform apply -auto-approve \
  -var "customer_id=$CUSTOMER_ID" \
  -var "agent_image_tag=$TAG"

RUNTIME_ARN=$(terraform output -raw agent_arn)

###############################################
# 4. accounting — reads gateway SSM, registers tool targets, outputs lambda ARNs
###############################################
echo "==> [4/5] modules/accounting"
cd "$REPO_ROOT/modules/accounting/infra"
terraform init -input=false >/dev/null
select_or_create_workspace
terraform apply -auto-approve -var "customer_id=$CUSTOMER_ID"

PJE_ARN=$(terraform output -json lambda_arns | jq -r '.post_journal_entry')
PJE_NAME=$(terraform output -json lambda_functions | jq -r '.post_journal_entry')
echo "    post_journal_entry: $PJE_NAME"

###############################################
# 5. inventory — reads gateway SSM + accounting's post_journal_entry
###############################################
echo "==> [5/5] modules/inventory"
cd "$REPO_ROOT/modules/inventory/infra"
terraform init -input=false >/dev/null
select_or_create_workspace
terraform apply -auto-approve \
  -var "customer_id=$CUSTOMER_ID" \
  -var "post_journal_entry_fn_arn=$PJE_ARN" \
  -var "post_journal_entry_fn_name=$PJE_NAME"

###############################################
# smoke invoke
###############################################
echo "==> smoke invoke"
SID="$(uuidgen | tr -d '-')$(uuidgen | tr -d '-' | head -c 1)"  # 33+ chars
PAYLOAD=$(printf '%s' '{"prompt":"reply with just OK"}' | base64)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$RUNTIME_ARN" \
  --runtime-session-id "$SID" \
  --payload "$PAYLOAD" \
  --content-type application/json \
  --region "$REGION" \
  --profile "$PROFILE" \
  --no-cli-pager \
  "/tmp/customer_apply_${CUSTOMER_ID}.json"

echo "==> response:"
cat "/tmp/customer_apply_${CUSTOMER_ID}.json"
echo
echo "==> done. runtime arn: $RUNTIME_ARN"
