# treasury — the distribution handler + the capital marketplace (offer → settle → instrument).
#
# distribution fires on period close (accounting's get_statement (balances) completion-invoke). For each
# INSTRUMENT (a rule instance attached to `DISTRIBUTION#<id>` in the rule-instances table), it runs
# what's attached, posts DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE via accounting, and emits
# distribution.paid.
#
# The marketplace side rides the SHARED modules/agreements substrate — the same request/accept
# (thread, terms_hash) table purchasing/invoicing use. The agent tools (propose_offer / accept_offer
# / record_capital_receipt / get_offers) stamp the row; the funded gate (both stamps +
# funds_receipt_ledger_entry) fires settlement over the table's stream, which creates the instrument.
#
# Bundling is by import graph (scripts/deploy.py): distribution pulls rules/instances + treasury_rules;
# the tools + settlement pull modules/agreements/agreements.py + the lambdas-root _helpers.py.

locals {
  prefix            = "${var.stack_prefix}-treasury-${replace(var.gerp_id, "_", "-")}"
  distribution_name = "${local.prefix}-distribution"
  settlement_name   = "${local.prefix}-settlement"

  # the agent tools — the capital marketplace primitives (each has a schema.json → gateway target).
  # propose_offer / accept_offer are TARGET-ONLY now: the schema (the agent's surface) lives here
  # with the domain, the lambda is the shared agreements service (modules/agreements grants its
  # invoke permission).
  tools = toset([
    "manage_capital", # record_receipt / record_outlay (the two money legs), offers, holdings
  ])
  tool_only = merge(
    var.agreements_tools ? { propose_offer = var.agreements_request_fn_arn } : {},
    var.agreements_tools ? { accept_offer = var.agreements_accept_fn_arn } : {},
    var.agreements_tools ? { decline_offer = var.agreements_decline_fn_arn } : {},
  )

  # Router-invoked, not an agent tool and not a gateway target: the inbox dispatches it by
  # detail_type when a counterparty's stamp (or a distribution they declared) lands. Same shape as
  # invoicing's apply_inbound.
  apply_inbound_name = "${local.prefix}-apply_inbound"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── iam: distribution ───

resource "aws_iam_role" "distribution" {
  name = local.distribution_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "distribution" {
  name = local.distribution_name
  role = aws_iam_role.distribution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # fold each instrument's prior DIVIDENDS_PAYABLE credits from accounting's ledger
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.ledger_table_name}"
      },
      {
        # an instrument's terms — the instances attached to it (Query) + the instrument
        # enumeration (Scan for the `DISTRIBUTION#` subjects)
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:Scan"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}"
      },
      {
        # post the distribution entry
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
      },
      {
        # announce distribution.paid on the shared bus (fire-and-forget)
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.op_event_bus_arn
      },
      {
        # openly_operated flag — read from the settings config table at cold start
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = var.settings_table_arn
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

module "distribution" {
  source = "../../terraform/lambda"

  name            = local.distribution_name
  role            = aws_iam_role.distribution.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/treasury/lambdas/distribution.zip"
  src_dir         = "modules/treasury/lambdas/distribution"
  gerp_id         = var.gerp_id
  timeout         = 60
  env_vars = {
    LEDGER_TABLE          = var.ledger_table_name
    RULE_INSTANCES_TABLE  = var.rule_instances_table_name # an instrument IS a row here
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    OP_EVENT_BUS_ARN      = var.op_event_bus_arn
    CUSTOMER_ID           = var.gerp_id
    SETTINGS_TABLE        = var.settings_table_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.distribution
  to   = module.distribution.aws_lambda_function.this
}


# get_statement (balances) (accounting, same account) invokes this on period close by constructed name —
# accounting doesn't depend on a treasury output, keeping the accounting<->treasury graph acyclic.

# ════════════════════════════════════════════════════════════════════════════
# treasury-agreements — the capital negotiation store (the shared modules/agreements substrate).
#
# A (thread, terms_hash) row: the firm (seller) and investor (buyer) each stamp their slot on the
# terms; the firm records the incoming funds (funds_receipt_ledger_entry). Both stamps + funds ⇒
# the stream fires settlement, which ATTACHES the rule instance that IS the instrument
# (pk `DISTRIBUTION#<thread>`). Same table shape as purchasing/invoicing; treasury's extra gate is
# the funds receipt, and its settle effect is a rule instance instead of a PO/invoice.
# ════════════════════════════════════════════════════════════════════════════

resource "aws_dynamodb_table" "agreements" {
  name         = "${local.prefix}-agreements"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread"     # the capital negotiation (firm + investor + product)
  range_key    = "terms_hash" # fingerprint of the proposed terms (agreement key)

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

# ─── iam: the agent tools (shared role) ───

resource "aws_iam_role" "tools" {
  name = "${local.prefix}-tools"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "tools" {
  name = "${local.prefix}-tools"
  role = aws_iam_role.tools.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # request / approve / note stamp the row; get_agreement reads it; get_offers scans it
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
        Resource = compact([aws_dynamodb_table.agreements.arn, var.shared_agreements_table_arn])
      },
      {
        # the two capital money legs: manage_capital record_receipt posts DR CASH / CR OWNER_EQUITY
        # (the issuer), record_outlay posts DR INVESTMENTS / CR CASH (the holder)
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
      },
      {
        # manage_capital holdings folds the ledger for what a holding cost and what has come back — a holding
        # is not stored, it IS the agreement row plus this slice (see treasury/ledger.py)
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.ledger_table_name}"
      },
      {
        # propose_offer / accept_offer emit addressed offer.* events on the shared bus
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.op_event_bus_arn
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

module "tool" {
  for_each = local.tools
  source   = "../../terraform/lambda"

  name            = "${local.prefix}-${each.key}"
  role            = aws_iam_role.tools.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/treasury/lambdas/${each.key}.zip"
  src_dir         = "modules/treasury/lambdas/${each.key}"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    # capital deals live on the SHARED agreements store once the targets repoint — the tools
    # read and stamp there; the module's own table stays as settled history
    AGREEMENTS_TABLE      = var.shared_agreements_table_name != "" ? var.shared_agreements_table_name : aws_dynamodb_table.agreements.name
    LEDGER_TABLE          = var.ledger_table_name # holdings folds it; nothing stores a holding
    GERP_ID               = var.gerp_id           # the `from` on addressed events + the default seller/buyer
    OP_EVENT_BUS_ARN      = var.op_event_bus_arn
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name # the two capital money legs
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.tool["manage_capital"]
  to   = module.tool["manage_capital"].aws_lambda_function.this
}


# ─── apply_inbound: the counterparty's stamp, arriving over the rail ───
#
# Treasury's tools all stamp the CALLER's own side, which left a hole: nothing consumed a
# counterparty's stamp arriving inbound, so a bid sent to another firm stayed half-stamped forever.
# Router-invoked by detail_type (offer.proposed / offer.accepted / distribution.paid) — no ESM, no
# gateway target, not an agent tool. Mirror of invoicing's apply_inbound on the same substrate.

resource "aws_iam_role" "apply" {
  name = local.apply_inbound_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "apply" {
  name = local.apply_inbound_name
  role = aws_iam_role.apply.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # "do we hold a claim on this instrument" — list_agreements over the SHARED store
        Effect   = "Allow"
        Action   = "dynamodb:Scan"
        Resource = compact([var.shared_agreements_table_arn != "" ? var.shared_agreements_table_arn : aws_dynamodb_table.agreements.arn])
      },
      {
        # an inbound distribution.paid books DR ACCOUNTS_RECEIVABLE / CR INVESTMENT_INCOME
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "apply_inbound" {
  source = "../../terraform/lambda"

  name            = local.apply_inbound_name
  role            = aws_iam_role.apply.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/treasury/lambdas/apply_inbound.zip"
  src_dir         = "modules/treasury/lambdas/apply_inbound"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    AGREEMENTS_TABLE      = var.shared_agreements_table_name != "" ? var.shared_agreements_table_name : aws_dynamodb_table.agreements.name
    GERP_ID               = var.gerp_id
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.apply_inbound
  to   = module.apply_inbound.aws_lambda_function.this
}


output "apply_inbound_fn_name" {
  description = "Router target for offer.proposed / offer.accepted / distribution.paid — the inbound half of the capital rail."
  value       = module.apply_inbound.name
}

# ─── agent gateway registration (same pattern as modules/purchasing / modules/labor) ───

locals {
  tool_schemas = {
    for k in setunion(local.tools, keys(local.tool_only)) :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent && fileexists("${path.module}/../lambdas/${k}/schema.json")
  }
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_schemas

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent]) doesn't force-replace the target.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = replace(each.key, "_", "-")
  description = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = lookup(local.tool_only, each.key, null) != null ? local.tool_only[each.key] : module.tool[each.key].arn

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
  function_name       = module.tool[each.key].name
  principal           = var.gateway_role_arn

  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── settlement: an accepted, funded agreement → the created instrument (stream-fired, no agent) ───

resource "aws_iam_role" "settlement" {
  name = local.settlement_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "settlement" {
  name = local.settlement_name
  role = aws_iam_role.settlement.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # issue the instrument: attach its rule instance (pk `DISTRIBUTION#<thread>`)
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}"
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

module "settlement" {
  source = "../../terraform/lambda"

  name            = local.settlement_name
  role            = aws_iam_role.settlement.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/treasury/lambdas/settlement.zip"
  src_dir         = "modules/treasury/lambdas/settlement"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    RULE_INSTANCES_TABLE = var.rule_instances_table_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.settlement
  to   = module.settlement.aws_lambda_function.this
}


# ─── outputs ───

output "distribution_fn_name" {
  value = module.distribution.name
}

output "distribution_fn_arn" {
  value = module.distribution.arn
}

output "agreements_table_name" {
  value = aws_dynamodb_table.agreements.name
}

# latest artifact version per function — the apply-time read that makes terraform deploy BUCKET
# truth (always current via scripts/deploy.sh push) instead of the applier's tree.
