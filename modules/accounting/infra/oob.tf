# oob — accounting's public read surface, per the oob source-catalog design (prod/openlyoperated_biz/
# TODO.md). Two pieces the module owns:
#
#   1. register the descriptor — a GERP#oob_catalog#financials row in the settings table, which the
#      server module's GET /oob discovery endpoint scans (the "extend the object" step). terraform owns
#      this static row; runtime owns the GERP#openly_operated flag → no drift, no fighting.
#   2. serve the read — GET /oob/financials, an in-account reader over this gerp's OWN ledger (no
#      cross-account assume-role — the business serves its own book). Gated on GERP#openly_operated.
#
# The reader reuses aws_iam_role.lambda: it already grants Query on the ledger + GetItem on the settings
# table (the flag), so no new IAM. = MCP tools/call for the `financials` source.

resource "aws_dynamodb_table_item" "oob_catalog_financials" {
  table_name = var.settings_table_name
  hash_key   = "gerp_id"
  range_key  = "sk"

  item = jsonencode({
    gerp_id = { S = var.gerp_id }
    sk      = { S = "GERP#oob_catalog#financials" }
    kind    = { S = "ledger" }
    label   = { S = "financials" }
    path    = { S = "/oob/financials" }
  })
}

module "oob_financials" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-oob_financials"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/accounting/lambdas/oob_financials.zip"
  src_dir         = "modules/accounting/lambdas/oob_financials"
  gerp_id         = var.gerp_id
  timeout         = 15
  env_vars = {
    LEDGER_TABLE   = aws_dynamodb_table.ledger.name
    SETTINGS_TABLE = var.settings_table_name
    BALANCES_TABLE = aws_dynamodb_table.balances.name # the standing REVENUE_PENDING balance
    GERP_ID        = var.gerp_id
    WINDOW_DAYS    = "90"
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.oob_financials
  to   = module.oob_financials.aws_lambda_function.this
}


# ─── route (public GET /oob/financials) ───

resource "aws_apigatewayv2_integration" "oob_financials" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.oob_financials.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "oob_financials" {
  api_id    = var.server_api_id
  route_key = "GET /oob/financials"
  target    = "integrations/${aws_apigatewayv2_integration.oob_financials.id}"
}

resource "aws_lambda_permission" "apigw_oob_financials" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeOobFinancials"
  action              = "lambda:InvokeFunction"
  function_name       = module.oob_financials.name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

# ─── metrics — the statement folded into four metrics, one shape with the economy's counters ───
# (GET /oob/metrics; catalog kind `metrics`). Same role, same gate, same folds as financials.

resource "aws_dynamodb_table_item" "oob_catalog_metrics" {
  table_name = var.settings_table_name
  hash_key   = "gerp_id"
  range_key  = "sk"

  item = jsonencode({
    gerp_id = { S = var.gerp_id }
    sk      = { S = "GERP#oob_catalog#metrics" }
    kind    = { S = "metrics" }
    label   = { S = "metrics" }
    path    = { S = "/oob/metrics" }
  })
}

module "oob_metrics" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-oob_metrics"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/accounting/lambdas/oob_metrics.zip"
  src_dir         = "modules/accounting/lambdas/oob_metrics"
  gerp_id         = var.gerp_id
  timeout         = 15
  env_vars = {
    LEDGER_TABLE   = aws_dynamodb_table.ledger.name
    SETTINGS_TABLE = var.settings_table_name
    GERP_ID        = var.gerp_id
    WINDOW_DAYS    = "90"
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.oob_metrics
  to   = module.oob_metrics.aws_lambda_function.this
}


resource "aws_apigatewayv2_integration" "oob_metrics" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.oob_metrics.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "oob_metrics" {
  api_id    = var.server_api_id
  route_key = "GET /oob/metrics"
  target    = "integrations/${aws_apigatewayv2_integration.oob_metrics.id}"
}

resource "aws_lambda_permission" "apigw_oob_metrics" {
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeOobMetrics"
  action              = "lambda:InvokeFunction"
  function_name       = module.oob_metrics.name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}
