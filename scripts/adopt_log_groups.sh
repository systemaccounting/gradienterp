#!/usr/bin/env bash
# Adopt a live stack's lambda log groups into its state, once, before the apply that moves the
# stack onto modules/terraform/lambda. The groups exist already — Lambda made them on each
# function's first invoke, owned by nobody — and a create on the same name fails; an import
# hands them to the module, and the apply sets the retention.
#
#   bash scripts/adopt_log_groups.sh prod/per_customer customer-westwood-via-org operator-org westwood-c40fd8 222165865776
#   bash scripts/adopt_log_groups.sh prod/init_customer customer-westwood-via-org operator-org westwood-c40fd8 222165865776
#   bash scripts/adopt_log_groups.sh prod/tower operator-org default
#
# Args: <stack dir> <profile the log groups are read with> [<terraform profile> <gerp_id> <aws_account_id>]
# For prod/per_customer and prod/init_customer the last three name the gerp: the backend key and
# the tf vars. One gerp at a time in a template dir: the init points the dir's backend at that
# gerp's state, and a second run in the same dir re-points it under the first.
# Reads the plan for every `module.<x>.aws_cloudwatch_log_group.this` it would create, and
# imports the ones that exist. Idempotent: a group already in state is skipped by the plan.
set -euo pipefail
cd "$(dirname "$0")/.."
STACK="$1"; LOGS_PROFILE="$2"; TF_PROFILE="${3:-$2}"; GERP="${4:-}"; ACCT="${5:-}"

TFVARS=()
if [ "$STACK" = "prod/per_customer" ] || [ "$STACK" = "prod/init_customer" ]; then
  [ -n "$GERP" ] && [ -n "$ACCT" ] || { echo "${STACK} needs <gerp_id> <aws_account_id>"; exit 1; }
  KEY="${GERP}/terraform.tfstate"; [ "$STACK" = "prod/init_customer" ] && KEY="${GERP}/init.tfstate"
  (cd "$STACK" && AWS_PROFILE="$TF_PROFILE" terraform init -input=false -reconfigure -no-color -backend-config="key=${KEY}" >/dev/null)
  TFVARS=(-var "gerp_id=${GERP}" -var "aws_account_id=${ACCT}")
  [ "$STACK" = "prod/per_customer" ] && TFVARS+=(-var "sender_email=ops+sender@gradienterp.cloud" -var "chat_base_url=https://gradienterp.cloud/chat")
fi

echo "==> planning ${STACK} for log groups to adopt"
PLAN=$(mktemp)
(cd "$STACK" && AWS_PROFILE="$TF_PROFILE" terraform plan -input=false -no-color "${TFVARS[@]}" >"$PLAN" 2>&1 || true)
ADDRS=$(grep -E '^  # .*aws_cloudwatch_log_group\.this will be created' "$PLAN" | sed -E 's/^  # (.*) will be created/\1/' || true)
[ -n "$ADDRS" ] || { echo "nothing to adopt"; rm -f "$PLAN"; exit 0; }

EXISTING=$(aws logs describe-log-groups --profile "$LOGS_PROFILE" --no-cli-pager --query 'logGroups[].logGroupName' --output text | tr '\t' '\n')
n=0
while read -r addr; do
  [ -n "$addr" ] || continue
  # the group's name is the module's `name` var: read it off the plan block
  name=$(awk -v a="# ${addr} will be created" 'index($0, a) {f=1} f && /name *= "\/aws\/lambda\// {sub(/.*name *= "/, ""); sub(/".*/, ""); print; exit}' "$PLAN")
  [ -n "$name" ] || { echo "    ?  ${addr}: no name in the plan"; continue; }
  if echo "$EXISTING" | grep -qx "$name"; then
    echo "    importing ${addr} <- ${name}"
    (cd "$STACK" && AWS_PROFILE="$TF_PROFILE" terraform import -input=false -no-color "${TFVARS[@]}" "$addr" "$name" >/dev/null)
    n=$((n+1))
  else
    echo "    new  ${name} (the apply creates it)"
  fi
done <<< "$ADDRS"
rm -f "$PLAN"
echo "==> adopted ${n} log group(s); apply to set their retention"
