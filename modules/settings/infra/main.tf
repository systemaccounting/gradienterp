# settings — a gerp's settings, in a per-gerp config DDB table. `GET`/`PUT /settings` routes
# on the customer's API gateway, backed by one `tenant_settings` lambda over a low-volume config
# table (`pk = gerp_id`, `sk`): `GERP#<key>` instance-wide settings (`openly_operated` gates
# publication — read by the agent + accounting/treasury/schemas at cold start) and
# `USER#<account_id>` per-user settings (`notification_email`). Provisioning metadata
# (`business_name`/`owner_email`) stays in the SSM tenant blob `/gradienterp/customers/<id>`.


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
  description = "Logical tenant identifier; per-tenant resource-name suffix + the tenant SSM param key."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "server_api_id" {
  description = "Per-customer HTTP API id (from modules/server/infra). The /settings routes attach here."
  type        = string
}

variable "server_api_execution_arn" {
  description = "Per-customer HTTP API execution ARN root. Source ARN for the API-gateway invoke permission."
  type        = string
}

variable "owner_authorizer_id" {
  description = "JWT authorizer id (from modules/server) trusting the operator Cognito pool. When set, /settings requires an owner JWT; empty falls back to AWS_IAM (standalone/no-cognito)."
  type        = string
  default     = ""
}

variable "op_event_bus_arn" {
  description = "The shared operator bus: the openly_operated flip announces itself there (gerp.published / gerp.unpublished)."
  type        = string
}

variable "agent_email_parent_domain" {
  description = "Parent domain for the gerp's agent mailbox (matches modules/agent's var). Lets GET /settings report the agent address + whether the owner's reply identity is verified. Empty ⇒ the email front door is off and the status is reported empty/unverified."
  type        = string
  default     = ""
}

