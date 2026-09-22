variable "gerp_id" {
  description = "logical identifier for the tenant this notes stack belongs to."
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
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). manage_notes queries it at cold start for field-name validation."
  type        = string
}

variable "register_with_agent" {
  description = "Register notes lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}"
}

locals {
  prefix = "${var.stack_prefix}-notes-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (notes) ───
#
# Append-only versioning. Each put/update appends a new row with the same
# note_id and a fresh version_ts. The get op returns the latest version
# (Query partition ScanIndexForward=false Limit=1); the query op returns the
# latest version per matching note (lambda dedupes after the GSI Query).

resource "aws_dynamodb_table" "notes" {
  name         = local.prefix
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "note_id"
  range_key    = "version_ts"

  attribute {
    name = "note_id"
    type = "S"
  }

  attribute {
    name = "version_ts"
    type = "S"
  }

  attribute {
    name = "contact_id"
    type = "S"
  }

  attribute {
    name = "journal_entry_id"
    type = "S"
  }

  attribute {
    name = "purchase_order_id"
    type = "S"
  }

  attribute {
    name = "invoice_id"
    type = "S"
  }

  global_secondary_index {
    name = "contact-index"
    key_schema {
      attribute_name = "contact_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "version_ts"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  global_secondary_index {
    name = "journal-entry-index"
    key_schema {
      attribute_name = "journal_entry_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "version_ts"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  global_secondary_index {
    name = "purchase-order-index"
    key_schema {
      attribute_name = "purchase_order_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "version_ts"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  global_secondary_index {
    name = "invoice-index"
    key_schema {
      attribute_name = "invoice_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "version_ts"
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
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          aws_dynamodb_table.notes.arn,
          "${aws_dynamodb_table.notes.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
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

# ─── lambdas ───

locals {
  functions = toset([
    "manage_notes",
  ])

  env_vars = {
    NOTES_TABLE  = aws_dynamodb_table.notes.name
    SCHEMA_TABLE = var.schema_table_name
    CUSTOMER_ID  = var.gerp_id
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/notes/lambdas/${each.key}.zip"
  src_dir            = "modules/notes/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_notes"]
  to   = module.fn["manage_notes"].aws_lambda_function.this
}


# ─── agent gateway registration ───

locals {
  tool_schemas = {
    for k in local.functions :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent && fileexists("${path.module}/../lambdas/${k}/schema.json")
  }
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_schemas

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
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

                  dynamic "property" {
                    iterator = innerprop
                    for_each = prop.value.type == "object" ? try(prop.value.properties, {}) : {}
                    content {
                      name        = innerprop.key
                      type        = innerprop.value.type
                      description = try(innerprop.value.description, "")
                      required    = contains(try(prop.value.required, []), innerprop.key)
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
  for_each = local.tool_schemas

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn[each.key].name
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

output "notes_table" {
  description = "DDB notes table name. PK note_id, SK version_ts; append-only versioning."
  value       = aws_dynamodb_table.notes.name
}

output "notes_stream_arn" {
  value = aws_dynamodb_table.notes.stream_arn
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "lambda_arns" {
  value = { for k, fn in module.fn : k => fn.arn }
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
