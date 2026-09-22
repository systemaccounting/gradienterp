variable "gerp_id" {
  description = "logical identifier for the tenant this tasks stack belongs to."
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
  description = "Per-customer registry DDB table name. manage_tasks queries it at cold start for field-name validation."
  type        = string
}

variable "op_event_bus_arn" {
  description = "Shared operator event bus — the escalate tool emits platform/escalation.raised here."
  type        = string
}

variable "issue_collector_role_arn" {
  description = "The operator issue-collector's role (constructed name — the operator platform stack creates it before any per_customer apply). It cross-account-invokes tasks_put to land each escalation as an inc task on the operator gerp's books."
  type        = string
}

variable "register_with_agent" {
  description = "Register tasks lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}"
}

locals {
  prefix = "${var.stack_prefix}-tasks-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (tasks) ───
#
# In-place mutation (no append-only versioning — see modules/tasks/TODO.md
# § design notes). PK task_id; lifecycle tracked via resolved_at + a
# lambda-managed open_flag sparse-GSI partition.

# A task is a PARTITION: (task_id, sk) — sk="HEADER" is current state (all GSIs live on the
# header's attributes; changelog rows don't carry them, so the indexes stay header-only),
# sk="<ms>#<id>" rows are the append-only changelog. Re-keyed 2026-07-31 (destroy/create —
# the reshape); the stream arn changed with it, and the ui lambda's version-bump ESM follows
# via per_customer wiring.
resource "aws_dynamodb_table" "tasks" {
  name         = local.prefix
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "task_id"
  range_key    = "sk"

  attribute {
    name = "task_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "contact_id"
    type = "S"
  }

  # tag rows only. A task's header and changelog carry neither, so they stay out of the index —
  # DynamoDB omits an item missing an index key, which is what keeps this index the tag list rather
  # than the table again.
  attribute {
    name = "gsi_tag"
    type = "S"
  }

  attribute {
    name = "gsi_tag_sk"
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

  attribute {
    name = "open_flag"
    type = "S"
  }

  attribute {
    name = "due_date"
    type = "S"
  }

  attribute {
    name = "subject_key"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "N"
  }

  global_secondary_index {
    name = "contact-index"
    key_schema {
      attribute_name = "contact_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "due_date"
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
      attribute_name = "due_date"
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
      attribute_name = "due_date"
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
      attribute_name = "due_date"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  # "every task tagged X". A set cannot be a key, so the tags are rows and this is how they are
  # found without scanning. Sorted newest first by `<applied_at>#<task_id>`.
  global_secondary_index {
    name = "tag-index"
    key_schema {
      attribute_name = "gsi_tag"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "gsi_tag_sk"
      key_type       = "RANGE"
    }
    projection_type = "KEYS_ONLY"
  }

  # subject-index ranges on created_at (always present) rather than due_date —
  # incidents rarely carry a due date, and service history reads in time order.
  global_secondary_index {
    name = "subject-index"
    key_schema {
      attribute_name = "subject_key"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  # ranged on created_at (always present) — a GSI is SPARSE, and ranging on due_date hid
  # every open task without a stamped ceiling from the queue. Lapse order = allocation order.
  global_secondary_index {
    name = "open-tasks-index"
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
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          aws_dynamodb_table.tasks.arn,
          "${aws_dynamodb_table.tasks.arn}/index/*",
        ]
      },
      {
        # Query for the field registry that validates a task's fields; GetItem for one tag's
        # declaration — `tag_declared` reads a single key rather than listing the vocabulary.
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
      },
      {
        # escalate emits platform/escalation.raised to the shared operator bus
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.op_event_bus_arn
      },
      {
        # tasks_poke consumes this table's stream
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams",
        ]
        Resource = "${aws_dynamodb_table.tasks.arn}/stream/*"
      },
      {
        # tasks_poke wakes this gerp's own agent (the inbox poke_agent shape)
        Effect = "Allow"
        Action = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:runtime/*",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:runtime-endpoint/*",
        ]
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
    "manage_tasks",
    "escalate",
    "tasks_poke",
  ])

  env_vars = {
    TASKS_TABLE      = aws_dynamodb_table.tasks.name
    SCHEMA_TABLE     = var.schema_table_name
    CUSTOMER_ID      = var.gerp_id
    OP_EVENT_BUS_ARN = var.op_event_bus_arn # escalate emits platform/escalation.raised
    # tasks_poke wakes the gerp's own agent; empty when no agent registers (poke ESM absent too)
    AGENT_RUNTIME_ENDPOINT_ARN = var.register_with_agent ? var.agent_runtime_endpoint_arn : ""
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/tasks/lambdas/${each.key}.zip"
  src_dir            = "modules/tasks/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = each.key == "tasks_poke" ? 300 : 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_tasks"]
  to   = module.fn["manage_tasks"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["escalate"]
  to   = module.fn["escalate"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["tasks_poke"]
  to   = module.fn["tasks_poke"].aws_lambda_function.this
}


# ─── agent gateway registration ───

locals {
  # one tool per lambda
  tool_schemas = {
    for k in local.functions :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent && fileexists("${path.module}/../lambdas/${k}/schema.json")
  }
  tool_fn     = { for t in keys(local.tool_schemas) : t => t }
  gateway_fns = toset(values(local.tool_fn))
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
        lambda_arn = module.fn[local.tool_fn[each.key]].arn

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
  for_each = local.gateway_fns

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

# The operator issue-collector invokes manage_tasks cross-account (payload carries op=put) to
# land each escalation as one inc task on the operator gerp's books. Role-ARN principal, same posture as the inbox
# module's dispatcher grant.
resource "aws_lambda_permission" "collector_put" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowOperatorIssueCollectorInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["manage_tasks"].name
  principal           = var.issue_collector_role_arn
}

# ─── tasks_poke — the stream wakes an agent when a task needs one ───

# The filter narrows the stream to exactly the poke-worthy rows: a landing escalation
# header (triage), or a header whose assigned_to is present (the lambda confirms it CHANGED —
# filters can't compare images). Everything else — changelog rows, ordinary updates — never
# invokes.
module "tasks_poke_stream" {
  count = var.register_with_agent ? 1 : 0

  source               = "../../terraform/stream"
  name                 = "${local.prefix}-tasks_poke"
  stream_arn           = aws_dynamodb_table.tasks.stream_arn
  function_arn         = module.fn["tasks_poke"].arn
  role_name            = aws_iam_role.lambda.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
  batch_size           = 10
  # a poke is at-most-twice, never a storm: a timeout mid-agent-turn must not re-poke forever
  retries = 1
  filter_patterns = [
    jsonencode({
      eventName = ["INSERT"]
      dynamodb  = { NewImage = { sk = { S = ["HEADER"] }, category = { S = ["escalation", "alarm"] } } }
    }),
    jsonencode({
      eventName = ["MODIFY"]
      dynamodb  = { NewImage = { sk = { S = ["HEADER"] }, assigned_to = { S = [{ exists = true }] } } }
    }),
  ]
}

moved {
  from = aws_lambda_event_source_mapping.tasks_poke[0]
  to   = module.tasks_poke_stream[0].aws_lambda_event_source_mapping.this
}

# ─── outputs ───

output "tasks_table" {
  description = "DDB tasks table name. (task_id, sk): HEADER = current state, <ms>#<id> rows = the changelog."
  value       = aws_dynamodb_table.tasks.name
}

output "tasks_stream_arn" {
  value = aws_dynamodb_table.tasks.stream_arn
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

variable "agent_runtime_endpoint_arn" {
  description = "module.agent.runtime_endpoint_arn — the endpoint a poke invokes."
  type        = string
  default     = ""
}
