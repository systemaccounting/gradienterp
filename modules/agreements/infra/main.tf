# agreements — the shared two-stamp negotiation store, as infra.
#
# One (thread, terms_hash) table for EVERY agreement kind — a capital offer, a PO, a salary — plus
# apply_inbound, the router target that stamps a counterparty's slot whatever the product is. The
# per-module part of an agreement is only what an agreed row PRODUCES (the settle effect), which
# stays in the owning module and is reached through the table's stream.
#
# Both roles coexist on one table (a firm raising capital is the seller, a firm investing is the
# buyer) — treasury proved that live, so there is no per-role or per-kind table.

locals {
  prefix             = "${var.stack_prefix}-agreements-${replace(var.gerp_id, "_", "-")}"
  apply_inbound_name = "${local.prefix}-apply_inbound"

  # the two services — one lambda per verb, every kind's gateway target points at one of these
  # two arns (a target's inline schema stays domain-shaped; two targets can share a lambda_arn)
  services = toset(["request", "accept", "decline"])

  settle_name = "${local.prefix}-settle"
  gerp        = replace(var.gerp_id, "_", "-")

  # each kind's settle effects, by CONSTRUCTED name (the inbox-router convention) — a module ref
  # here would cycle, since those modules will point their gateway targets at the service arns
  effect_fns = {
    purchasing_settle = "${var.stack_prefix}-purchasing-${local.gerp}-settle_agreement"
    invoicing_settle  = "${var.stack_prefix}-invoicing-${local.gerp}-settle_agreement"
    treasury_settle   = "${var.stack_prefix}-treasury-${local.gerp}-settlement"
  }
  effect_arn_prefix = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_dynamodb_table" "agreements" {
  name         = local.prefix
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread"     # the negotiation (requester-created id)
  range_key    = "terms_hash" # fingerprint of the terms (agreement key)

  attribute {
    name = "thread"
    type = "S"
  }
  attribute {
    name = "terms_hash"
    type = "S"
  }

  # the settle stream — an agreed row (both stamps) fires the kind's effect lambda
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"
}

# ─── apply_inbound: the counterparty's stamp, arriving over the rail ───
#
# Router-invoked by detail_type (`<product>.proposed` / `<product>.accepted`) — no ESM, no gateway
# target, not an agent tool. Money events (distribution.paid) are not stamps and route elsewhere.

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
        # request / accept stamp the counterparty's slot on the (thread, terms_hash) row
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.agreements.arn
      },
      {
        # a proposal decided here: the firm's PROPOSAL#<kind> rows, and the shelf for a rule that
        # answers a PO from it
        Effect = "Allow"
        Action = ["dynamodb:Query", "dynamodb:GetItem"]
        Resource = [
          "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}",
          "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.items_table_name}",
        ]
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
  artifact_key    = "modules/agreements/lambdas/apply_inbound.zip"
  src_dir         = "modules/agreements/lambdas/apply_inbound"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    AGREEMENTS_TABLE     = aws_dynamodb_table.agreements.name
    GERP_ID              = var.gerp_id
    OP_EVENT_BUS_ARN     = var.op_event_bus_arn # a rule's accept or counter says so to the sender
    DIRECTORY_TABLE_ARN  = var.directory_table_arn
    HUB_ID               = var.hub_id
    RULE_INSTANCES_TABLE = var.rule_instances_table_name # PROPOSAL#<kind>: what this firm answers without a turn
    ITEMS_TABLE          = var.items_table_name          # the shelf, for accept_in_stock
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.apply_inbound
  to   = module.apply_inbound.aws_lambda_function.this
}


# ─── the two services: request / accept (shared role) ───
#
# No gateway targets here — each KIND registers its own domain-shaped target (create_po,
# propose_offer, ...) from its module's schema, pointing at these arns. The lambda permission for
# the gateway role lands with those targets.

