###############################################
# plaid_gateway — the operator-account Plaid I/O boundary (see modules/accounting/AGENTS.md § bank-feed reconciliation).
#
# Holds the shared Plaid client_id/secret (operator SSM) so the credential never sprawls into
# customer accounts, and makes the outbound Plaid calls. Per-gerp connect_bank/reconcile invoke
# this cross-account carrying only that gerp's access_token.
#
# Ops: create_link/complete (Hosted Link connect), exchange, pull (/transactions/sync), verification_key
# (JWK for the webhook shim to verify Plaid's ES256 signature), webhook_route (item_id → gerp →
# cross-account invoke reconcile). The public webhook Function URL lives in the Node shim
# (plaid_webhook.tf) — Python can't verify ES256. Inbound gerp-invoke is one org-scoped grant below.
###############################################

# ─── shared Plaid app creds (operator SSM; value set out-of-band, tf only seeds a placeholder) ───

resource "aws_ssm_parameter" "plaid_client_id" {
  name  = "/gradienterp/operator/plaid/client_id"
  type  = "String"
  value = "SET_ME"
  lifecycle { ignore_changes = [value] } # sandbox now / prod later — set via CLI, never in tf
}

resource "aws_ssm_parameter" "plaid_secret" {
  name  = "/gradienterp/operator/plaid/secret"
  type  = "SecureString"
  value = "SET_ME"
  lifecycle { ignore_changes = [value] }
}

# ─── role ───

resource "aws_iam_role" "plaid_gateway" {
  name = "${local.stack_prefix}-plaid-gateway"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "plaid_gateway" {
  name = "${local.stack_prefix}-plaid-gateway"
  role = aws_iam_role.plaid_gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the shared Plaid creds
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = [aws_ssm_parameter.plaid_client_id.arn, aws_ssm_parameter.plaid_secret.arn]
      },
      {
        # decrypt the SecureString (account-default aws/ssm key; gated by its key policy)
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
      },
      {
        # item→gerp map (recorded on connect, read on webhook) + customers registry (gerp→account)
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.plaid_items.arn, aws_dynamodb_table.customers.arn]
      },
      {
        # webhook_route pokes the resolved gerp's reconcile cross-account (constructed name; account
        # wildcarded — the operator is the caller into many gerp accounts).
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:*:function:${local.stack_prefix}-accounting-*-reconcile"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambda ───

data "archive_file" "plaid_gateway" {
  type        = "zip"
  output_path = "${path.module}/.build/plaid_gateway.zip"
  source {
    content  = file("${path.module}/lambdas/plaid_gateway/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/../../../modules/aws/aws.py")
    filename = "aws.py"
  }
}

module "plaid_gateway" {
  source = "../../../modules/terraform/lambda"

  name             = "${local.stack_prefix}-plaid-gateway"
  role             = aws_iam_role.plaid_gateway.arn
  filename         = data.archive_file.plaid_gateway.output_path
  source_code_hash = data.archive_file.plaid_gateway.output_base64sha256
  src_dir          = "prod/platform/operator/lambdas/plaid_gateway"
  timeout          = 30
  env_vars = {
    PLAID_ENV         = "production" # with the production secret in /gradienterp/operator/plaid/secret
    PLAID_SSM_PREFIX  = "/gradienterp/operator/plaid"
    STACK_PREFIX      = local.stack_prefix
    PLAID_ITEMS_TABLE = aws_dynamodb_table.plaid_items.name
    CUSTOMERS_TABLE   = aws_dynamodb_table.customers.name
    PLAID_WEBHOOK_URL = aws_lambda_function_url.plaid_webhook.function_url # create_link sets this so Plaid webhooks land at the shim
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.plaid_gateway
  to   = module.plaid_gateway.aws_lambda_function.this
}


# ─── who may invoke the gateway: any principal in an admitted org (one grant per org, all gerps) ───
#
# The per-gerp accounting reconcile/connect_bank lambdas invoke this cross-account. Rather than
# per-tenant grants, one org-scoped resource policy covers every current + future gerp — the mirror
# of the shared bus policy (aws:PrincipalOrgID). Caller side is a single concrete ARN (this fn), so
# the gerp's identity policy names it directly; no account wildcard anywhere.
# aws_lambda_permission takes one org id per statement, so local.org_ids becomes one statement each.

resource "aws_lambda_permission" "org_invoke" {
  for_each = toset(local.org_ids)
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowOrgGerpInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.plaid_gateway.name
  principal           = "*"
  principal_org_id    = each.value
}

output "plaid_gateway_fn_arn" {
  value = module.plaid_gateway.arn
}
