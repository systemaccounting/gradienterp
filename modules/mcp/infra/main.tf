# modules/mcp — a vendor's MCP server installed as a target on the firm's vendor gateway.
#
# Two gateways per gerp. The main one (modules/agent) is AWS_IAM and holds the platform's own
# tools. This one is CUSTOM_JWT and holds one `mcp_server` target per vendor the firm installed,
# because a three-legged OAuth target is refused on an AWS_IAM gateway and its grant keys on the
# caller's `sub`. The firm's `sub` is one Cognito client-credentials app client per gerp, made
# here on the operator's pool: the container and automation fetch that token and call this
# gateway as the firm, so a vendor grant is the firm's, not an owner's, and an automated turn
# holds it too.
#
# Targets, credential providers and the `MCP#<provider>` rows are the door's (lambdas/manage_mcp),
# made at install; this file is the shape that exists before any vendor does.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.operator]
      version               = "~> 6.65"
    }
  }
}

variable "gerp_id" {
  description = "logical identifier for the tenant this stack belongs to."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root."
  type        = string
  default     = "gerp"
}

variable "log_retention_days" {
  description = "How long each function's log group keeps its events; root-set (prod/per_customer)."
  type        = number
  default     = 90
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "settings_table_name" {
  description = "The gerp's settings table (modules/settings); the `MCP#<provider>` rows live there."
  type        = string
}

variable "settings_table_arn" {
  type = string
}

variable "cognito_user_pool_id" {
  description = "The gradienterp.cloud pool in the operator account; the firm's app client is made on it and the vendor gateway trusts its issuer."
  type        = string
}

variable "cognito_token_url" {
  description = "The pool's token endpoint (`https://<domain>.auth.<region>.amazoncognito.com/oauth2/token`); callers fetch the firm's token there."
  type        = string
}

variable "landing_url" {
  description = "Where a vendor's consent returns the owner: the gerp-cloud page that completes the session."
  type        = string
  default     = "https://gradienterp.cloud/mcp/callback"
}

variable "landing_invoker_role_arn" {
  description = "The gerp-cloud BFF's role, allowed to invoke complete_mcp_auth cross-account when the owner lands from a vendor's consent. Set on every gerp."
  type        = string
  default     = "arn:aws:iam::185369506315:role/gerp-cloud-bff"
}

variable "gateway_id" {
  description = "The MAIN gateway (module.agent) this module registers manage_mcp on. Read at plan as an input, never from SSM."
  type        = string
  default     = ""
}

variable "gateway_role_arn" {
  description = "The main gateway's role — the principal on manage_mcp's invoke permission."
  type        = string
  default     = ""
}

variable "register_with_agent" {
  type    = bool
  default = true
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  prefix   = "${var.stack_prefix}-mcp-${replace(var.gerp_id, "_", "-")}"
  ssm_root = "/gradienterp/customers/${var.gerp_id}/mcp"
}

# ─── the firm's identity: one client-credentials app client on the operator's pool ───
#
# `sub` on its token is the client id — the firm, with no person in it. The resource server
# `gerp-mcp` (scope `call`) is the pool's, made once in prod/platform/operator. 24h tokens: the
# container caches one, so Cognito's per-request M2M charge is ~30 requests a month.

resource "aws_cognito_user_pool_client" "firm" {
  provider = aws.operator

  name            = local.prefix
  user_pool_id    = var.cognito_user_pool_id
  generate_secret = true

  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = ["gerp-mcp/call"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]

  access_token_validity = 24
  token_validity_units {
    access_token = "hours"
  }
}

# The callers read these at runtime by name: the container and automation's lambda.
resource "aws_ssm_parameter" "client_id" {
  name  = "${local.ssm_root}/client_id"
  type  = "String"
  value = aws_cognito_user_pool_client.firm.id
}

resource "aws_ssm_parameter" "client_secret" {
  name  = "${local.ssm_root}/client_secret"
  type  = "SecureString"
  value = aws_cognito_user_pool_client.firm.client_secret
}

resource "aws_ssm_parameter" "token_url" {
  name  = "${local.ssm_root}/token_url"
  type  = "String"
  value = var.cognito_token_url
}

# ─── the vendor gateway ───

data "aws_iam_policy_document" "gateway_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${local.prefix}-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust.json
}