locals {
  prefix = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── config table (pk = gerp_id, sk = GERP#<key> | USER#<account_id>) ───

resource "aws_dynamodb_table" "settings" {
  name         = local.prefix
  tags         = { "gerp:layer" = "config" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gerp_id"
  range_key    = "sk"

  attribute {
    name = "gerp_id"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

# ─── seed (create-only; rows are user-editable after, so never reconciled) ───
#
# Seeded once at provision so the flag keeps parity with the signup value (the readers now
# default absent→false — without this, a customer provisioned openly-operated would silently
# go private at cutover) and the owner's "email me" works out of the box. Both rows are editable
# via PUT /settings, so `ignore_changes = all` makes this seed-once-don't-manage: a later screen
# edit (UpdateItem outside TF) is never reverted on the next apply.

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}" # the tenant blob (openly_operated, owner_email) — provision writes it before this applies
}

data "aws_ssm_parameters_by_path" "customer" {
  path = "/gradienterp/customers/${var.gerp_id}/" # direct children — tolerates absence (owner_sub is stashed only in the real signup flow, not CLI provisioning)
}

locals {
  customer = jsondecode(data.aws_ssm_parameter.customer.value)
  # the owner's Cognito sub if provision stashed it; "" ⇒ no owner USER row seeded (can't key USER#<sub> without it)
  owner_sub = try([
    for i, n in data.aws_ssm_parameters_by_path.customer.names : data.aws_ssm_parameters_by_path.customer.values[i]
    if endswith(n, "/owner_sub")
  ][0], "")
}

resource "aws_dynamodb_table_item" "seed_openly_operated" {
  table_name = aws_dynamodb_table.settings.name
  hash_key   = aws_dynamodb_table.settings.hash_key
  range_key  = aws_dynamodb_table.settings.range_key
  item = jsonencode({
    gerp_id = { S = var.gerp_id }
    sk      = { S = "GERP#openly_operated" }
    value   = { BOOL = try(local.customer.openly_operated, false) }
  })
  lifecycle {
    ignore_changes = all # user-editable via the settings screen — seed once, never revert
  }
}

# Every gerp is born with LOCATION#1 — "main", the permanent default-location anchor. The
# ordinal is the identifier stamped everywhere (item ids, journal dims); label/city are
# description the owner edits at onboarding. #1 is the default by doctrine (posting paths stamp
# the constant "1", never a settings read), so main office is where shared costs land.
resource "aws_dynamodb_table_item" "seed_location_1" {
  table_name = aws_dynamodb_table.settings.name
  hash_key   = aws_dynamodb_table.settings.hash_key
  range_key  = aws_dynamodb_table.settings.range_key
  item = jsonencode({
    gerp_id = { S = var.gerp_id }
    sk      = { S = "LOCATION#1##main" }
    label   = { S = "Main" }
  })
  lifecycle {
    ignore_changes = all # owner-editable (label/city rename rewrites the row keeping the ordinal)
  }
}

resource "aws_dynamodb_table_item" "seed_owner_user" {
  count      = local.owner_sub != "" ? 1 : 0
  table_name = aws_dynamodb_table.settings.name
  hash_key   = aws_dynamodb_table.settings.hash_key
  range_key  = aws_dynamodb_table.settings.range_key
  item = jsonencode({
    gerp_id            = { S = var.gerp_id }
    sk                 = { S = "USER#${local.owner_sub}" }
    notification_email = { S = try(local.customer.owner_email, "") } # = owner_email, already SES-verified as the reply recipient
  })
  lifecycle {
    ignore_changes = all
  }
}

# ─── iam ───

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

resource "aws_iam_role_policy" "lambda" {
  name = "${local.prefix}-lambda"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the config table: GERP#/USER# rows read (GET) + upserted (PUT); LOCATION# rows
        # listed (Query) + rewritten (Put/Delete — a rename replaces the sk keeping the ordinal).
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.settings.arn
      },
      {
        # read the tenant blob (owner_email) for the agent-mailbox verify status. GetParameter only —
        # openly_operated moved to DDB, so no PutParameter; the blob is provisioning metadata now.
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}"
      },
      {
        # the flip announces itself to the operator
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.op_event_bus_arn
      },
      {
        # verify a newly set notification address + report verify status (neither is resource-scopable)
        Effect   = "Allow"
        Action   = ["ses:VerifyEmailIdentity", "ses:GetIdentityVerificationAttributes"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambda ───

# ─── manage_locations — the agent's gateway tool over the LOCATION# rows ───

variable "register_with_agent" {
  description = "Register manage_locations as an MCP tool on the customer's agent gateway."
  type        = bool
  default     = true
}

module "manage_locations" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-manage_locations"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/settings/lambdas/manage_locations.zip"
  src_dir         = "modules/settings/lambdas/manage_locations"
  gerp_id         = var.gerp_id
  timeout         = 10
  env_vars = {
    CUSTOMER_ID    = var.gerp_id
    SETTINGS_TABLE = aws_dynamodb_table.settings.name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.manage_locations
  to   = module.manage_locations.aws_lambda_function.this
}


resource "aws_bedrockagentcore_gateway_target" "manage_locations" {
  count              = var.register_with_agent ? 1 : 0
  gateway_identifier = var.gateway_id
  lifecycle {
    ignore_changes = [gateway_identifier] # immutable per customer; see other modules' rationale
  }
  name        = "manage-locations"
  description = jsondecode(file("${path.module}/../lambdas/manage_locations/schema.json")).description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.manage_locations.arn

        tool_schema {
          inline_payload {
            name        = "manage_locations"
            description = jsondecode(file("${path.module}/../lambdas/manage_locations/schema.json")).description
            input_schema {
              type        = "object"
              description = jsondecode(file("${path.module}/../lambdas/manage_locations/schema.json")).description

              dynamic "property" {
                iterator = prop
                for_each = jsondecode(file("${path.module}/../lambdas/manage_locations/schema.json")).properties
                content {
                  name        = prop.key
                  type        = try(prop.value.type, "string")
                  description = try(prop.value.description, "")
                  required    = contains(jsondecode(file("${path.module}/../lambdas/manage_locations/schema.json")).required, prop.key)
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

resource "aws_lambda_permission" "gateway_invoke_manage_locations" {
  count               = var.register_with_agent ? 1 : 0
  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.manage_locations.name
  principal           = var.gateway_role_arn
  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

module "tenant_settings" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-tenant_settings"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/settings/lambdas/tenant_settings.zip"
  src_dir         = "modules/settings/lambdas/tenant_settings"
  gerp_id         = var.gerp_id
  timeout         = 10
  env_vars = {
    CUSTOMER_ID               = var.gerp_id
    OP_EVENT_BUS_ARN          = var.op_event_bus_arn
    SETTINGS_TABLE            = aws_dynamodb_table.settings.name
    AGENT_EMAIL_PARENT_DOMAIN = var.agent_email_parent_domain
    OWNER_SUB_PARAM           = "/gradienterp/customers/${var.gerp_id}/owner_sub" # the owner routes answer only this sub
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.tenant_settings
  to   = module.tenant_settings.aws_lambda_function.this
}


# ─── api gateway routes (GET / PUT /settings) ───
# Owner-authed: when an owner JWT authorizer is wired (modules/server, trusting the operator
# Cognito pool) the routes require the owner's id token; falls back to AWS_IAM standalone.

resource "aws_apigatewayv2_integration" "tenant_settings" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.tenant_settings.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "settings" {
  for_each           = toset(["GET /settings", "PUT /settings"])
  api_id             = var.server_api_id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.tenant_settings.id}"
  authorization_type = var.owner_authorizer_id != "" ? "JWT" : "AWS_IAM"
  authorizer_id      = var.owner_authorizer_id != "" ? var.owner_authorizer_id : null
}

resource "aws_lambda_permission" "apigw_tenant_settings" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeTenantSettings"
  action              = "lambda:InvokeFunction"
  function_name       = module.tenant_settings.name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# ─── outputs ───

output "tenant_settings_fn_name" {
  value = module.tenant_settings.name
}

output "settings_table_name" {
  description = "Config table name — the emit-readers (accounting/treasury/schemas) read GERP#openly_operated from it; provisioning seeds the GERP + owner USER rows."
  value       = aws_dynamodb_table.settings.name
}

output "settings_table_arn" {
  description = "Config table ARN — for the reader lambdas' GetItem IAM."
  value       = aws_dynamodb_table.settings.arn
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "gateway_id" {
  description = "The agent's gateway this module registers its tools on — module.agent.gateway_id. Read at plan as an input, never from SSM: a fresh account has no parameter to read yet."
  type        = string
  default     = ""
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes tools as — module.agent.gateway_role_arn; the principal on each tool lambda's invoke permission."
  type        = string
  default     = ""
}

# The owner routes read who the owner is: the one parameter, nothing beside it.
resource "aws_iam_role_policy" "owner_sub" {
  name = "${local.prefix}-settings-owner-sub"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/owner_sub"
    }]
  })
}
