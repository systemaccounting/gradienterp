variable "gerp_id" {
  description = "logical identifier for the tenant this shipping stack belongs to."
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
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). manage_shipments queries it at cold start for shipping_fields validation."
  type        = string
}

variable "post_journal_entry_fn_arn" {
  description = "arn of the accounting module's post_journal_entry lambda — the freight leg"
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of the accounting module's post_journal_entry lambda"
  type        = string
}

variable "register_with_agent" {
  description = "Register manage_shipments as an MCP tool on the customer's agent gateway."
  type        = bool
  default     = true
}

locals {
  prefix = "${var.stack_prefix}-shipping-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (custody records) ───
#
# One row per shipment/parcel, either direction. open-shipments-index is the working
# view (in flight / on the dock / awaiting pickup): open_flag is a lambda-managed
# sparse marker, dropped at terminal status. Stream feeds the future Pipes fabric.

resource "aws_dynamodb_table" "shipments" {
  name         = local.prefix
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "shipment_id"

  attribute {
    name = "shipment_id"
    type = "S"
  }

  attribute {
    name = "open_flag"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "N"
  }

  global_secondary_index {
    name = "open-shipments-index"
    key_schema {
      attribute_name = "open_flag"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
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
          "dynamodb:Query",
        ]
        Resource = [
          aws_dynamodb_table.shipments.arn,
          "${aws_dynamodb_table.shipments.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
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
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambda ───

module "manage_shipments" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-manage_shipments"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/shipping/lambdas/manage_shipments.zip"
  src_dir         = "modules/shipping/lambdas/manage_shipments"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    SHIPMENTS_TABLE       = aws_dynamodb_table.shipments.name
    SCHEMA_TABLE          = var.schema_table_name
    CUSTOMER_ID           = var.gerp_id
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.manage_shipments
  to   = module.manage_shipments.aws_lambda_function.this
}


# the inbound custody record: the router invokes this when a counterparty's `shipment.sent` lands.
# Not a gateway tool (no schema.json) — nothing calls it but the router.
module "apply_shipment_event" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-apply_shipment_event"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/shipping/lambdas/apply_shipment_event.zip"
  src_dir         = "modules/shipping/lambdas/apply_shipment_event"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    SHIPMENTS_TABLE       = aws_dynamodb_table.shipments.name
    SCHEMA_TABLE          = var.schema_table_name
    CUSTOMER_ID           = var.gerp_id
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.apply_shipment_event
  to   = module.apply_shipment_event.aws_lambda_function.this
}


# ─── agent gateway registration ───

locals {
  tool_schema = var.register_with_agent ? jsondecode(file("${path.module}/../lambdas/manage_shipments/schema.json")) : null
}

resource "aws_bedrockagentcore_gateway_target" "manage_shipments" {
  count = var.register_with_agent ? 1 : 0

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = "manage-shipments"
  description = local.tool_schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.manage_shipments.arn

        tool_schema {
          inline_payload {
            name        = "manage_shipments"
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

                  dynamic "items" {
                    for_each = prop.value.type == "array" ? [prop.value.items] : []
                    content {
                      type        = items.value.type
                      description = try(items.value.description, "")
                      dynamic "property" {
                        iterator = innerprop
                        for_each = try(items.value.properties, {})
                        content {
                          name        = innerprop.key
                          type        = innerprop.value.type
                          description = try(innerprop.value.description, "")
                          required    = contains(try(items.value.required, []), innerprop.key)
                        }
                      }
                    }
                  }
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
  function_name       = module.manage_shipments.name
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

output "shipments_table" {
  description = "DDB shipments table name. PK shipment_id (`<ordinal>#s-<hex>`); one custody row per shipment/parcel."
  value       = aws_dynamodb_table.shipments.name
}

output "shipments_stream_arn" {
  value = aws_dynamodb_table.shipments.stream_arn
}

output "manage_shipments_fn" {
  value = module.manage_shipments.name
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
