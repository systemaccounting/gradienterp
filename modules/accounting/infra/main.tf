variable "gerp_id" {
  description = "logical identifier for the tenant this accounting stack belongs to. used as the per-tenant resource-name suffix AND as the SSM lookup key for tenant metadata at /gradienterp/customers/<gerp_id>."
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

variable "sender_email" {
  description = "operator-wide SES verified sender email. not per-tenant — one SES identity serves all customers' outbound mail."
  type        = string
}

variable "chat_base_url" {
  description = "operator-wide base url for agent chat interview links. not per-tenant."
  type        = string
}

variable "reporting_standard" {
  description = "whether compute_balances marks entries as GAAP-standard"
  type        = bool
  default     = true
}

variable "op_event_bus_arn" {
  description = "ARN of the operator's shared EventBridge bus. Default targets the live operator account; per_customer/ may override."
  type        = string
  default     = "arn:aws:events:us-east-1:185369506315:event-bus/gerp-events"
}

variable "plaid_gateway_arn" {
  description = "ARN of the operator's plaid-gateway lambda (prod/platform/operator/plaid_gateway.tf). The reconcile lambda invokes it cross-account carrying only this gerp's access_token. Default targets the live operator account; per_customer/ may override."
  type        = string
  default     = "arn:aws:lambda:us-east-1:185369506315:function:gerp-plaid-gateway"
}

variable "schema_table_name" {
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). post_journal_entry queries it at cold start to load is_account() — rejects fully-classified entries with account names not in the customer's registry."
  type        = string
}

variable "settings_table_name" {
  description = "Settings config table name (modules/settings/infra). post_journal_entry reads GERP#openly_operated from it at cold start to gate publication."
  type        = string
}

variable "settings_table_arn" {
  description = "Settings config table ARN — the dynamodb:GetItem grant for the openly_operated cold-start read."
  type        = string
}

variable "extend_schema_fn_name" {
  description = "Function name of the registries module's extend_schema lambda. add_classification invokes it after writing to the classifications DDB so the new account is also added to the chart_of_accounts registry."
  type        = string
}

variable "extend_schema_fn_arn" {
  description = "Function ARN of the registries module's extend_schema lambda. Used for the lambda:InvokeFunction IAM grant."
  type        = string
}

variable "distribution_fn_name" {
  description = "Name of treasury's distribution handler. compute_balances invokes it on period close (the durable distribution trigger) via the DISTRIBUTION_FN env var. Passed as a constructed string by per_customer (not module.treasury's output) so the accounting<->treasury graph stays acyclic. Empty (default) → compute_balances skips the invoke."
  type        = string
  default     = ""
}

variable "distribution_fn_arn" {
  description = "ARN of treasury's distribution handler — the lambda:InvokeFunction grant for compute_balances. Empty (default) → no grant, no invoke."
  type        = string
  default     = ""
}

variable "server_api_id" {
  description = "Per-customer HTTP API ID from modules/server/infra. Accounting attaches its route integrations here."
  type        = string
}

variable "server_api_execution_arn" {
  description = "Per-customer HTTP API execution ARN root. Source ARN for aws_lambda_permission so the gateway can invoke this module's lambdas."
  type        = string
}

variable "register_with_agent" {
  description = "Register accounting lambdas as MCP tools on the customer's agent gateway. Requires the agent module to have applied first (writes /gradienterp/customers/<id>/agent/* SSM params). Set false for per_customer/ MVP that doesn't yet wire the agent module."
  type        = bool
  default     = true
}

# Timestream retention vars removed — ledger moved to DDB (AWS closed Timestream
# for LiveAnalytics to new accounts in early 2026). DDB has no retention knob;
# archival-to-S3 is planned as a separate layer per the "audit as a diff" thesis.

# ─── tenant metadata (SSM lookup, operator-seeded at onboard time) ───
#
# Operator provisions per-tenant config via `aws ssm put-parameter` against
# /gradienterp/customers/<gerp_id> BEFORE `terraform apply` on this
# module. The blob is a JSON object the module decodes into `local.customer`.
# Keys this module reads: `owner_email`, `reporting_schedule`.

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}"
}

