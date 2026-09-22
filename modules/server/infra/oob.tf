# oob discovery — the single GET /oob endpoint returning this gerp's published oob sources.
#
# The catalog "object" is a ddb collection, not a tf local: each oob-exposing module's oob.tf writes its
# own GERP#oob_catalog#<key> row into the settings table (same move as register_with_agent attaching a
# target to the shared gateway — self-registration, no central aggregation). This lambda scans those rows
# and serves them, gated on GERP#openly_operated. = MCP tools/list; each source's path (GET /oob/<key>) is
# the read (tools/call), owned by the module that registered it.
#
# The settings table name/arn are CONSTRUCTED here, not sourced from module.settings: settings depends on
# this module's api_id, so consuming its output would cycle the graph. The name is deterministic (the same
# prefix formula settings/infra uses), so constructing it keeps the graph acyclic + standalone-applyable.

locals {
  oob_settings_table_name = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
  oob_settings_table_arn  = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.oob_settings_table_name}"
}

resource "aws_iam_role" "oob_discovery" {
  name = "${local.prefix}-oob-discovery"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "oob_discovery" {
  name = "${local.prefix}-oob-discovery"
  role = aws_iam_role.oob_discovery.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the flag (GetItem GERP#openly_operated) + the catalog rows (Query begins_with GERP#oob_catalog#)
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = local.oob_settings_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "oob_discovery" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-oob_discovery"
  role            = aws_iam_role.oob_discovery.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/server/lambdas/oob_discovery.zip"
  src_dir         = "modules/server/lambdas/oob_discovery"
  gerp_id         = var.gerp_id
  timeout         = 10
  env_vars = {
    SETTINGS_TABLE = local.oob_settings_table_name
    GERP_ID        = var.gerp_id
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.oob_discovery
  to   = module.oob_discovery.aws_lambda_function.this
}


# ─── route (public GET /oob) ───

resource "aws_apigatewayv2_integration" "oob_discovery" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.oob_discovery.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "oob" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /oob"
  target    = "integrations/${aws_apigatewayv2_integration.oob_discovery.id}"
}

resource "aws_lambda_permission" "apigw_oob_discovery" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeOobDiscovery"
  action              = "lambda:InvokeFunction"
  function_name       = module.oob_discovery.name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.
