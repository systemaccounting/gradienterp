###############################################
# plaid_webhook — the operator webhook shim (see modules/accounting/AGENTS.md § bank-feed reconciliation).
#
# Public Lambda Function URL where Plaid delivers webhooks. Plaid signs them with an ES256 JWT, which
# Python can't verify, so this is a thin Node lambda (built-in `crypto`, zero bundled deps — @aws-sdk
# is runtime-provided). It never holds the Plaid secret: it asks the Python gateway for the JWK
# (verification_key op) and, on a verified SYNC_UPDATES_AVAILABLE, tells the gateway to route
# (webhook_route → item_id → gerp → cross-account invoke reconcile).
#
# No cycle: the shim names the gateway by CONSTRUCTED string (no resource ref); only the gateway's
# env references this shim's URL. One-directional.
###############################################

# ─── item → gerp map (recorded by the gateway on connect, read on webhook) ───

resource "aws_dynamodb_table" "plaid_items" {
  name         = "${local.stack_prefix}-plaid-items"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "item_id"

  attribute {
    name = "item_id"
    type = "S"
  }
}

# ─── role ───

resource "aws_iam_role" "plaid_webhook" {
  name = "${local.stack_prefix}-plaid-webhook"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "plaid_webhook" {
  name = "${local.stack_prefix}-plaid-webhook"
  role = aws_iam_role.plaid_webhook.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the shim's only job beyond crypto: ask the gateway for the JWK, then hand it the item to route
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = module.plaid_gateway.arn
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

# ─── lambda (Node — ES256 verification) ───

data "archive_file" "plaid_webhook" {
  type        = "zip"
  output_path = "${path.module}/.build/plaid_webhook.zip"
  source {
    content  = file("${path.module}/lambdas/plaid_webhook/index.mjs")
    filename = "index.mjs"
  }
}

module "plaid_webhook" {
  source = "../../../modules/terraform/lambda"

  name             = "${local.stack_prefix}-plaid-webhook"
  role             = aws_iam_role.plaid_webhook.arn
  filename         = data.archive_file.plaid_webhook.output_path
  source_code_hash = data.archive_file.plaid_webhook.output_base64sha256
  src_dir          = "prod/platform/operator/lambdas/plaid_webhook"
  handler          = "index.handler"
  runtime          = "nodejs22.x"
  timeout          = 15
  env_vars = {
    # constructed name (not a resource ref) so the shim doesn't depend on the gateway resource — breaks
    # the cycle with the gateway's PLAID_WEBHOOK_URL env.
    PLAID_GATEWAY_FN = "${local.stack_prefix}-plaid-gateway"
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.plaid_webhook
  to   = module.plaid_webhook.aws_lambda_function.this
}


# public endpoint — Plaid's signed JWT is the authentication (verified in-code), so auth NONE.
resource "aws_lambda_function_url" "plaid_webhook" {
  function_name      = module.plaid_webhook.name
  authorization_type = "NONE"
}

output "plaid_webhook_url" {
  value = aws_lambda_function_url.plaid_webhook.function_url
}
