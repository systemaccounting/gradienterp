###############################################
# Per-customer registry infra: DDB table + agent tool lambdas.
#
# Stack config (chart_of_accounts, contact_fields, calendar_fields, future)
# lives in this DDB table — the single runtime source of truth for the
# customer's registry. Validators in domain modules (accounting, contacts,
# calendar) query the table at lambda cold start, cache in module globals,
# zero runtime DDB on warm invocations.
#
# Updates flow two paths, both agent-mediated:
#   - extension (immediate): customer agent calls write_schema (op=extend) →
#     row written with origin='extension' + event emitted on operator bus
#   - canonical pull (weekly): EventBridge Scheduler fires a prompt at the
#     customer's bedrock runtime → agent reads operator's canonical S3,
#     diffs against local DDB, surfaces to owner, merges approved entries
#     via write_schema (op=merge) with origin='canonical'
#
# At provisioning, a seed lambda runs once per customer to bulk-write the
# operator's current canonical baseline into this DDB. Idempotent on re-apply.
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
  description = "Logical identifier for the tenant. Used as the per-customer DDB table suffix and SSM lookup key for tenant metadata."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix for resources this module owns (e.g. `gerp` → `gerp-schema-<id>`). Single source of truth is repo-root `config.json` (`STACK_PREFIX`), threaded by the composition root. Default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "op_event_bus_arn" {
  description = "ARN of the operator's shared EventBridge bus. Default targets the live operator account; per_customer/ may override."
  type        = string
  default     = "arn:aws:events:us-east-1:185369506315:event-bus/gerp-events"
}

variable "canonical_bucket" {
  description = "Operator-account S3 bucket holding the canonical registry JSON files (org-scoped read). read_schema (source=canonical) + seed_schema pull from here."
  type        = string
  default     = "gerp-canonical-185369506315"
}

variable "register_with_agent" {
  description = "Register registry lambdas as MCP tools on the customer's agent gateway. Requires the agent module to have applied first (writes /gradienterp/customers/<id>/agent/* SSM params). Set false for per_customer/ MVP that doesn't yet wire the agent module."
  type        = bool
  default     = true
}

variable "agent_runtime_endpoint_arn" {
  description = "ARN of the customer's AgentCore Runtime Endpoint (from modules/agent/infra output). canonical_pull_invoke lambda invokes this on its weekly cron."
  type        = string
  default     = ""
}

variable "enable_canonical_pull" {
  description = "Whether to provision the weekly canonical-pull cron (EventBridge Scheduler + invoke lambda). Must be statically known at plan time; cannot derive from `agent_runtime_endpoint_arn` (which is apply-time-computed when wired through per_customer/). Set true when the agent module is wired and agent_runtime_endpoint_arn is non-empty."
  type        = bool
  default     = false
}

variable "rules_params_table_name" {
  description = "Name of the modules/rules rules-params DDB table. seed_schema also seeds the GENERAL canonical rule params (tax tables + rates) into it from rule_params.json — one canonical seed for all per-customer reference data, not a seed lambda per module."
  type        = string
}

variable "settings_table_name" {
  description = "Settings config table name (modules/settings/infra). write_schema (op=extend) reads GERP#openly_operated from it at cold start to gate publication."
  type        = string
}

variable "settings_table_arn" {
  description = "Settings config table ARN — the dynamodb:GetItem grant for the openly_operated cold-start read."
  type        = string
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}"
}

