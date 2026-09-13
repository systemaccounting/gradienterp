locals {
  prefix = "${var.stack_prefix}-labor-${replace(var.gerp_id, "_", "-")}"
  # the agent's encrypted uploads bucket (modules/agent/infra/uploads.tf) — same
  # constructed name. manage_labor's delete op removes a worker_legal row's referenced
  # doc blobs (I-9 / ID scans) from here before deleting the row.
  uploads_bucket = "${var.stack_prefix}-agent-${replace(var.gerp_id, "_", "-")}-uploads-${data.aws_caller_identity.current.account_id}"
}

# ─── dynamodb ───
#
# Three schemaless tables (extend the labor_fields registry, not the schema):
#   worker        — config / rate book. one row per (contact_id, role) a person
#                   is paid for. holds rate + classification (W-2/1099).
#   time-entries  — clock-in/out events. durably holds the OPEN clock-ins; thin
#                   (no rate / gross — a worker lookup + ledger queries give those).
#                   Streams feed the close-handler (modules/labor/infra/close_handler.tf):
#                   on a clock-out, resolve the rate from worker and post the accrual.
#   worker-legal  — PII / legal, EAV. typed JSON per (worker_id, role#type). the
#                   agent sees the SSN-masked JSON; raw SSN / bank live in the
#                   secure store, injected by the emitter only at filing.

resource "aws_dynamodb_table" "worker" {
  name         = "${local.prefix}-worker"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "contact_id"
  range_key    = "role"

  attribute {
    name = "contact_id"
    type = "S"
  }

  attribute {
    name = "role"
    type = "S"
  }
}

resource "aws_dynamodb_table" "time_entries" {
  name         = "${local.prefix}-time-entries"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "worker_id"
  range_key    = "entry_id"

  attribute {
    name = "worker_id"
    type = "S"
  }

  attribute {
    name = "entry_id"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
}

resource "aws_dynamodb_table" "worker_legal" {
  name         = "${local.prefix}-worker-legal"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "worker_id"
  range_key    = "sk" # composite role#type

  attribute {
    name = "worker_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
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
        Effect   = "Allow",
        Action   = ["dynamodb:GetItem"],
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
        # `clock` reads GERP#timezone at cold start — the owner-editable source of truth for which
        # calendar a period closes on. Arn constructed, not an output: no cross-module dependency.
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          aws_dynamodb_table.worker.arn,
          aws_dynamodb_table.time_entries.arn,
          aws_dynamodb_table.worker_legal.arn,
        ]
      },
      {
        # manage_labor's delete op drops a worker_legal row's uploaded doc(s) (I-9 / ID
        # scans) from the agent's encrypted uploads bucket before deleting the row.
        # DeleteObject needs no KMS (only Get/Put touch the key).
        Effect   = "Allow"
        Action   = "s3:DeleteObject"
        Resource = "arn:aws:s3:::${local.uploads_bucket}/*"
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

# ─── lambdas (agent's parameterized DDB tools) ───
#
# manage_labor — one op-routed CRUD tool over the three tables (the caller passes
# the op and which table). On worker-legal the agent sees the masked JSON only.
# close-handler is the one compute lambda and is wired separately
# (modules/labor/infra/close_handler.tf) onto time_entries' stream.

locals {
  functions = toset([
    "manage_labor",
  ])

  env_vars = {
    WORKER_TABLE          = aws_dynamodb_table.worker.name
    TIME_ENTRIES_TABLE    = aws_dynamodb_table.time_entries.name
    WORKER_LEGAL_TABLE    = aws_dynamodb_table.worker_legal.name
    SCHEMA_TABLE          = var.schema_table_name
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    CUSTOMER_ID           = var.gerp_id
    # the delete op removes a worker_legal row's referenced doc blobs from here first.
    UPLOADS_BUCKET = local.uploads_bucket
    # The business's clock — `clock.py` places a naive shift time ("7am") in THIS zone instead of
    # assuming UTC. Unset ⇒ UTC ⇒ exactly the prior behaviour.
    GERP_TIMEZONE  = var.timezone
    SETTINGS_TABLE = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}" # clock reads GERP#timezone; env var is the fallback
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/labor/lambdas/${each.key}.zip"
  src_dir            = "modules/labor/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_labor"]
  to   = module.fn["manage_labor"].aws_lambda_function.this
}


# ─── agent gateway registration ───
#
# Same pattern as modules/tasks / modules/inventory: each lambda with a sibling
# schema.json registers as an MCP tool against the agent's gateway, discovered
# via SSM. Permission is resource-based (aws_lambda_permission), keyed off the
# gateway's role ARN (also SSM).

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
                  type        = try(prop.value.type, "object")
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.required, []), prop.key)

                  dynamic "items" {
                    for_each = try(prop.value.type, "") == "array" ? [prop.value.items] : []
                    content {
                      type        = try(items.value.type, "object")
                      description = try(items.value.description, "")
                      dynamic "property" {
                        iterator = innerprop
                        for_each = try(items.value.properties, {})
                        content {
                          name        = innerprop.key
                          type        = try(innerprop.value.type, "object")
                          description = try(innerprop.value.description, "")
                          required    = contains(try(items.value.required, []), innerprop.key)
                        }
                      }
                    }
                  }

                  dynamic "property" {
                    iterator = innerprop
                    for_each = try(prop.value.type, "") == "object" ? try(prop.value.properties, {}) : {}
                    content {
                      name        = innerprop.key
                      type        = try(innerprop.value.type, "object")
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

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.
