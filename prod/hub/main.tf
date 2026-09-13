# hub — a vended account whose whole stack is routing.
#
# The graph is buses. This account holds one main bus (`gerp-events`) that any gerp in the
# organization puts to — its own gerps for everything, a gerp elsewhere for an event addressed to
# one of this hub's gerps, having read this hub off the platform directory — a role every edge
# assumes, the queue an edge's failed delivery parks on, and the door a module adds an edge
# through. It holds no data, no consumer and nothing about any other hub: the operator account is
# a spoke behind one `forward` edge, and every gerp is a spoke behind its own. Rules per bus is an
# adjustable quota (L-244521F2), and nothing else in this account draws on it, so the hub's
# budget of edges is that number.

locals {
  config       = jsondecode(file("${path.module}/../../config.json"))
  stack_prefix = local.config.STACK_PREFIX
  org_ids      = concat([data.aws_organizations_organization.this.id], local.config.ORG_IDS)
  operator     = local.config.OPERATOR_ACCOUNT_ID
  ops_topic    = try(local.config.OPS_ALERTS_TOPICS[var.aws_region], local.config.OPS_ALERTS_TOPIC_ARN, "") # this region's (an alarm notifies its own region only)
  edge_sets    = local.config.EDGE_SETS
  bus_name     = "${local.stack_prefix}-events"
}

data "aws_organizations_organization" "this" {}

# ─── the main bus: the gerps of this region put here ───

resource "aws_cloudwatch_event_bus" "main" {
  name = local.bus_name
}

resource "aws_cloudwatch_event_bus_policy" "main" {
  event_bus_name = aws_cloudwatch_event_bus.main.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgPutEvents"
      Effect    = "Allow"
      Principal = "*"
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.main.arn
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })
}

# ─── the edge role: what a rule assumes to reach a bus or a queue in the org ───

resource "aws_iam_role" "edges" {
  name = "${local.stack_prefix}-hub-edges"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "events.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "edges" {
  name = "${local.stack_prefix}-hub-edges"
  role = aws_iam_role.edges.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "events:PutEvents"
        Resource  = "arn:aws:events:*:*:event-bus/*"
        Condition = { StringEquals = { "aws:ResourceOrgID" = local.org_ids } }
      },
      {
        # a capture's queue, in the account of whoever asked for it
        Effect    = "Allow"
        Action    = "sqs:SendMessage"
        Resource  = "arn:aws:sqs:*:*:*"
        Condition = { StringEquals = { "aws:ResourceOrgID" = local.org_ids } }
      },
    ]
  })
}

# ─── the queue a failed delivery parks on ───

resource "aws_sqs_queue" "failed" {
  name                      = "${local.stack_prefix}-edges-failed"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_policy" "failed" {
  queue_url = aws_sqs_queue.failed.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.failed.arn
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_cloudwatch_metric_alarm" "parked" {
  count               = local.ops_topic != "" ? 1 : 0
  alarm_name          = "${local.stack_prefix}-edges-parked"
  alarm_description   = "an edge delivery hub ${var.hub_id} gave up on is parked on ${aws_sqs_queue.failed.name}; read it, then redrive it or remove it"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.failed.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.ops_topic]
  ok_actions          = [local.ops_topic]
}

# ─── the forward edge: the operator is a spoke ───
#
# Every event not addressed to a gerp goes to the operator's own bus, where the publisher, the
# counters, the archive and the platform's reports are rules. Firehose cannot be a cross-account
# target, so the forward is what carries them.

resource "aws_cloudwatch_event_rule" "forward" {
  name           = "${local.stack_prefix}-edge-forward-operator"
  event_bus_name = aws_cloudwatch_event_bus.main.name
  event_pattern  = jsonencode({ detail = { to = [{ exists = false }] } })
}