# What a 3LO target needs of the gateway's role, found by its own log: the workload token for the
# caller, the vendor token from the vault — on `token-vault/default` itself, not only the
# provider's arn — and the credential provider's secret, which Identity keeps in Secrets Manager
# under `bedrock-agentcore-identity!default/`.
data "aws_iam_policy_document" "gateway" {
  statement {
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
      "bedrock-agentcore:GetResourceOauth2Token",
      "bedrock-agentcore:GetResourceApiKey",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/*",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default/*",
    ]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:bedrock-agentcore-identity!default/*"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "${local.prefix}-gateway"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_bedrockagentcore_gateway" "vendors" {
  name        = local.prefix
  description = "vendor MCP servers installed for ${var.gerp_id}; callers carry the firm's token"
  role_arn    = aws_iam_role.gateway.arn

  authorizer_type = "CUSTOM_JWT"
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = "https://cognito-idp.${split("_", var.cognito_user_pool_id)[0]}.amazonaws.com/${var.cognito_user_pool_id}/.well-known/openid-configuration"
      allowed_clients = [aws_cognito_user_pool_client.firm.id]
    }
  }

  protocol_type = "MCP"
  protocol_configuration {
    mcp {
      # a per-caller consent reaches the caller as MCP url elicitation, which needs 2025-11-25;
      # the default set is 2025-03-26 alone and the gateway logs the refusal instead
      supported_versions = ["2025-11-25", "2025-06-18", "2025-03-26"]
      # the gateway's own x_amz_bedrock_agentcore_search tool: the container's search mode
      # (past VENDOR_TOOLS_INLINE_MAX mounted tools) finds a task's tools by query
      search_type = "SEMANTIC"
    }
  }
}

resource "aws_ssm_parameter" "gateway_url" {
  name  = "${local.ssm_root}/gateway_url"
  type  = "String"
  value = aws_bedrockagentcore_gateway.vendors.gateway_url
}

# ─── the gateway's log: what the agent did at each vendor ───

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/aws/bedrock-agentcore/gateway/${local.prefix}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_delivery_source" "gateway" {
  name         = "${local.prefix}-gateway"
  log_type     = "APPLICATION_LOGS"
  resource_arn = aws_bedrockagentcore_gateway.vendors.gateway_arn
}

resource "aws_cloudwatch_log_delivery_destination" "gateway" {
  name = "${local.prefix}-gateway"
  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.gateway.arn
  }
}

resource "aws_cloudwatch_log_delivery" "gateway" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.gateway.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.gateway.arn
}

# ─── the door and the completion ───

resource "aws_iam_role" "lambda" {
  name = "${local.prefix}-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

data "aws_iam_policy_document" "lambda" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"]
    resources = [var.settings_table_arn]
  }
  # the key path reads an owner-submitted secret by name; the door's own parameters beside it
  statement {
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/secrets/*",
      "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_root}/*",
    ]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }
  # the install: credential providers in the vault, targets on the vendor gateway, the gateway's
  # workload identity (its allowed return urls), and the completion of a consent
  statement {
    actions = [
      # the account's default token vault is made by the first credential provider
      "bedrock-agentcore:CreateTokenVault",
      "bedrock-agentcore:GetTokenVault",
      "bedrock-agentcore:CreateOauth2CredentialProvider",
      "bedrock-agentcore:UpdateOauth2CredentialProvider",
      "bedrock-agentcore:GetOauth2CredentialProvider",
      "bedrock-agentcore:DeleteOauth2CredentialProvider",
      "bedrock-agentcore:CreateApiKeyCredentialProvider",
      "bedrock-agentcore:GetApiKeyCredentialProvider",
      "bedrock-agentcore:DeleteApiKeyCredentialProvider",
      "bedrock-agentcore:GetWorkloadIdentity",
      "bedrock-agentcore:UpdateWorkloadIdentity",
      "bedrock-agentcore:CompleteResourceTokenAuth",
      # a 3LO target's own consent is started as the caller of CreateGatewayTarget: the door
      # holds the gateway's workload token and asks the vault, the way the gateway will
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
      "bedrock-agentcore:GetResourceOauth2Token",
      # a key target's first sync reads the key as the caller too
      "bedrock-agentcore:GetResourceApiKey",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:token-vault/default/*",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/*",
    ]
  }
  statement {
    actions = [
      "bedrock-agentcore:GetGateway",
      "bedrock-agentcore:CreateGatewayTarget",
      "bedrock-agentcore:GetGatewayTarget",
      "bedrock-agentcore:ListGatewayTargets",
      "bedrock-agentcore:DeleteGatewayTarget",
      "bedrock-agentcore:SynchronizeGatewayTargets",
    ]
    resources = [
      aws_bedrockagentcore_gateway.vendors.gateway_arn,
      "${aws_bedrockagentcore_gateway.vendors.gateway_arn}/*",
    ]
  }
  # a credential provider is a Secrets Manager secret Identity writes in this account; the
  # target's own consent reads it back as the caller
  statement {
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
      "secretsmanager:UpdateSecret",
      "secretsmanager:DeleteSecret",
      "secretsmanager:TagResource",
    ]
    resources = ["arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:bedrock-agentcore-identity!default/*"]
  }
  # CreateGatewayTarget hands the gateway's role to the service
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.gateway.arn]
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.prefix}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

locals {
  functions = toset(["manage_mcp", "complete_mcp_auth"])

  env_vars = {
    SETTINGS_TABLE     = var.settings_table_name
    VENDOR_GATEWAY_ID  = aws_bedrockagentcore_gateway.vendors.gateway_id
    VENDOR_GATEWAY_ARN = aws_bedrockagentcore_gateway.vendors.gateway_arn
    VENDOR_GATEWAY_URL = aws_bedrockagentcore_gateway.vendors.gateway_url
    LANDING_URL        = var.landing_url
    SECRETS_PREFIX     = "/gradienterp/customers/${var.gerp_id}/secrets/"
    # the zip carries the current AgentCore service models (scripts/deploy.py): the runtime's
    # boto3 lags the fields the door sends
    AWS_DATA_PATH = "/var/task/botocore_data"
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/mcp/lambdas/${each.key}.zip"
  src_dir            = "modules/mcp/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 60
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

# the owner lands on gerp-cloud after a vendor's consent; the BFF calls this in the gerp's account
resource "aws_lambda_permission" "landing_invoker" {
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowBffLanding"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["complete_mcp_auth"].name
  principal           = var.landing_invoker_role_arn
}

# ─── the door on the main gateway ───

locals {
  tool_schemas = var.register_with_agent ? {
    manage_mcp = jsondecode(file("${path.module}/../lambdas/manage_mcp/schema.json"))
  } : {}
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_schemas

  gateway_identifier = var.gateway_id
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = replace(each.key, "_", "-")
  description = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.fn[each.key].arn

        tool_schema {
          inline_payload {
            name        = each.key
            description = each.value.description
            input_schema {
              type        = each.value.type
              description = each.value.description

              dynamic "property" {
                iterator = prop
                for_each = try(each.value.properties, {})
                content {
                  name        = prop.key
                  type        = prop.value.type
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.required, []), prop.key)
                }
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

resource "aws_lambda_permission" "gateway_invoke" {
  for_each = local.tool_schemas

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn[each.key].name
  principal           = var.gateway_role_arn

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── outputs ───

output "vendor_gateway_id" {
  value = aws_bedrockagentcore_gateway.vendors.gateway_id
}

output "vendor_gateway_url" {
  value = aws_bedrockagentcore_gateway.vendors.gateway_url
}

output "vendor_gateway_arn" {
  value = aws_bedrockagentcore_gateway.vendors.gateway_arn
}

output "firm_client_id" {
  description = "The firm's app client on the operator's pool — the `sub` every caller of the vendor gateway carries."
  value       = aws_cognito_user_pool_client.firm.id
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}
