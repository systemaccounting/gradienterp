###############################################
# Per-customer HTTP API gateway.
#
# Single API per customer. Owns the gateway primitive (api, stage, custom
# domain when added). Doesn't know about any specific module's routes —
# domain modules (accounting, inventory, etc.) attach their own integrations +
# routes by sourcing `api_id` and `api_execution_arn` as inputs.
#
# Per-customer placement (in customer's sub-account) gives:
#   - webhook signature verification reads from customer's SSM, same account
#   - failure isolation per customer
#   - cost attribution to the customer's account
#   - matches every other per-customer surface (lambdas, DDB, agent runtime)
#
# Routes register elsewhere — see AGENTS.md in modules/server/.
###############################################


variable "log_retention_days" {
  description = "How long each function's log group keeps its events; root-set (prod/per_customer)."
  type        = number
  default     = 90
}

variable "ops_alerts_topic_arn" {
  description = "The operator's ops topic each function's Errors alarm publishes to; empty = no alarm (local)."
  type        = string
  default     = ""
}

variable "gerp_id" {
  description = "Logical identifier for the tenant — used as the API name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "cognito_user_pool_id" {
  description = "Operator Cognito user pool id (from config.json). When set, a JWT authorizer trusting this pool is created for owner-facing routes (e.g. /secrets). Empty = no authorizer (standalone-applyable)."
  type        = string
  default     = ""
}

variable "cognito_client_id" {
  description = "Operator Cognito app client id (gerp-cloud SPA). The JWT authorizer audience."
  type        = string
  default     = ""
}

variable "owner_app_origins" {
  description = "Browser origins allowed to call owner-facing routes (CORS). The owner web app / dogfood login page."
  type        = list(string)
  default     = ["http://localhost:3000", "https://gradienterp.cloud"]
}

variable "public_read_origins" {
  description = "Browser origins allowed to call the public oob read routes (GET /oob, /oob/*) — the openlyoperated.biz surface. Kept separate from owner_app_origins so the public-read CORS intent stays legible; owner routes remain JWT-gated regardless (CORS is not auth)."
  type        = list(string)
  default     = ["https://openlyoperated.biz", "https://www.openlyoperated.biz"]
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  prefix      = "${var.stack_prefix}-server-${replace(var.gerp_id, "_", "-")}"
  has_cognito = var.cognito_user_pool_id != ""
}

resource "aws_apigatewayv2_api" "this" {
  name          = local.prefix
  protocol_type = "HTTP"
  description   = "Per-customer HTTP API for ${var.gerp_id}. Domain modules attach their own routes."

  # CORS for browser-called routes: owner routes (e.g. /secrets from the login page) + the public oob
  # reads (GET /oob* from openlyoperated.biz). API-level (one config per HTTP API); harmless for
  # server-to-server webhook routes (no browser Origin). GET covers the oob reads; owner routes stay
  # JWT-gated (CORS widens which origins may call, not who's authorized).
  cors_configuration {
    allow_origins = concat(var.owner_app_origins, var.public_read_origins)
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 300
  }
}

# JWT authorizer trusting the operator Cognito pool. Owner-facing routes (in domain
# modules) reference this by id; webhook routes stay unauthenticated + signature-
# verified. Cross-account-clean: validates via the pool's public JWKS, no IAM.
resource "aws_apigatewayv2_authorizer" "owner" {
  count            = local.has_cognito ? 1 : 0
  api_id           = aws_apigatewayv2_api.this.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${local.prefix}-owner"

  jwt_configuration {
    audience = [var.cognito_client_id]
    issuer   = "https://cognito-idp.${split("_", var.cognito_user_pool_id)[0]}.amazonaws.com/${var.cognito_user_pool_id}" # the pool's region, whatever this gerp's
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  # access logs land in CloudWatch — one log group per customer
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId       = "$context.requestId"
      requestTime     = "$context.requestTime"
      httpMethod      = "$context.httpMethod"
      routeKey        = "$context.routeKey"
      status          = "$context.status"
      responseLatency = "$context.responseLatency"
      sourceIp        = "$context.identity.sourceIp"
      # why: the fields that explain a 502 or a 401 the function never saw — the 30 s
      # integration wall, a payload too large, a permission gap, a bad JWT
      integrationStatus = "$context.integration.status"
      integrationError  = "$context.integrationErrorMessage"
      error             = "$context.error.message"
      authorizerError   = "$context.authorizer.error"
    })
  }
}

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${local.prefix}-access"
  retention_in_days = var.log_retention_days
}

# the stage's own failures — a 5xx the function never saw — to the ops topic; the collector's
# threshold reader files it, and the access log above says why
resource "aws_cloudwatch_metric_alarm" "gateway_5xx" {
  count               = var.ops_alerts_topic_arn != "" ? 1 : 0
  alarm_name          = "${local.prefix}-gateway-5xx"
  alarm_description   = "the gerp's API stage answered 5xx; the access log /aws/apigateway/${local.prefix}-access carries integrationStatus, integrationError and error for the request"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.this.id, Stage = aws_apigatewayv2_stage.default.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.ops_alerts_topic_arn]
  ok_actions          = [var.ops_alerts_topic_arn]
}

# ─── outputs ───

output "api_id" {
  value       = aws_apigatewayv2_api.this.id
  description = "HTTP API ID. Domain modules pass this to their aws_apigatewayv2_integration / aws_apigatewayv2_route resources."
}

output "api_endpoint" {
  value       = aws_apigatewayv2_api.this.api_endpoint
  description = "Auto-generated invocation URL: https://<id>.execute-api.<region>.amazonaws.com. Customer pastes this base + the route path (e.g. /webhooks/stripe) into third-party dashboards. Custom-domain mapping comes later."
}

output "api_execution_arn" {
  value       = aws_apigatewayv2_api.this.execution_arn
  description = "ARN root for lambda_permission source_arn. Suffix /*/* covers all stages + routes."
}

output "owner_authorizer_id" {
  value       = local.has_cognito ? aws_apigatewayv2_authorizer.owner[0].id : ""
  description = "JWT authorizer id for owner-facing routes (trusts the operator Cognito pool). Empty when no cognito configured. Domain modules set a route's authorizer_id to this."
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}