resource "aws_cloudwatch_event_target" "forward" {
  rule           = aws_cloudwatch_event_rule.forward.name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  arn            = "arn:aws:events:${var.aws_region}:${local.operator}:event-bus/${local.stack_prefix}-operator"
  role_arn       = aws_iam_role.edges.arn
  dead_letter_config {
    arn = aws_sqs_queue.failed.arn
  }
}

# ─── the door ───

data "archive_file" "manage_edges" {
  type        = "zip"
  output_path = "${path.module}/.build/manage_edges.zip"
  source {
    content  = file("${path.module}/lambdas/manage_edges/main.py")
    filename = "main.py"
  }
  # the handler imports `aws` (modules/aws/aws.py): a hand-listed manifest, like tower's
  source {
    content  = file("${path.module}/../../modules/aws/aws.py")
    filename = "aws.py"
  }
}

resource "aws_iam_role" "manage_edges" {
  name = "${local.stack_prefix}-hub-manage-edges"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "manage_edges" {
  name = "${local.stack_prefix}-hub-manage-edges"
  role = aws_iam_role.manage_edges.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the edges: rules named gerp-edge-* on the main bus, and nothing else
        Effect = "Allow"
        Action = ["events:PutRule", "events:PutTargets", "events:RemoveTargets", "events:DeleteRule",
        "events:DescribeRule", "events:ListTargetsByRule"]
        Resource = "arn:aws:events:${var.aws_region}:${var.aws_account_id}:rule/${aws_cloudwatch_event_bus.main.name}/${local.stack_prefix}-edge-*"
      },
      {
        Effect   = "Allow"
        Action   = "events:ListRules"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.edges.arn
      },
      {
        Effect   = "Allow"
        Action   = "servicequotas:GetServiceQuota"
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "gerp/platform" } }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:*"
      },
    ]
  })
}

module "manage_edges" {
  source = "../../modules/terraform/lambda"

  name             = "${local.stack_prefix}-hub-manage-edges"
  role             = aws_iam_role.manage_edges.arn
  filename         = data.archive_file.manage_edges.output_path
  source_code_hash = data.archive_file.manage_edges.output_base64sha256
  src_dir          = "prod/hub/lambdas/manage_edges"
  timeout          = 30
  env_vars = {
    HUB_ID        = var.hub_id
    BUS_NAME      = aws_cloudwatch_event_bus.main.name
    BUS_ARN       = aws_cloudwatch_event_bus.main.arn
    EDGE_ROLE_ARN = aws_iam_role.edges.arn
    DLQ_ARN       = aws_sqs_queue.failed.arn
    EDGE_SETS     = jsonencode(local.edge_sets)
    STACK_PREFIX  = local.stack_prefix
  }
}

# the operator account's functions call the door: the provisioner adds a spoke at vend, the
# closure removes one, a person through scripts/edge.sh
resource "aws_lambda_permission" "manage_edges_operator" {
  statement_id  = "AllowOperatorInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.manage_edges.name
  # the account as its root arn: a bare account id is read back normalized to this and replaces
  # the grant on every plan
  principal = "arn:aws:iam::${local.operator}:root"
}

# ─── the sets, watched ───
#
# The door publishes each set's use as a percentage of its share of the rule quota; 80% is the
# word to raise the quota (L-244521F2) before the vend that needs it.

resource "aws_cloudwatch_metric_alarm" "set_80pct" {
  for_each            = local.ops_topic != "" ? local.edge_sets : {}
  alarm_name          = "${local.stack_prefix}-hub-${var.hub_id}-edges-${each.key}-80pct"
  alarm_description   = "hub ${var.hub_id}: the ${each.key} edge set is at 80% of its share of the rules-per-bus quota (L-244521F2); request the raise from this account"
  namespace           = "gerp/platform"
  metric_name         = "EdgesUsedPercent"
  dimensions          = { hub = var.hub_id, set = each.key }
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.ops_topic]
  ok_actions          = [local.ops_topic]
}

output "bus_arn" {
  value = aws_cloudwatch_event_bus.main.arn
}

output "manage_edges_arn" {
  value = module.manage_edges.arn
}
