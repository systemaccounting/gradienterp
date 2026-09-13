###############################################
# issue_collector — agent-raised escalations land as the operator's own inc tasks.
#
# One rule on the shared gerp-events bus matches platform/escalation.raised (any gerp's
# escalate tool); the collector resolves the OPERATOR GERP via the customers registry and
# cross-account-invokes manage_tasks (op: put) — one escalation, one inc task. Triage (public/private
# split, grouping, assignment) is the operator gerp's agent's job, poked by its own tasks
# stream. Role name is fixed (`<prefix>-issue-collector`) so modules/tasks grants it
# invoke by constructed ARN.
###############################################

resource "aws_iam_role" "issue_collector" {
  name = "${local.stack_prefix}-issue-collector"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "issue_collector" {
  name = "${local.stack_prefix}-issue-collector"
  role = aws_iam_role.issue_collector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # resolve the operator gerp -> aws_account_id
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = aws_dynamodb_table.customers.arn
      },
      {
        # cross-account invoke the operator gerp's tasks door (constructed name)
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:*:function:${local.stack_prefix}-tasks-*-manage_tasks"
      },
      {
        # an alarm's account -> its gerp
        Effect   = "Allow"
        Action   = "dynamodb:Scan"
        Resource = aws_dynamodb_table.customers.arn
      },
      {
        # read the lines behind an alarm in the gerp's account (init_customer's gerp-ops-read),
        # and this account's own for the operator stacks' alarms
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/${local.stack_prefix}-ops-read"
      },
      {
        Effect = "Allow"
        Action = ["logs:StartQuery", "logs:GetQueryResults", "logs:FilterLogEvents", "logs:DescribeLogGroups",
          "cloudwatch:ListMetrics", "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
        "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
        Resource = "*"
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

data "archive_file" "issue_collector" {
  type        = "zip"
  output_path = "${path.module}/.build/issue_collector.zip"
  source {
    content  = file("${path.module}/lambdas/issue_collector/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/../../../modules/aws/aws.py")
    filename = "aws.py"
  }
}

module "issue_collector" {
  source = "../../../modules/terraform/lambda"

  name             = "${local.stack_prefix}-issue-collector"
  role             = aws_iam_role.issue_collector.arn
  filename         = data.archive_file.issue_collector.output_path
  source_code_hash = data.archive_file.issue_collector.output_base64sha256
  src_dir          = "prod/platform/operator/lambdas/issue_collector"
  timeout          = 30
  env_vars = {
    CUSTOMERS_TABLE     = aws_dynamodb_table.customers.name
    STACK_PREFIX        = local.stack_prefix
    OPERATOR_GERP_ID    = var.operator_gerp_id
    OPERATOR_ACCOUNT_ID = local.operator_account_id
    OPS_READ_ROLE       = "${local.stack_prefix}-ops-read"
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.issue_collector
  to   = module.issue_collector.aws_lambda_function.this
}


resource "aws_cloudwatch_event_rule" "platform_reports" {
  name           = "${local.stack_prefix}-platform-reports"
  description    = "Agent-raised escalations -> the issue collector."
  event_bus_name = aws_cloudwatch_event_bus.operator.name
  event_pattern = jsonencode({
    source        = ["platform"]
    "detail-type" = ["escalation.raised"]
  })
}

resource "aws_cloudwatch_event_target" "issue_collector" {
  rule           = aws_cloudwatch_event_rule.platform_reports.name
  event_bus_name = aws_cloudwatch_event_bus.operator.name
  arn            = module.issue_collector.arn
}

resource "aws_lambda_permission" "reports_invoke" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowEventBridgeInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.issue_collector.name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.platform_reports.arn
}

variable "operator_gerp_id" {
  description = "The gerp whose task list is the platform's issue tracker (the platform is its own first customer)."
  type        = string
  default     = "gradienterp"
}

# the ops topic of every region (tower regions.tf; an alarm notifies its own region's) invokes
# the collector on every alarm state change — one permission per topic, since a permission's
# source arn names one region
resource "aws_lambda_permission" "issue_collector_alarms" {
  for_each = local.config.OPS_ALERTS_TOPICS

  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowOpsAlertsInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.issue_collector.name
  principal           = "sns.amazonaws.com"
  source_arn          = each.value
}

moved {
  from = aws_lambda_permission.issue_collector_alarms
  to   = aws_lambda_permission.issue_collector_alarms["us-east-1"]
}
