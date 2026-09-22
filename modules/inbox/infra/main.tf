# inbox — the firm's inbound door for addressed cross-firm events.
#
# The hub's spoke edge puts every event whose detail.to == this gerp onto the firm's own bus;
# the consume rule below takes it to receive_inbound, which lands it as a durable row in the
# inbound table — the firm's own state. The table's stream is enabled for the own-stream-poke
# (the firm's agent wakes on an inbound, under its own policy).
#
# No agent-gateway registration — this isn't a chat tool; the bus invokes it.

locals {
  prefix = "${var.stack_prefix}-inbox-${replace(var.gerp_id, "_", "-")}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── inbound table ───

resource "aws_dynamodb_table" "inbound" {
  name         = "${local.prefix}-inbound"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "inbound_id"

  attribute {
    name = "inbound_id"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"
}

# ─── iam ───

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
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.inbound.arn
      },
      {
        # the sender's directory row names its account, which an inbound event has to come from
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

# ─── lambda ───

module "receive_inbound" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-receive_inbound"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/inbox/lambdas/receive_inbound.zip"
  src_dir         = "modules/inbox/lambdas/receive_inbound"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    INBOUND_TABLE       = aws_dynamodb_table.inbound.name
    DIRECTORY_TABLE_ARN = var.directory_table_arn
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.receive_inbound
  to   = module.receive_inbound.aws_lambda_function.this
}



# ─── own-stream-poke: wake this firm's agent on inbound ───
# ESM on the inbound stream (INSERT) -> poke_agent -> invoke this gerp's agent runtime.
# The poke is the firm's own — its stream, its agent, its policy — not the sender's.
# (Which detail_types are worth waking for is a policy refinement; first cut pokes on
# every inbound INSERT.)

resource "aws_iam_role" "poke" {
  name = "${local.prefix}-poke"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "poke" {
  name = "${local.prefix}-poke"
  role = aws_iam_role.poke.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # wake this gerp's own agent runtime (same account)
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

module "poke_agent" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-poke_agent"
  role            = aws_iam_role.poke.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/inbox/lambdas/poke_agent.zip"
  src_dir         = "modules/inbox/lambdas/poke_agent"
  gerp_id         = var.gerp_id
  timeout         = 60
  env_vars = {
    AGENT_RUNTIME_ENDPOINT_ARN = var.agent_runtime_endpoint_arn
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.poke_agent
  to   = module.poke_agent.aws_lambda_function.this
}


# ─── router: the single inbound-stream consumer, fans out by detail_type ───
# One ESM (so the DDB-stream 2-reader limit isn't a wall). Routed events go to their module
# handler by constructed name; everything else → the agent poke. Same thin shape as the
# hub's spoke edge, one level down (detail_type instead of detail.to).

locals {
  gerp               = replace(var.gerp_id, "_", "-")
  poke_fn            = "${local.prefix}-poke_agent"
  shipping_fn        = "${var.stack_prefix}-shipping-${local.gerp}-apply_shipment_event" # shipment.sent → our inbound custody row + ETA
  treasury_fn        = "${var.stack_prefix}-treasury-${local.gerp}-apply_inbound"        # distribution.paid → income on a holding (money, not a stamp)
  agreements_fn      = "${var.stack_prefix}-agreements-${local.gerp}-apply_inbound"      # every <kind>.proposed / <kind>.accepted → the counterparty's stamp, one handler for all kinds
  handler_arn_prefix = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function"
}

resource "aws_iam_role" "router" {
  name = "${local.prefix}-router"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "router" {
  name = "${local.prefix}-router"
  role = aws_iam_role.router.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # consume the inbound stream
        Effect   = "Allow"
        Action   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
        Resource = "${aws_dynamodb_table.inbound.arn}/stream/*"
      },
      {
        # invoke the handlers (same account, by constructed name)
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          "${local.handler_arn_prefix}:${local.poke_fn}",
          "${local.handler_arn_prefix}:${local.shipping_fn}",
          "${local.handler_arn_prefix}:${local.treasury_fn}",
          "${local.handler_arn_prefix}:${local.agreements_fn}",
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

module "router" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-router"
  role            = aws_iam_role.router.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/inbox/lambdas/router.zip"
  src_dir         = "modules/inbox/lambdas/router"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    ROUTES = jsonencode({
      "po.accepted"       = local.agreements_fn, # the negotiation stamps — one kind-agnostic handler
      "po.proposed"       = local.agreements_fn,
      "offer.proposed"    = local.agreements_fn,
      "offer.accepted"    = local.agreements_fn,
      "po.declined"       = local.agreements_fn, # the terminal stamp, mirrored
      "offer.declined"    = local.agreements_fn,
      "shipment.sent"     = local.shipping_fn,
      "distribution.paid" = local.treasury_fn, # money on a holding, not a stamp
    })
    POKE_FN = local.poke_fn
    # Mechanical AND worth waking the owner's agent for. A counterparty accepting a capital deal,
    # or offering one, is not something to discover from a balance sheet later; an inbound
    # distribution is money and the owner should hear about it as it lands.
    # A decline is heard too: the sender's agent learns its proposal ended. A `.proposed` in this
    # list is poked only when apply_inbound answered nothing — a rule that accepted or countered
    # it already decided (the router waits for that answer).
    POKE_ALSO = jsonencode(["po.proposed", "offer.proposed", "offer.accepted", "po.declined", "offer.declined", "distribution.paid"])
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.router
  to   = module.router.aws_lambda_function.this
}


module "router_stream" {
  source               = "../../terraform/stream"
  name                 = "${local.prefix}-router"
  stream_arn           = aws_dynamodb_table.inbound.stream_arn
  function_arn         = module.router.arn
  role_name            = aws_iam_role.router.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
  filter_patterns      = [jsonencode({ eventName = ["INSERT"] })]
}

moved {
  from = aws_lambda_event_source_mapping.router
  to   = module.router_stream.aws_lambda_event_source_mapping.this
}

# ─── get_inbound: the agent reads its inbox (the recipient's perception) ───

resource "aws_iam_role" "get_inbound" {
  name = "${local.prefix}-get_inbound"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "get_inbound" {
  name = "${local.prefix}-get_inbound"
  role = aws_iam_role.get_inbound.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:Query", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.inbound.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "get_inbound" {
  source = "../../terraform/lambda"

  name               = "${local.prefix}-get_inbound"
  role               = aws_iam_role.get_inbound.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/inbox/lambdas/get_inbound.zip"
  src_dir            = "modules/inbox/lambdas/get_inbound"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = { INBOUND_TABLE = aws_dynamodb_table.inbound.name }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.get_inbound
  to   = module.get_inbound.aws_lambda_function.this
}


# ─── agent gateway registration (one tool) ───

locals {
  get_inbound_schema = jsondecode(file("${path.module}/../lambdas/get_inbound/schema.json"))
}

resource "aws_bedrockagentcore_gateway_target" "get_inbound" {
  count = var.register_with_agent ? 1 : 0

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = "get-inbound"
  description = local.get_inbound_schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.get_inbound.arn

        tool_schema {
          inline_payload {
            name        = "get_inbound"
            description = local.get_inbound_schema.description
            input_schema {
              type        = local.get_inbound_schema.type
              description = local.get_inbound_schema.description

              dynamic "property" {
                iterator = prop
                for_each = try(local.get_inbound_schema.properties, {})
                content {
                  name        = prop.key
                  type        = try(prop.value.type, "string")
                  description = try(prop.value.description, "")
                  required    = contains(try(local.get_inbound_schema.required, []), prop.key)
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

resource "aws_lambda_permission" "get_inbound_gateway" {
  count = var.register_with_agent ? 1 : 0

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.get_inbound.name
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

output "inbound_table_name" {
  value = aws_dynamodb_table.inbound.name
}

output "inbound_stream_arn" {
  description = "Stream of the -inbound table. Domain modules subscribe to route their inbound events deterministically (e.g. purchasing's po.proposed/po.accepted → request/approve)."
  value       = aws_dynamodb_table.inbound.stream_arn
}

output "receive_inbound_fn_name" {
  value = module.receive_inbound.name
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

# ─── the consume edge: this gerp's bus delivers what is addressed to it ───
#
# The hub's `spoke` edge (a rule on the hub's bus) puts every event with `detail.to` = this gerp
# onto the firm's own bus; this rule takes it from there to receive_inbound. The hub knows only
# the bus; what receives is declared here, beside the function. A delivery EventBridge gives up
# on parks on the failed queue, and the alarm on it stays until a person redrives or removes it.

resource "aws_cloudwatch_event_rule" "consume" {
  count          = var.internal_bus_name != "" ? 1 : 0
  name           = "${local.prefix}-consume"
  event_bus_name = var.internal_bus_name
  event_pattern  = jsonencode({ detail = { to = [var.gerp_id] } })
}

resource "aws_sqs_queue" "consume_failed" {
  count                     = var.internal_bus_name != "" ? 1 : 0
  name                      = "${local.prefix}-consume-failed"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_policy" "consume_failed" {
  count     = var.internal_bus_name != "" ? 1 : 0
  queue_url = aws_sqs_queue.consume_failed[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.consume_failed[0].arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.consume[0].arn } }
    }]
  })
}

resource "aws_cloudwatch_event_target" "consume" {
  count          = var.internal_bus_name != "" ? 1 : 0
  rule           = aws_cloudwatch_event_rule.consume[0].name
  event_bus_name = var.internal_bus_name
  arn            = module.receive_inbound.arn
  dead_letter_config {
    arn = aws_sqs_queue.consume_failed[0].arn
  }
}

resource "aws_lambda_permission" "consume" {
  count         = var.internal_bus_name != "" ? 1 : 0
  statement_id  = "AllowConsumeRule"
  action        = "lambda:InvokeFunction"
  function_name = module.receive_inbound.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.consume[0].arn
}

resource "aws_cloudwatch_metric_alarm" "consume_parked" {
  count               = var.internal_bus_name != "" && var.ops_alerts_topic_arn != "" ? 1 : 0
  alarm_name          = "${local.prefix}-consume-parked"
  alarm_description   = "an inbound delivery ${local.prefix} gave up on is parked on ${aws_sqs_queue.consume_failed[0].name}; read it, then redrive it or remove it"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.consume_failed[0].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.ops_alerts_topic_arn]
  ok_actions          = [var.ops_alerts_topic_arn]
}