locals {
  customer = jsondecode(data.aws_ssm_parameter.customer.value)
  prefix   = "${var.stack_prefix}-accounting-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (journal / the ledger) ───
# AWS closed Timestream for LiveAnalytics to new accounts in early 2026. DDB
# covers our access patterns and is truly pay-per-use (vs InfluxDB's always-on
# instance pricing, which breaks our per-tenant cost story).
#
# Schema:
#   pk = "<YYYY-MM>"                       month bucket; keeps each partition small
#                                           and matches how most queries are framed
#                                           ("this month's P&L")
#   sk = "<timestamp_ms>#<entry_id>#<i>"   sortable by time within the month; `i`
#                                           disambiguates pair rows from the same
#                                           N-leg entry
#
# Immutability: ConditionExpression "attribute_not_exists(pk)" on the FIRST pair
# row (i=0) of every entry. Duplicate entry_id submission fails cleanly.

resource "aws_dynamodb_table" "ledger" {
  name         = "${local.prefix}-ledger"
  tags         = { "gerp:layer" = "books" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # Streams gets us append-only audit + downstream event fan-out (public stream
  # publisher, cross-customer dispatcher) without polling.
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"
}

# ─── dynamodb (pending queue) ───

resource "aws_dynamodb_table" "pending" {
  name         = "${local.prefix}-pending"
  tags         = { "gerp:layer" = "books" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "entry_id"

  attribute {
    name = "entry_id"
    type = "S"
  }
}

# ─── dynamodb (computed balances) ───

resource "aws_dynamodb_table" "balances" {
  name         = "${local.prefix}-balances"
  tags         = { "gerp:layer" = "books" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "period_end"
  range_key    = "account_id"

  attribute {
    name = "period_end"
    type = "S"
  }

  attribute {
    name = "account_id"
    type = "S"
  }
}

# ─── s3 (report csvs) ───

resource "aws_s3_bucket" "report" {
  bucket        = "${local.prefix}-report"
  force_destroy = true # generated CSV statements; regenerated by generate_report, safe to drop on replace/teardown
  tags          = { "gerp:layer" = "books" }
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
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
        ]
        Resource = aws_dynamodb_table.pending.arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchWriteItem",
        ]
        Resource = aws_dynamodb_table.balances.arn
      },
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction",
        ]
        Resource = concat([
          "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-post_journal_entry",
          "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-get_statement",
          var.extend_schema_fn_arn, # add_classification invokes extend_schema
          var.plaid_gateway_arn,    # reconcile invokes the operator plaid-gateway cross-account
          # compute_balances invokes treasury's distribution handler on period close (by
          # constructed name, so no accounting<->treasury module cycle)
        ], var.distribution_fn_arn != "" ? [var.distribution_fn_arn] : [])
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.report.arn}/*"
      },
      {
        # get_aws_cost: this account's own bill, by service. Cost Explorer has no resource-level
        # scoping; the linked-account view is what the organization grants
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
        ]
        Resource = "*"
      },
      {
        # the ledger: reads + conditional-append writes on the per-month partitions
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:BatchGetItem",
        ]
        Resource = aws_dynamodb_table.ledger.arn
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
      {
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.op_event_bus_arn
      },
      {
        # post_journal_entry reads GERP#openly_operated from the settings config table at cold start.
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = var.settings_table_arn
      },
      {
        # is_account() in post_journal_entry queries the customer's registry
        # DDB at cold start.
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
      },
      {
        # the plaid access_token (a SecureString derived secret): reconcile reads it, check_bank_connection
        # writes it on a linked session.
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/secrets/plaid/*"
      },
      {
        # accounting-owned plaid state (not secrets): the sync cursor (reconcile) + the pending Hosted
        # Link token (connect_bank writes, check_bank_connection reads/deletes).
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/accounting/plaid_*"
      },
      {
        # encrypt/decrypt SecureString params (account-default aws/ssm key; gated by its key policy)
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
        Resource = "*"
      },
    ]
  })
}

# ─── lambdas ───

locals {
  functions = {
    post_journal_entry = "post_journal_entry"
    get_statement      = "get_statement"
    report_pending     = "report_pending"
    classify_pending   = "classify_pending"
    add_classification = "add_classification"
    reconcile          = "reconcile"
    connect_bank       = "connect_bank"
    get_aws_cost       = "get_aws_cost"
  }

  # lambdas that bundle sibling helper modules alongside main.py (reconcile imports the pure
  # reconcile.py). Keyed by function; each value is the list of extra files in the lambda's dir.
  extra_sources = {
    reconcile = ["reconcile.py"]
    # each statement's body, moved in unchanged from the tool that used to carry it
    get_statement = ["get_trial_balance.py", "get_income_statement.py", "get_balance_sheet.py",
    "compute_balances.py", "generate_report.py"]
  }

  timeouts = {
    get_statement = 120 # the balances integration is the long pole
  }

  env_vars = {
    PENDING_TABLE         = aws_dynamodb_table.pending.name
    GERP_TIMEZONE         = var.timezone # the business's clock (modules/clock)
    BALANCES_TABLE        = aws_dynamodb_table.balances.name
    LEDGER_TABLE          = aws_dynamodb_table.ledger.name
    REPORT_BUCKET         = aws_s3_bucket.report.id
    OWNER_EMAIL           = local.customer.owner_email
    SENDER_EMAIL          = var.sender_email
    CHAT_BASE_URL         = var.chat_base_url
    POST_JOURNAL_ENTRY_FN = "${local.prefix}-post_journal_entry"
    GENERATE_REPORT_FN    = "${local.prefix}-get_statement" # the suite writer lives there; a periodEnd-only payload routes to it
    DISTRIBUTION_FN       = var.distribution_fn_name        # treasury's distribution handler; "" → compute_balances skips it
    REPORTING_STANDARD    = tostring(var.reporting_standard)
    CUSTOMER_ID           = var.gerp_id
    OP_EVENT_BUS_ARN      = var.op_event_bus_arn
    SCHEMA_TABLE          = var.schema_table_name
    SETTINGS_TABLE        = var.settings_table_name
    EXTEND_SCHEMA_FN      = var.extend_schema_fn_name
    # reconcile: the operator gateway it pulls through, + where this gerp's access_token / sync cursor live
    PLAID_GATEWAY_ARN        = var.plaid_gateway_arn
    PLAID_ACCESS_TOKEN_PARAM = "/gradienterp/customers/${var.gerp_id}/secrets/plaid/access_token"
    PLAID_CURSOR_PARAM       = "/gradienterp/customers/${var.gerp_id}/accounting/plaid_cursor"
    PLAID_PENDING_LINK_PARAM = "/gradienterp/customers/${var.gerp_id}/accounting/plaid_pending_link"
  }
}

# Lambda zips contain only main.py. The chart of accounts is sourced at runtime:
# (a) the agent reads the customer's registry DDB at boot for prompt assembly,
# and (b) post_journal_entry calls is_account() at cold start to enforce known-
# account names on fully-classified entries.
module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/accounting/lambdas/${each.key}.zip"
  src_dir            = "modules/accounting/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = lookup(local.timeouts, each.key, 30)
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["post_journal_entry"]
  to   = module.fn["post_journal_entry"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["get_statement"]
  to   = module.fn["get_statement"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["report_pending"]
  to   = module.fn["report_pending"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["classify_pending"]
  to   = module.fn["classify_pending"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["add_classification"]
  to   = module.fn["add_classification"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["reconcile"]
  to   = module.fn["reconcile"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["connect_bank"]
  to   = module.fn["connect_bank"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["get_aws_cost"]
  to   = module.fn["get_aws_cost"].aws_lambda_function.this
}


# ─── HTTP API routes (modules/server) ───
#
# Accounting attaches no route to the per-customer HTTP API: `post_journal_entry` is invoked by
# the modules that post, and `POST /journal` was taken off the internet (it had no authorizer).
# See modules/server/AGENTS.md for the route catalog.

# ─── eventbridge cron (weekly report) ───

resource "aws_cloudwatch_event_rule" "report_pending" {
  name                = "${local.prefix}-report-pending"
  schedule_expression = "rate(7 days)"
}

resource "aws_cloudwatch_event_target" "report_pending" {
  rule = aws_cloudwatch_event_rule.report_pending.name
  arn  = module.fn["report_pending"].arn
}

resource "aws_lambda_permission" "report_pending" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  action        = "lambda:InvokeFunction"
  function_name = module.fn["report_pending"].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.report_pending.arn
}

# ─── eventbridge cron (reporting schedule) ───

resource "aws_cloudwatch_event_rule" "compute_balances" {
  name                = "${local.prefix}-compute-balances"
  schedule_expression = local.customer.reporting_schedule
}

resource "aws_cloudwatch_event_target" "compute_balances" {
  rule = aws_cloudwatch_event_rule.compute_balances.name
  arn  = module.fn["get_statement"].arn

  input = jsonencode({
    statement = "balances"
  })
}

resource "aws_lambda_permission" "compute_balances" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  action        = "lambda:InvokeFunction"
  function_name = module.fn["get_statement"].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.compute_balances.arn
}

resource "aws_cloudwatch_event_rule" "generate_report" {
  name                = "${local.prefix}-generate-report"
  description         = "Fire generate_report on the owner's reporting schedule (writes the standard statement suite to S3)."
  schedule_expression = local.customer.reporting_schedule
}

resource "aws_cloudwatch_event_target" "generate_report" {
  rule      = aws_cloudwatch_event_rule.generate_report.name
  target_id = "generate_report"
  arn       = module.fn["get_statement"].arn

  input = jsonencode({
    periodEnd = "$$.scheduledTime"
  })
}

resource "aws_lambda_permission" "generate_report_cron" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  action        = "lambda:InvokeFunction"
  function_name = module.fn["get_statement"].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.generate_report.arn
}

# ─── eventbridge cron (daily reconcile backstop) ───
#
# The gateway's SYNC_UPDATES_AVAILABLE webhook poke is the low-latency path; this daily rule
# guarantees a pull even if no webhook fires. No-ops until a bank is linked (reconcile returns
# early when there's no access_token).

resource "aws_cloudwatch_event_rule" "reconcile" {
  name                = "${local.prefix}-reconcile"
  description         = "Daily bank-feed reconciliation backstop for this gerp."
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "reconcile" {
  rule = aws_cloudwatch_event_rule.reconcile.name
  arn  = module.fn["reconcile"].arn
}

resource "aws_lambda_permission" "reconcile_cron" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  action        = "lambda:InvokeFunction"
  function_name = module.fn["reconcile"].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconcile.arn
}

# the operator plaid-gateway pokes reconcile cross-account on a verified SYNC_UPDATES_AVAILABLE webhook
# (account derived from the gateway ARN; role name is the gateway's fixed convention).
resource "aws_lambda_permission" "gateway_poke_reconcile" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowPlaidGatewayPoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["reconcile"].name
  principal           = "arn:aws:iam::${split(":", var.plaid_gateway_arn)[4]}:role/${var.stack_prefix}-plaid-gateway"
}

# ─── agent gateway registration ───
#
# Each lambda with a sibling schema.json registers as an MCP tool against the
# agent's gateway. Gateway is provisioned by modules/agent/infra; we discover it
# via SSM (operator-hosted invariant: agent applies first, domains plug in
# after). Schema files are the single source of truth — same artifact eventually
# consumed by handler-time validation in main.py.

locals {
  tool_schemas = {
    for k in keys(local.functions) :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent && fileexists("${path.module}/../lambdas/${k}/schema.json")
  }

  # Agent-facing tool-name overrides (default = the function key). The pending lister
  # reads clearer to the model as `list_pending_entries` and matches the prompt + dev
  # harness, which already use that name. The lambda dir / function stays report_pending.
  tool_names = {
    report_pending = "list_pending_entries"
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
  # AgentCore registration name: hyphens only, no underscores. The agent-facing
  # tool name stays snake_case via inline_payload.name (preserves prompts).
  name        = replace(each.key, "_", "-")
  description = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.fn[each.key].arn

        tool_schema {
          inline_payload {
            name        = lookup(local.tool_names, each.key, each.key)
            description = each.value.description
            input_schema {
              type        = each.value.type
              description = each.value.description

              # Top-level properties. For arrays / objects, nest `items` /
              # `property` blocks one level deep — covers our schemas
              # (lineItems[].account, range.start). Deeper nesting could fall
              # back to `items_json` / `properties_json` (provider exposes
              # those at the level-2 nesting), not needed yet.
              dynamic "property" {
                iterator = prop
                for_each = try(each.value.properties, {})
                content {
                  name        = prop.key
                  type        = prop.value.type
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.required, []), prop.key)

                  # array property → describe its items
                  dynamic "items" {
                    for_each = prop.value.type == "array" ? [prop.value.items] : []
                    content {
                      type        = items.value.type
                      description = try(items.value.description, "")
                      # array of objects → describe each field
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

                  # object property → describe its fields
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
    # Gateway invokes the lambda under its own configured IAM role (the agent
    # module's gateway_role_arn). No per-owner OAuth — same-account dispatch.
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

output "ledger_table" {
  description = "DDB journal/ledger table name. hash pk = YYYY-MM month bucket, range sk = timestamp_ms#entry_id#pair_index."
  value       = aws_dynamodb_table.ledger.name
}

output "balances_table" {
  value = aws_dynamodb_table.balances.name
}

output "report_bucket" {
  value = aws_s3_bucket.report.id
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "lambda_arns" {
  description = "map of lambda logical name → ARN. agent/infra consumes this as `accounting_lambda_arns`; inventory/infra consumes the post_journal_entry entry."
  value       = { for k, fn in module.fn : k => fn.arn }
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "timezone" {
  description = "The gerp's IANA timezone, surfaced as GERP_TIMEZONE for `clock.py` — civil period boundaries resolve on the business calendar. Default UTC = the behaviour before the clock module existed."
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
