# purchasing — the buy side (procure-to-pay).
#
# First cut: the direct, non-agentic flow — create_po → manage_po receive → manage_po pay
# (+ manage_po get). A PO is the procurement record + the AP lifecycle (open → received → paid);
# receipt/payment post journal entries against accounting. The agentic cross-firm
# negotiation (quote → po.proposed/accepted over addressed events) is the deferred layer.

locals {
  prefix = "${var.stack_prefix}-purchasing-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb ───

resource "aws_dynamodb_table" "orders" {
  name         = "${local.prefix}-orders"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "po_id"

  attribute {
    name = "po_id"
    type = "S"
  }

  # a receipt's consequences are their own inserts, fired off this stream (on_po_received moves the
  # stock; custody + notifications attach here next) — so the receive op keeps doing one thing.
  # OLD image too: the handler fires on the open → received EDGE, not on every write to the row.
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
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = aws_dynamodb_table.orders.arn
      },
      {
        # create_po stamps its self-approved rows (on the shared store when provided)
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [var.shared_agreements_table_arn]
      },
      {
        # receipt/payment post the PO's journal entries
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
      },
      {
        # the receipt cascade consumes the orders stream and asks inventory to move the count
        Effect   = "Allow"
        Action   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
        Resource = "${aws_dynamodb_table.orders.arn}/stream/*"
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.update_stock_fn_name}"
      },
      {
        # manage_po get reads shipping's custody rows to answer a PO with its delivery. READ ONLY —
        # custody stays shipping's to write (subledger discipline); this is a join, not ownership.
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:Query"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.shipments_table_name}"
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

# ─── lambdas (agent tools — and the non-agentic interface: the owner records POs directly) ───

locals {
  functions = toset([
    "create_po", # the buyer's create — cross-firm by default, self-approve (approved:true) for an off-platform PO
    "manage_po", # receive | pay | get — the AP lifecycle after the PO exists, and the read
    "request_quote",
    # accept_po (the seller's acceptance) lives in modules/invoicing — the sell side
  ])

  env_vars = {
    ORDERS_TABLE          = aws_dynamodb_table.orders.name
    AGREEMENTS_TABLE      = var.shared_agreements_table_name # modules/agreements owns the store; there is no local one
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    GERP_ID               = var.gerp_id              # the `from` on addressed events
    SHIPMENTS_TABLE       = var.shipments_table_name # manage_po get joins the PO to its delivery (read-only)
    OP_EVENT_BUS_ARN      = var.op_event_bus_arn     # request_quote / create_po / accept_po emit here
    DIRECTORY_TABLE_ARN   = var.directory_table_arn
    HUB_ID                = var.hub_id
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/purchasing/lambdas/${each.key}.zip"
  src_dir            = "modules/purchasing/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["create_po"]
  to   = module.fn["create_po"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["manage_po"]
  to   = module.fn["manage_po"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["request_quote"]
  to   = module.fn["request_quote"].aws_lambda_function.this
}


# ─── agent gateway registration (same pattern as modules/labor / modules/tasks) ───

locals {
  # target-only tools: the schema lives here with the domain, the lambda is the shared agreements
  # request service (return_quote is the seller's proposal on the buyer's thread; the kind rides on
  # the tool name, KIND_BY_TOOL in the service)
  tool_only = var.agreements_tools ? { return_quote = var.agreements_request_fn_arn } : {}

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
        # create_po repointed to the shared agreements request service — same schema, the kind
        # rides on the tool name (KIND_BY_TOOL in the service)
        lambda_arn = lookup(local.tool_only, each.key, null) != null ? local.tool_only[each.key] : ((each.key == "create_po" && var.agreements_request_fn_arn != "") ? var.agreements_request_fn_arn : module.fn[each.key].arn)

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
  # target-only tools point at the shared agreements service, whose grant lives in modules/agreements
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

# ─── settlement: an accepted agreement → an open PO (stream-fired, no agent) ───

resource "aws_iam_role" "settle" {
  name = "${local.prefix}-settle"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "settle" {
  name = "${local.prefix}-settle"
  role = aws_iam_role.settle.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # write the agreed PO into orders (status open)
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = aws_dynamodb_table.orders.arn
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

module "settle_agreement" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-settle_agreement"
  role            = aws_iam_role.settle.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/purchasing/lambdas/settle_agreement.zip"
  src_dir         = "modules/purchasing/lambdas/settle_agreement"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    ORDERS_TABLE = aws_dynamodb_table.orders.name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.settle_agreement
  to   = module.settle_agreement.aws_lambda_function.this
}


# ─── the receipt cascade: an insert triggering its own inserts ───

module "on_po_received" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-on_po_received"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/purchasing/lambdas/on_po_received.zip"
  src_dir         = "modules/purchasing/lambdas/on_po_received"
  gerp_id         = var.gerp_id
  timeout         = 60
  env_vars = {
    UPDATE_STOCK_FN = var.update_stock_fn_name # inventory owns the write; this only tells it
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.on_po_received
  to   = module.on_po_received.aws_lambda_function.this
}


module "on_po_received_stream" {
  source               = "../../terraform/stream"
  name                 = "${local.prefix}-on_po_received"
  stream_arn           = aws_dynamodb_table.orders.stream_arn
  function_arn         = module.on_po_received.arn
  role_name            = aws_iam_role.lambda.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
}

moved {
  from = aws_lambda_event_source_mapping.on_po_received
  to   = module.on_po_received_stream.aws_lambda_event_source_mapping.this
}

# ─── outputs ───

output "orders_table_name" {
  value = aws_dynamodb_table.orders.name
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.