resource "aws_iam_role" "services" {
  name = "${local.prefix}-services"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "services" {
  name = "${local.prefix}-services"
  role = aws_iam_role.services.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # request / accept stamp the caller's slot on the (thread, terms_hash) row
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
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "service" {
  for_each = local.services
  source   = "../../terraform/lambda"

  name            = "${local.prefix}-${each.key}"
  role            = aws_iam_role.services.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/agreements/lambdas/${each.key}.zip"
  src_dir         = "modules/agreements/lambdas/${each.key}"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    AGREEMENTS_TABLE    = aws_dynamodb_table.agreements.name
    GERP_ID             = var.gerp_id
    OP_EVENT_BUS_ARN    = var.op_event_bus_arn
    DIRECTORY_TABLE_ARN = var.directory_table_arn
    HUB_ID              = var.hub_id
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.service["request"]
  to   = module.service["request"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.service["accept"]
  to   = module.service["accept"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.service["decline"]
  to   = module.service["decline"].aws_lambda_function.this
}


# ─── gateway invoke permission (the targets live with their kinds; the grant lives here) ───

resource "aws_lambda_permission" "gateway_invoke" {
  for_each = var.register_with_agent ? local.services : toset([])

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.service[each.key].name
  principal           = var.gateway_role_arn

  # gateway role arn is immutable per customer — pin it so an agent-image bump doesn't churn
  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── the per-kind config rows: AGREEMENT#<kind> ───
#
# What an agreed row of each kind PRODUCES (per side — the same row settles on both gerps and
# means opposite things) and which money step fires off the accept. Terraform-seeded so the
# dispatch config is code, not something a seeding script has to remember.

resource "aws_dynamodb_table_item" "kind_po" {
  table_name = aws_dynamodb_table.agreements.name
  hash_key   = "thread"
  range_key  = "terms_hash"

  item = jsonencode({
    thread     = { S = "AGREEMENT#po" }
    terms_hash = { S = "config" }
    produces = { M = {
      buyer  = { S = local.effect_fns.purchasing_settle } # the buyer's open PO
      seller = { S = local.effect_fns.invoicing_settle }  # the seller's draft invoice
    } }
  })
}

resource "aws_dynamodb_table_item" "kind_offer" {
  table_name = aws_dynamodb_table.agreements.name
  hash_key   = "thread"
  range_key  = "terms_hash"

  item = jsonencode({
    thread     = { S = "AGREEMENT#offer" }
    terms_hash = { S = "config" }
    money      = { S = "capital_purchase" } # both sides' legs fire off the accept (purchase.pay)
    produces = { M = {
      # the holder (buyer) produces NOTHING — its claim is the agreement row + the DR INVESTMENTS
      # entry, and what has come back is a ledger fold (manage_capital holdings)
      seller = { S = local.effect_fns.treasury_settle } # the issuer attaches the instrument
    } }
  })
}

# ─── settle: the ONE consumer of the shared stream ───
#
# A DDB stream takes two readers before throttling, so the per-kind effects don't each mount an
# ESM — settle does, and dispatches by the kind's config row (sync invoke, domain work only; every
# write to the agreement row itself happens in settle).

resource "aws_iam_role" "settle" {
  name = local.settle_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "settle" {
  name = local.settle_name
  role = aws_iam_role.settle.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the kind's config row (GetItem) + the funds stamp and settled_time (UpdateItem)
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.agreements.arn
      },
      {
        # the money step posts through accounting; the effects are the per-kind settle lambdas
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = concat(
          [var.post_journal_entry_fn_arn],
          [for fn in values(local.effect_fns) : "${local.effect_arn_prefix}:${fn}"],
        )
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams",
        ]
        Resource = "${aws_dynamodb_table.agreements.arn}/stream/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "settle" {
  source = "../../terraform/lambda"

  name            = local.settle_name
  role            = aws_iam_role.settle.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/agreements/lambdas/settle.zip"
  src_dir         = "modules/agreements/lambdas/settle"
  gerp_id         = var.gerp_id
  timeout         = 60
  env_vars = {
    AGREEMENTS_TABLE      = aws_dynamodb_table.agreements.name
    GERP_ID               = var.gerp_id
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.settle
  to   = module.settle.aws_lambda_function.this
}


module "settle_stream" {
  source               = "../../terraform/stream"
  name                 = "${local.prefix}-settle"
  stream_arn           = aws_dynamodb_table.agreements.stream_arn
  function_arn         = module.settle.arn
  role_name            = aws_iam_role.settle.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
}

moved {
  from = aws_lambda_event_source_mapping.settle
  to   = module.settle_stream.aws_lambda_event_source_mapping.this
}

# ─── outputs ───

output "agreements_table_name" {
  value = aws_dynamodb_table.agreements.name
}

output "agreements_table_arn" {
  value = aws_dynamodb_table.agreements.arn
}

output "agreements_stream_arn" {
  description = "The settle stream — the per-kind effect consumes agreed rows from it."
  value       = aws_dynamodb_table.agreements.stream_arn
}

output "apply_inbound_fn_name" {
  description = "Router target for every <kind>.proposed / <kind>.accepted."
  value       = module.apply_inbound.name
}

output "service_fn_arns" {
  description = "request/accept lambda arns — every kind's gateway target points at one of these two."
  value       = { for k in local.services : k => module.service[k].arn }
}

output "service_fn_names" {
  value = { for k in local.services : k => module.service[k].name }
}

# latest artifact version per function — the apply-time read that makes terraform deploy BUCKET
# truth (always current via scripts/deploy.sh push) instead of the applier's tree.
