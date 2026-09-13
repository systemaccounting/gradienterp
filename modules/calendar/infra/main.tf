variable "gerp_id" {
  description = "logical identifier for the tenant. used as the schedule-group suffix and resource-name suffix."
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

variable "register_with_agent" {
  description = "Register calendar lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "agent_runtime_endpoint_arn" {
  description = "ARN of the customer's AgentCore Runtime endpoint. Used as the default target_arn when target_type=agent_runtime and target_arn is omitted — agent doesn't need to know its own runtime ARN to schedule self-reminders."
  type        = string
  default     = ""
}

variable "schema_table_name" {
  description = "Per-customer schema-registry DDB table. The DDB CRUD lambda (manage_event) validates field names against the calendar_fields registry there at cold start."
  type        = string
  default     = ""
}

locals {
  prefix     = "${var.stack_prefix}-calendar-${replace(var.gerp_id, "_", "-")}"
  group_name = local.prefix
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── EBS Scheduler schedule group ───
#
# Per-tenant namespace for all time-fired things in this customer's stack.
# Both terraform-time (system/static) and runtime (owner/agent-driven)
# schedules live here. Calendar is the per-tenant clock.

resource "aws_scheduler_schedule_group" "calendar" {
  name = local.group_name
}

# ─── DynamoDB — the calendar's record layer ───
#
#   events  — standalone dated commitments (a conference, an appointment): expr + description on a
#             subject's calendar. GSI calendar-index (subject, starts_at) → "my calendar in August".
# EBS firing (above/below) is the actionable subset; this table is the record.
#
# Sellable CAPACITY (the free/busy of a bookable resource) is NOT here — it's a capacity item in
# modules/inventory, metered by an append-only movement log. An event consumes no capacity.

resource "aws_dynamodb_table" "events" {
  name         = "${local.prefix}-events"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "subject"
    type = "S"
  }
  attribute {
    name = "starts_at"
    type = "S"
  }

  global_secondary_index {
    name            = "calendar-index"
    hash_key        = "subject"
    range_key       = "starts_at"
    projection_type = "ALL"
  }
}

# ─── target-invocation role ───
#
# The role EBS assumes to fire each schedule's target. Trust on scheduler.amazonaws.com;
# lambda invoke scoped to the dispatcher alone, plus SNS publish.

resource "aws_iam_role" "scheduler_target" {
  name = "${local.prefix}-target"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_target" {
  name = "${local.prefix}-target"
  role = aws_iam_role.scheduler_target.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # An ALLOWLIST, not a wildcard. This role fires exactly one lambda of its own —
        # agent_dispatcher, the bridge to bedrock-agentcore that EBS cannot invoke directly.
        # `function:*` let anything schedulable through calendar be pointed at any function in
        # the account with any payload on any cadence, which is the shape modules/invoicing and
        # modules/automation already avoid by scoping their scheduler roles to one function each.
        #
        # A module that needs to schedule its own lambda owns its own group and role — which is
        # what every one of them has done, so nothing consumed the wildcard.
        #
        # Not replaceable by a universal target: `aws-sdk:bedrockagentcore:invokeAgentRuntime`
        # validates at CreateSchedule but fails at fire time (TargetErrorCount, nothing reaching
        # the runtime) with the payload blob either raw or base64. The dispatcher stays.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = module.dispatcher.arn
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = "arn:aws:sns:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── agent_dispatcher lambda ───
#
# EBS Scheduler doesn't natively invoke bedrock-agentcore — only lambda / sns /
# sqs / a few other services. This dispatcher is a thin bridge: when a schedule
# with target_type=agent_runtime fires, EBS invokes this lambda, which then
# calls InvokeAgentRuntime on the customer's runtime.
#
# Not exposed as a Gateway tool — purely internal plumbing.

resource "aws_iam_role" "dispatcher" {
  name = "${local.prefix}-dispatcher"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "dispatcher" {
  name = "${local.prefix}-dispatcher"
  role = aws_iam_role.dispatcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:runtime/*",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:runtime-endpoint/*",
        ]
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

module "dispatcher" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-agent_dispatcher"
  role            = aws_iam_role.dispatcher.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/calendar/lambdas/agent_dispatcher.zip"
  src_dir         = "modules/calendar/lambdas/agent_dispatcher"
  gerp_id         = var.gerp_id
  timeout         = 60
  env_vars = {
    AGENT_RUNTIME_ENDPOINT_ARN = var.agent_runtime_endpoint_arn
    GERP_TIMEZONE              = var.timezone
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.dispatcher
  to   = module.dispatcher.aws_lambda_function.this
}


# ─── schedule-lambda execution role (EBS CRUD) ───

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
          "scheduler:CreateSchedule",
          "scheduler:GetSchedule",
          "scheduler:UpdateSchedule",
          "scheduler:DeleteSchedule",
        ]
        Resource = [
          "arn:aws:scheduler:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:schedule/${local.group_name}/*",
          aws_scheduler_schedule_group.calendar.arn,
        ]
      },
      {
        # ListSchedules cannot be scoped to a group. It can list ACROSS groups, so IAM evaluates it
        # against `schedule/*/*` no matter which GroupName the call passes — a grant naming one
        # group matches nothing and the call is refused. Scoped as it was, this tool had never
        # worked; nothing called it, so nothing noticed.
        #
        # The group is still enforced, by the call rather than by IAM: every tool here resolves a
        # GroupName through `_helpers.group_or_error` and passes it, so a listing only ever returns
        # one group's contents.
        Effect   = "Allow"
        Action   = "scheduler:ListSchedules"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.scheduler_target.arn
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

# ─── DDB-lambda execution role (scheduled / availability / events CRUD) ───

resource "aws_iam_role" "crud" {
  name = "${local.prefix}-crud"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "crud" {
  name = "${local.prefix}-crud"
  role = aws_iam_role.crud.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [
          aws_dynamodb_table.events.arn,
          "${aws_dynamodb_table.events.arn}/index/*",
        ]
      },
      {
        # manage_event validates field names against the calendar_fields registry
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── schedule lambda (EBS passthrough; op: create|get|list|update|delete) ───

locals {
  functions = toset([
    "manage_schedule",
  ])

  env_vars = {
    SCHEDULE_GROUP_NAME       = aws_scheduler_schedule_group.calendar.name
    SCHEDULER_TARGET_ROLE_ARN = aws_iam_role.scheduler_target.arn
    AGENT_DISPATCHER_ARN      = module.dispatcher.arn
    CUSTOMER_ID               = var.gerp_id
    GERP_TIMEZONE             = var.timezone                                                     # a schedule with no explicit zone runs on the business's clock
    SETTINGS_TABLE            = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}" # clock reads GERP#timezone; the env var above is the fallback
  }

  crud_env = {
    EVENTS_TABLE = aws_dynamodb_table.events.name
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
  artifact_key       = "modules/calendar/lambdas/${each.key}.zip"
  src_dir            = "modules/calendar/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_schedule"]
  to   = module.fn["manage_schedule"].aws_lambda_function.this
}


# ─── DDB lambda (manage_event: file-list w/ _crud.py) ───

module "manage_event" {
  source = "../../terraform/lambda"

  name               = "${local.prefix}-manage_event"
  role               = aws_iam_role.crud.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/calendar/lambdas/manage_event.zip"
  src_dir            = "modules/calendar/lambdas/manage_event"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.crud_env
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.manage_event
  to   = module.manage_event.aws_lambda_function.this
}


# ─── agent gateway registration (2 tools: manage_schedule + manage_event) ───

locals {
  schedule_tools = var.register_with_agent ? {
    for k in local.functions : k => {
      arn    = module.fn[k].arn
      name   = module.fn[k].name
      schema = jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    } if fileexists("${path.module}/../lambdas/${k}/schema.json")
  } : {}

  ddb_tools = var.register_with_agent ? {
    manage_event = {
      arn    = module.manage_event.arn
      name   = module.manage_event.name
      schema = jsondecode(file("${path.module}/../lambdas/manage_event/schema.json"))
    }
  } : {}

  tools = merge(local.schedule_tools, local.ddb_tools)
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tools

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = replace(each.key, "_", "-")
  description = each.value.schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = each.value.arn

        tool_schema {
          inline_payload {
            name        = each.key
            description = each.value.schema.description
            input_schema {
              type        = each.value.schema.type
              description = each.value.schema.description

              dynamic "property" {
                iterator = prop
                for_each = try(each.value.schema.properties, {})
                content {
                  name        = prop.key
                  type        = try(prop.value.type, "object")
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.schema.required, []), prop.key)

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
  for_each = local.tools

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = each.value.name
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

output "schedule_group_name" {
  description = "EBS Scheduler schedule group for this customer. Both system-static and runtime schedules live here."
  value       = aws_scheduler_schedule_group.calendar.name
}

output "scheduler_target_role_arn" {
  description = "ARN of the role EBS assumes to fire schedule targets. Other modules creating terraform-static schedules in this group reference this."
  value       = aws_iam_role.scheduler_target.arn
}

output "events_table" {
  value = aws_dynamodb_table.events.name
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

variable "timezone" {
  description = "The gerp's IANA timezone, surfaced as GERP_TIMEZONE for `clock.py` — schedules default to the business clock; EBS Scheduler tracks DST when given an IANA name. Default UTC = the behaviour before the clock module existed."
  type        = string
  default     = "UTC"
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
