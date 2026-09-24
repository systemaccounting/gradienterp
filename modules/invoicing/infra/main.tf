# invoicing — the sell side (the AR lifecycle).
#
# First cut: the direct, non-agentic flow — manage_invoice (create) -> issue_invoice -> record_invoice_paid
# (+ manage_invoice's get). An invoice is the billing record + the AR lifecycle (draft -> issued ->
# paid); issue/payment post journal entries against accounting. issue_invoice folds in
# a `multiply_item_value` rule instance keyed on the item (DR AR / CR SALES_TAX_PAYABLE) when a line is taxable.
# The cross-firm case (the invoice arriving as the counterparty's PO over the bus) is the
# deferred wiring layer. Sell-side mirror of modules/purchasing.

locals {
  prefix = "${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb ───

resource "aws_dynamodb_table" "invoices" {
  name         = "${local.prefix}-invoices"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "invoice_id"

  attribute {
    name = "invoice_id"
    type = "S"
  }
  # An improvised ticket lands here as a draft with holes; on_incomplete_draft fires on that edge
  # and wakes the agent to make it postable. OLD_AND_NEW because the consumer must distinguish
  # "became unresolved" from "was already unresolved" — otherwise every subsequent write to the same
  # draft is another agent turn.
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

}

# transitions — the transaction object's grain. Each ITEM on an invoice owns an append-only stream
# of timestamped state-transition rows; an item's current state is the fold (its latest row), and
# "the invoice" is the fold across items — never itself stateful. That's what lets one seat be
# `earned` while a sibling is `refunded` on one invoice, with no credit memo.
#   pk = invoice_id                        → one query returns the whole object's history
#   sk = "{item_id}#{at}#{transition_id}"  → item-major, then chronological
# An invoice's LINES, as range rows. `sk = item#<item>#range#<start>`, so a quantity is stated
# rather than enumerated: fifty units with a modifier on the first ten is three rows, not fifty.
# The range key is also the line's IDENTITY — what `transition_item` hangs per-unit state off — so
# ordinals never renumber in flight; voiding unit 3 of 10 leaves a gap and 4-10 keep their numbers.
#
# The GSI is the query the ledger cannot answer cheaply: one product across time. "Which menu item
# is a loss-leader" and "who sells the most per hour" are both item-across-invoices, and without it
# they mean reading a CSV export instead of the books.
resource "aws_dynamodb_table" "invoice_lines" {
  name         = "${local.prefix}-invoice-lines"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "invoice_id"
  range_key    = "sk"

  attribute {
    name = "invoice_id"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi_item"
    type = "S"
  }

  attribute {
    name = "gsi_tag"
    type = "S"
  }
  attribute {
    name = "gsi_sk"
    type = "S"
  }

  global_secondary_index {
    name = "item-index"
    key_schema {
      attribute_name = "gsi_item"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "gsi_sk"
      key_type       = "RANGE"
    }
    projection_type = "ALL" # the ranking queries are the point; a KEYS_ONLY hit costs a second read
  }

  # TAGS live in this table too, as `tag#<tag>` rows beside the `item#…` ones — a set attribute on
  # the invoice could not be a GSI key, and "every invoice tagged X" has to be a Query rather than a
  # scan. `get_invoice` already queries this table for its lines, so tags cost no extra read.
  global_secondary_index {
    name = "tag-index"
    key_schema {
      attribute_name = "gsi_tag"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "gsi_sk"
      key_type       = "RANGE"
    }
    projection_type = "KEYS_ONLY" # the answer is which invoices; the caller reads the ones it wants
  }
}

resource "aws_dynamodb_table" "transitions" {
  name         = "${local.prefix}-transitions"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "invoice_id"
  range_key    = "tx_sk"

  attribute {
    name = "invoice_id"
    type = "S"
  }
  attribute {
    name = "tx_sk"
    type = "S"
  }
}

# agreements — the sell-side agreement commit (this firm always the seller on these rows).
# Same shared shape as purchasing's (the modules/agreements substrate); the settle effect is
# a draft invoice instead of an open PO. Single-role table — no merge with the buy side.
resource "aws_dynamodb_table" "agreements" {
  name         = "${local.prefix}-agreements"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread"
  range_key    = "terms_hash"

  attribute {
    name = "thread"
    type = "S"
  }
  attribute {
    name = "terms_hash"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"
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
        # the settings rows the clock and `events.publish` read: `GERP#timezone`, `GERP#openly_operated`
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          # removing a TAG deletes its row. Nothing else here deletes — an invoice and its lines are
          # corrected by rewrite, and a tag is the one thing meant to be taken back off.
          "dynamodb:DeleteItem",
        ]
        Resource = [
          aws_dynamodb_table.invoices.arn,
          aws_dynamodb_table.invoice_lines.arn,
          # the index arn is NOT covered by the table arn — a policy granting Query on a table does
          # not grant it on that table's GSIs
          "${aws_dynamodb_table.invoice_lines.arn}/index/*",
        ]
      },
      {
        # transition_item appends to an item's stream, and Query's the invoice's history back to fold it
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.transitions.arn
      },
      {
        # create_from_template: the template is a rule INSTANCE (`INVOICE_TEMPLATE#<name>`, queried from the
        # instance table alongside every other rule) whose emitted keys resolve against inventory's
        # catalog (GetItem the item def — name / rate / revenue_account).
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.items_table_name}"
      },
      {
        # a tag must be declared before it can be applied — read-only, the vocabulary is written by
        # modules/schemas
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
      },
      {
        # building an invoice queries the instances attached to each inventory key it is selling
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}"
      },
      {
        # accept_po stamps the agreement row
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.agreements.arn
      },
      {
        # an addressed event goes to the RECIPIENT'S hub (modules/events): its row in the platform
        # directory names the hub's bus, and every hub's bus is `gerp-events` by name
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = "arn:aws:events:*:*:event-bus/${var.stack_prefix}-events"
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = var.directory_table_arn
      },
      {
        # a firm's INVOICE#<status> rule announces on the firm's OWN bus, for modules in this firm.
        # Two grants rather than one wildcard: the two buses are different blast radii, and a module
        # that only talks to itself should get AccessDenied from the shared one.
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.internal_bus_arn
      },
      {
        # issue/payment post the invoice's journal entries
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

# ─── lambdas (agent tools — and the non-agentic interface: the owner bills customers directly) ───

locals {
  functions = toset([
    "manage_invoice",
    "issue_invoice",
    "record_invoice_paid",
  ])

  # rule libs bundled at a lambda's zip root (lib name → path relative to path.module). A lambda that
  # runs the engine takes rules.py, instances.py (the attachments ARE the dispatch), and whichever
  # rule lib holds the rules those instances name: manage_invoice's create ops derive a tax as another ITEM off
  # the rules attached to what's being sold (general_rules); its transition op moves an item's money off
  # the rules attached to its kind + the state it enters (transition_rules). issue_invoice runs neither —
  # a tax is already an item by then, so it just credits each item's own account.
  rule_libs = {
    manage_invoice = {
      rules            = "../../rules/rules.py"
      general_rules    = "../../rules/general_rules.py"
      instances        = "../../rules/instances.py"
      template_rules   = "../template_rules.py"
      transition_rules = "../transition_rules.py"
    }
  }

  env_vars = {
    INVOICES_TABLE        = aws_dynamodb_table.invoices.name
    INVOICE_LINES_TABLE   = aws_dynamodb_table.invoice_lines.name
    GERP_TIMEZONE         = var.timezone                                                     # the business's clock (modules/clock)
    SETTINGS_TABLE        = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}" # the clock's `GERP#timezone`, and `publish`'s `GERP#openly_operated`
    TRANSITIONS_TABLE     = aws_dynamodb_table.transitions.name
    AGREEMENTS_TABLE      = aws_dynamodb_table.agreements.name
    RULE_INSTANCES_TABLE  = var.rule_instances_table_name # the rules ATTACHED to what's being sold (a tax, a fee)
    SCHEMA_TABLE          = var.schema_table_name         # the invoice_tags vocabulary a tag must be declared in
    ITEMS_TABLE           = var.items_table_name          # inventory's catalog — the template's keys resolve here
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    GERP_ID               = var.gerp_id          # the `from` on addressed events
    OP_EVENT_BUS_ARN      = var.op_event_bus_arn # accept_po emits po.accepted here
    DIRECTORY_TABLE_ARN   = var.directory_table_arn
    HUB_ID                = var.hub_id
    INTERNAL_BUS_NAME     = var.internal_bus_name                             # where a firm's transition rules announce
    OWNER_SUB_PARAM       = "/gradienterp/customers/${var.gerp_id}/owner_sub" # GET /invoices answers only this sub
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/invoicing/lambdas/${each.key}.zip"
  src_dir            = "modules/invoicing/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_invoice"]
  to   = module.fn["manage_invoice"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["issue_invoice"]
  to   = module.fn["issue_invoice"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["record_invoice_paid"]
  to   = module.fn["record_invoice_paid"].aws_lambda_function.this
}


# ─── agent gateway registration (same pattern as modules/purchasing / labor / tasks) ───

locals {
  # target-only tools: the schema (the agent's surface) lives here with the domain; the lambda is
  # the shared agreements service. Its invoke permission is granted in modules/agreements.
  tool_only = var.agreements_tools ? { accept_po = var.agreements_accept_fn_arn, decline_po = var.agreements_decline_fn_arn } : {}

  tool_schemas = {
    for k in setunion(local.functions, keys(local.tool_only)) :
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
        lambda_arn = lookup(local.tool_only, each.key, null) != null ? local.tool_only[each.key] : module.fn[each.key].arn

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
  for_each = { for k, v in local.tool_schemas : k => v if !contains(keys(local.tool_only), k) }

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

# ─── settle_agreement: the sell side's settle EFFECT (invoked by the shared agreements settle) ───

resource "aws_iam_role" "settle" {
  name = "${local.prefix}-settle"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "settle" {
  name = "${local.prefix}-settle"
  role = aws_iam_role.settle.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "dynamodb:PutItem"
        Resource = [
          aws_dynamodb_table.invoices.arn,
          aws_dynamodb_table.invoice_lines.arn,
          # the index arn is NOT covered by the table arn — a policy granting Query on a table does
          # not grant it on that table's GSIs
          "${aws_dynamodb_table.invoice_lines.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "settle_agreement" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-settle_agreement"
  role            = aws_iam_role.settle.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/invoicing/lambdas/settle_agreement.zip"
  src_dir         = "modules/invoicing/lambdas/settle_agreement"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    INVOICES_TABLE      = aws_dynamodb_table.invoices.name
    INVOICE_LINES_TABLE = aws_dynamodb_table.invoice_lines.name
    AGREEMENTS_TABLE    = aws_dynamodb_table.agreements.name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.settle_agreement
  to   = module.settle_agreement.aws_lambda_function.this
}


# A charge that did not land. Not gateway-registered: `charge_saved_method` is the caller, and an
# owner writing off an invoice by hand is a different act that does not exist yet. Takes the shared
# lambda role and env because it runs the transition rules — `INVOICE_STATUS#unpaid` is where a firm
# attaches its chase, so this needs whatever those rules reach.
module "mark_unpaid" {
  source = "../../terraform/lambda"

  name               = "${local.prefix}-mark_unpaid"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/invoicing/lambdas/mark_unpaid.zip"
  src_dir            = "modules/invoicing/lambdas/mark_unpaid"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.mark_unpaid
  to   = module.mark_unpaid.aws_lambda_function.this
}


resource "aws_iam_role" "on_incomplete_draft" {
  name = "${local.prefix}-on_incomplete_draft"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "on_incomplete_draft" {
  name = "${local.prefix}-on_incomplete_draft"
  role = aws_iam_role.on_incomplete_draft.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
        Resource = "${aws_dynamodb_table.invoices.arn}/stream/*"
      },
      {
        # the poke itself — the runtime AND its endpoint are both authorized on a qualified invoke
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [var.agent_runtime_endpoint_arn, replace(var.agent_runtime_endpoint_arn, "/(/runtime-endpoint/.*)$/", "")]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

module "on_incomplete_draft" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-on_incomplete_draft"
  role            = aws_iam_role.on_incomplete_draft.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/invoicing/lambdas/on_incomplete_draft.zip"
  src_dir         = "modules/invoicing/lambdas/on_incomplete_draft"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    AGENT_RUNTIME_ENDPOINT_ARN = var.agent_runtime_endpoint_arn
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.on_incomplete_draft
  to   = module.on_incomplete_draft.aws_lambda_function.this
}


module "on_incomplete_draft_stream" {
  count                = var.poke_agent ? 1 : 0
  source               = "../../terraform/stream"
  name                 = "${local.prefix}-on_incomplete_draft"
  stream_arn           = aws_dynamodb_table.invoices.stream_arn
  function_arn         = module.on_incomplete_draft.arn
  role_name            = aws_iam_role.on_incomplete_draft.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
}

moved {
  from = aws_lambda_event_source_mapping.on_incomplete_draft[0]
  to   = module.on_incomplete_draft_stream[0].aws_lambda_event_source_mapping.this
}

# ─── outputs ───

output "invoices_table_name" {
  value = aws_dynamodb_table.invoices.name
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

# ─── the POS read path ───
#
# A till POSTs a ticket and then POLLS for it. It never waits on the agent: an entry the protocol
# can't express lands as a draft, the agent completes it in the background, and the POS sees the
# result on its own clock. That needs a plain HTTP read, not a gateway tool — the gateway is the
# agent's surface, not a client's. The route lands on manage_invoice with no body: a request
# carrying path or query parameters and no op is a get.

resource "aws_apigatewayv2_integration" "get_invoices" {
  count                  = var.serve_web ? 1 : 0
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn["manage_invoice"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "invoices" {
  for_each           = var.serve_web ? toset(["GET /invoices", "GET /invoices/{invoice_id}"]) : toset([])
  api_id             = var.server_api_id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.get_invoices[0].id}"
  authorization_type = var.owner_authorizer_id != "" ? "JWT" : "AWS_IAM"
  authorizer_id      = var.owner_authorizer_id != "" ? var.owner_authorizer_id : null
}

resource "aws_lambda_permission" "apigw_get_invoices" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  count               = var.serve_web ? 1 : 0
  statement_id_prefix = "AllowAPIGatewayInvokeGetInvoices"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["manage_invoice"].name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# ─── the push for stuck drafts ───
#
# `on_incomplete_draft` wakes the agent the moment an unpostable ticket arrives; this is the
# backstop for the ones it couldn't finish. An incomplete draft never issues, so it never reaches a
# statement — the failure is silent, and it is revenue the owner thinks they have. Findable
# (`manage_invoice {op: get, incomplete: true}`) is not the same as noticed.
#

# The owner routes read who the owner is: the one parameter, nothing beside it.
resource "aws_iam_role_policy" "owner_sub" {
  name = "${local.prefix}-invoicing-owner-sub"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/owner_sub"
    }]
  })
}