locals {
  customer = jsondecode(data.aws_ssm_parameter.customer.value)
  prefix   = "${var.stack_prefix}-schemas-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (per-customer registry) ───
#
# Single substrate for chart_of_accounts, contact_fields, calendar_fields,
# and future schemas. Partition by schema, sort by composite bucket#name.
# Schema:
#   pk = "<registry>"            e.g. "chart_of_accounts"
#   sk = "<bucket>#<name>"       e.g. "asset#TIPS_REVENUE"
# Attributes: bucket, name, schema (any — bool for accounts, object for fields),
# origin ("canonical" | "extension"), reason, created_at, created_by.

resource "aws_dynamodb_table" "registry" {
  name         = "${var.stack_prefix}-schema-${replace(var.gerp_id, "_", "-")}"
  tags         = { "gerp:layer" = "config" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "registry"
  range_key    = "bucket_name"

  attribute {
    name = "registry"
    type = "S"
  }

  attribute {
    name = "bucket_name"
    type = "S"
  }
}

# ─── lambdas (agent tools) ───

locals {
  functions = {
    read_schema           = "read_schema"
    write_schema          = "write_schema"
    seed_schema           = "seed_schema"
    canonical_pull_invoke = "canonical_pull_invoke"
  }

  env_vars = {
    SCHEMA_TABLE               = aws_dynamodb_table.registry.name
    RULES_PARAMS_TABLE         = var.rules_params_table_name
    SETTINGS_TABLE             = var.settings_table_name
    OP_EVENT_BUS_ARN           = var.op_event_bus_arn
    CUSTOMER_ID                = var.gerp_id
    CANONICAL_BUCKET           = var.canonical_bucket
    AGENT_RUNTIME_ENDPOINT_ARN = var.agent_runtime_endpoint_arn
  }

  # Weekly canonical-pull cron is enabled by the caller via enable_canonical_pull.
  # Can't derive from agent_runtime_endpoint_arn — that's apply-time-computed
  # when wired through per_customer/, which breaks `count = ... ? 1 : 0`.
  canonical_pull_enabled = var.enable_canonical_pull
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/schemas/lambdas/${each.key}.zip"
  src_dir            = "modules/schemas/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 60
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["read_schema"]
  to   = module.fn["read_schema"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["write_schema"]
  to   = module.fn["write_schema"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["seed_schema"]
  to   = module.fn["seed_schema"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["canonical_pull_invoke"]
  to   = module.fn["canonical_pull_invoke"].aws_lambda_function.this
}


# ─── IAM ───

resource "aws_iam_role" "lambda" {
  name = "${local.prefix}-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
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
        Sid    = "RegistryDdbReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchWriteItem",
        ]
        Resource = aws_dynamodb_table.registry.arn
      },
      {
        # seed_schema also seeds the GENERAL canonical rule params into the rules-params table
        Sid      = "RulesParamsSeedWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:BatchWriteItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.rules_params_table_name}"
      },
      {
        Sid    = "CanonicalBucketRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.canonical_bucket}",
          "arn:aws:s3:::${var.canonical_bucket}/*",
        ]
      },
      {
        Sid      = "OpenlyOperatedRead"
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = var.settings_table_arn
      },
      {
        Sid      = "PutEventsOnOperatorBus"
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.op_event_bus_arn
      },
      {
        # canonical_pull_invoke fires the agent runtime once a week. Wildcarded
        # on action because the AgentCore Runtime invoke action name is still
        # in flux across provider versions; tighten when the action stabilizes.
        Sid    = "InvokeAgentRuntime"
        Effect = "Allow"
        Action = "bedrock-agentcore:*"
        # BOTH arns: a qualified invoke authorizes against the runtime AND its endpoint, so an
        # endpoint-only grant is denied even when the call is correctly formed.
        Resource = var.agent_runtime_endpoint_arn == "" ? ["*"] : [
          var.agent_runtime_endpoint_arn,
          replace(var.agent_runtime_endpoint_arn, "/(/runtime-endpoint/.*)$/", ""),
        ]
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── provisioning-time seed (one-time per customer) ───
#
# Invokes seed_schema lambda once per customer with input = gerp_id.
# aws_lambda_invocation resource lifecycle = recreates only when input
# changes; gerp_id is stable, so this runs once at first apply. Lambda
# itself is idempotent (skips if any canonical rows already exist).

resource "aws_lambda_invocation" "seed_canonical" {
  function_name = module.fn["seed_schema"].name
  input = jsonencode({
    gerp_id = var.gerp_id
  })

  depends_on = [
    aws_iam_role_policy.lambda,
  ]
}

# ─── agent gateway registration (matches modules/accounting/infra pattern) ───

locals {
  # seed_schema and canonical_pull_invoke aren't agent tools — exclude from
  # gateway registration. seed runs once at provisioning; canonical_pull_invoke
  # is an EventBridge cron target.
  internal_functions = ["seed_schema", "canonical_pull_invoke"]

  agent_tool_functions = {
    for k, v in local.functions : k => v if !contains(local.internal_functions, k)
  }

  tool_schemas = {
    for k in keys(local.agent_tool_functions) :
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

# ─── weekly canonical-pull cron (EventBridge Scheduler) ───
#
# Fires canonical_pull_invoke lambda once a week. Lambda invokes the customer's
# AgentCore Runtime endpoint with a prompt to diff canonical vs local registry,
# surface diffs to the owner, and merge approved entries on owner approval.
# Disabled (count=0) when no agent runtime endpoint is provided.
#
# EventBridge Scheduler over EventBridge Rules: purpose-built scheduling
# primitive (flexible time windows, retries, DLQ, universal targets, time zones).
# Scheduler-as-target-of-pattern-matching belongs in EB Rules; pure schedules
# belong here.

resource "aws_scheduler_schedule_group" "canonical_pull" {
  count = local.canonical_pull_enabled ? 1 : 0
  name  = "${local.prefix}-canonical-pull"
}

resource "aws_iam_role" "scheduler" {
  count = local.canonical_pull_enabled ? 1 : 0
  name  = "${local.prefix}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  count = local.canonical_pull_enabled ? 1 : 0
  name  = "invoke-canonical-pull"
  role  = aws_iam_role.scheduler[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "lambda:InvokeFunction"
      Resource = [
        module.fn["canonical_pull_invoke"].arn,
        module.fn["seed_schema"].arn, # weekly rule-params re-seed
      ]
    }]
  })
}

resource "aws_scheduler_schedule" "canonical_pull" {
  count = local.canonical_pull_enabled ? 1 : 0

  name       = "${local.prefix}-canonical-pull"
  group_name = aws_scheduler_schedule_group.canonical_pull[0].name

  schedule_expression          = "rate(7 days)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF" # exact-time fire; canonical pull isn't latency-sensitive but
    # we don't need a window either
  }

  target {
    arn      = module.fn["canonical_pull_invoke"].arn
    role_arn = aws_iam_role.scheduler[0].arn
  }
}

# Weekly rule-params re-seed. seed_schema is append-only per (rule, effective_from), so this
# is a no-op until a new tax year lands in canonical S3 — then it appends the new rows to
# every tenant automatically (platform tables need no owner approval, unlike the schema pull).
resource "aws_scheduler_schedule" "rule_params_seed" {
  count = local.canonical_pull_enabled ? 1 : 0

  name       = "${local.prefix}-rule-params-seed"
  group_name = aws_scheduler_schedule_group.canonical_pull[0].name

  schedule_expression          = "rate(7 days)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = module.fn["seed_schema"].arn
    role_arn = aws_iam_role.scheduler[0].arn
    input    = jsonencode({ gerp_id = var.gerp_id })
  }
}

# ─── outputs ───

output "schema_table_name" {
  value       = aws_dynamodb_table.registry.name
  description = "Per-customer registry DDB table. Read by domain validators (accounting, contacts, calendar) at cold start."
}

output "schema_table_arn" {
  value = aws_dynamodb_table.registry.arn
}

# the names are the external contract (accounting's add_classification wiring); the lambda
# behind them is write_schema and its payload needs `op: extend`
output "extend_schema_fn_arn" {
  value = module.fn["write_schema"].arn
}

output "extend_schema_fn_name" {
  value = module.fn["write_schema"].name
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
