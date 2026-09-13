# oob — inventory's public read surface, per the oob source-catalog design (see modules/server/AGENTS.md).
# Two pieces the module owns:
#   1. register the descriptor — a GERP#oob_catalog#inventory row in the settings table, which server's
#      GET /oob discovery endpoint scans (terraform owns this static row; runtime owns the flag → no drift).
#   2. serve the read — GET /oob/inventory, an in-account reader over this gerp's OWN items table (no
#      cross-account assume-role). Gated on GERP#openly_operated.
# The reader reuses aws_iam_role.lambda: it already grants Scan on the items table, and main.tf adds the
# settings GetItem (the flag) for it. = MCP tools/call for the `inventory` source.

resource "aws_dynamodb_table_item" "oob_catalog_inventory" {
  table_name = var.settings_table_name
  hash_key   = "gerp_id"
  range_key  = "sk"

  item = jsonencode({
    gerp_id = { S = var.gerp_id }
    sk      = { S = "GERP#oob_catalog#inventory" }
    kind    = { S = "ddb_catalog" }
    label   = { S = "inventory & supply" }
    path    = { S = "/oob/inventory" }
  })
}

module "oob_inventory" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-oob_inventory"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/inventory/lambdas/oob_inventory.zip"
  src_dir         = "modules/inventory/lambdas/oob_inventory"
  gerp_id         = var.gerp_id
  timeout         = 15
  env_vars = {
    ITEMS_TABLE    = aws_dynamodb_table.items.name
    SETTINGS_TABLE = var.settings_table_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.oob_inventory
  to   = module.oob_inventory.aws_lambda_function.this
}

# ─── route (public GET /oob/inventory) ───

resource "aws_apigatewayv2_integration" "oob_inventory" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.oob_inventory.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "oob_inventory" {
  api_id    = var.server_api_id
  route_key = "GET /oob/inventory"
  target    = "integrations/${aws_apigatewayv2_integration.oob_inventory.id}"
}

resource "aws_lambda_permission" "apigw_oob_inventory" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeOobInventory"
  action              = "lambda:InvokeFunction"
  function_name       = module.oob_inventory.name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

