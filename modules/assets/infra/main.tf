variable "gerp_id" {
  description = "logical identifier for the tenant this assets stack belongs to."
  type        = string
}


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

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "schema_table_name" {
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). manage_assets queries it at cold start for asset_fields validation."
  type        = string
}

variable "post_journal_entry_fn_arn" {
  description = "arn of the accounting module's post_journal_entry lambda — the acquisition entry (no capitalized row without a journal entry)"
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of the accounting module's post_journal_entry lambda"
  type        = string
}

variable "register_with_agent" {
  description = "Register manage_assets as an MCP tool on the customer's agent gateway."
  type        = bool
  default     = true
}

locals {
  prefix = "${var.stack_prefix}-assets-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (the register) ───
#
# One current row per asset (a register, not a movement log — stock is inventory's job).
# Stream feeds the future Pipes event fabric so later subscribers attach without
# touching this module.

resource "aws_dynamodb_table" "assets" {
  name         = local.prefix
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "asset_id"

  attribute {
    name = "asset_id"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
}

# ─── iam ───

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

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
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Scan",
        ]
        Resource = [aws_dynamodb_table.assets.arn]
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambda ───

module "manage_assets" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-manage_assets"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/assets/lambdas/manage_assets.zip"
  src_dir         = "modules/assets/lambdas/manage_assets"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    ASSETS_TABLE          = aws_dynamodb_table.assets.name
    SCHEMA_TABLE          = var.schema_table_name
    CUSTOMER_ID           = var.gerp_id
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.manage_assets
  to   = module.manage_assets.aws_lambda_function.this
}


# ─── agent gateway registration ───

locals {
  tool_schema = var.register_with_agent ? jsondecode(file("${path.module}/../lambdas/manage_assets/schema.json")) : null
}

resource "aws_bedrockagentcore_gateway_target" "manage_assets" {
  count = var.register_with_agent ? 1 : 0

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = "manage-assets"
  description = local.tool_schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.manage_assets.arn

        tool_schema {
          inline_payload {
            name        = "manage_assets"
            description = local.tool_schema.description
            input_schema {
              type        = local.tool_schema.type
              description = local.tool_schema.description

              dynamic "property" {
                iterator = prop
                for_each = local.tool_schema.properties
                content {
                  name        = prop.key
                  type        = prop.value.type
                  description = try(prop.value.description, "")
                  required    = contains(try(local.tool_schema.required, []), prop.key)
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
  count = var.register_with_agent ? 1 : 0

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.manage_assets.name
  principal           = var.gateway_role_arn

  # gateway role arn is immutable per customer — pin it (same rationale as gateway_identifier on
  # the target): an agent-image bump defers this SSM read, and principal is force-new, so without
  # this every gateway-invoke permission would be needlessly delete+recreated.
  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── outputs ───

output "assets_table" {
  description = "DDB asset register table name. PK asset_id (`<ordinal>#<slug>`); one current row per asset."
  value       = aws_dynamodb_table.assets.name
}

output "assets_stream_arn" {
  value = aws_dynamodb_table.assets.stream_arn
}

output "manage_assets_fn" {
  value = module.manage_assets.name
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
