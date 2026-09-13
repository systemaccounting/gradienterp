###############################################
# alerts — a vend or a closure that fails reaches a person.
#
#   tower-per-customer FAILED|STOPPED|TIMED_OUT ──rule──▶ gerp-ops-alerts ──email──▶ ops+alerts@
#   a tower lambda's Errors ≥ 1 (an exception or a timeout) ──alarm──▶ gerp-ops-alerts
#   an async invoke that fails (the vend, the daily bill) ──on_failure──▶ gerp-ops-alerts,
#     with the request and the error on the message
#   the org at 80% of its account quota, the customers OU at 80% of its cap ──alarm──▶ gerp-ops-alerts
#
# The vend is not retried: an async failure re-run by Lambda would call ProvisionProduct again
# for the same purchase. One failure, one email, a person re-runs into the account that exists.
###############################################

variable "ops_alerts_email" {
  description = "Where a failed vend, closure, bill or build is reported. An ops+ address; the SES catch-all forwards it. The subscription confirms once by the link that arrives there."
  type        = string
  default     = "ops+alerts@gradienterp.cloud"
}

resource "aws_sns_topic" "ops_alerts" {
  name = "gerp-ops-alerts"
}

resource "aws_sns_topic_subscription" "ops_alerts_email" {
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "email"
  endpoint  = var.ops_alerts_email
}

# EventBridge publishes the build events; the alarms publish through CloudWatch's own principal,
# which the topic's default policy already admits
resource "aws_sns_topic_policy" "ops_alerts" {
  arn = aws_sns_topic.ops_alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "events"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.ops_alerts.arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.build_failed.arn } }
      },
      {
        Sid       = "alarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.ops_alerts.arn
        Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
      },
      {
        # every gerp's functions carry an Errors alarm (modules/terraform/lambda) in the gerp's own
        # account, all publishing here — admitted by the alarm's name, which every stack prefixes
        Sid       = "gerp-alarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.ops_alerts.arn
        Condition = { ArnLike = { "aws:SourceArn" = "arn:aws:cloudwatch:${data.aws_region.current.id}:*:alarm:gerp-*" } }
      },
    ]
  })
}

# ─── the alarm as a task ───
#
# Every state change on the topic reaches the issue collector (platform/operator): an ALARM
# becomes one task per failure kind on the operator gerp's tasks, an OK closes them. The email
# subscription stays as the backstop.
resource "aws_sns_topic_subscription" "ops_alerts_collector" {
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "lambda"
  endpoint  = "arn:aws:lambda:${data.aws_region.current.id}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
}

# ─── the build ───

resource "aws_cloudwatch_event_rule" "build_failed" {
  name        = "gerp-ops-build-failed"
  description = "tower-per-customer did not succeed: an apply or a closure's destroy, FAILED, STOPPED or TIMED_OUT"
  event_pattern = jsonencode({
    source        = ["aws.codebuild"]
    "detail-type" = ["CodeBuild Build State Change"]
    detail = {
      "project-name" = [aws_codebuild_project.per_customer.name]
      "build-status" = ["FAILED", "STOPPED", "TIMED_OUT"]
    }
  })
}

resource "aws_cloudwatch_event_target" "build_failed" {
  rule      = aws_cloudwatch_event_rule.build_failed.name
  target_id = "ops-alerts"
  arn       = aws_sns_topic.ops_alerts.arn

  # what a person needs before opening anything: the status, which gerp and which action (the
  # build's environment carries CUSTOMER_ID and TF_ACTION), the log, and where the run's state is
  input_transformer {
    input_paths = {
      project = "$.detail.project-name"
      status  = "$.detail.build-status"
      build   = "$.detail.build-id"
      env     = "$.detail.additional-information.environment.environment-variables"
      logs    = "$.detail.additional-information.logs.deep-link"
    }
    # a JSON object, laid out one key per line: a JSON string's escapes are delivered literally
    # by SNS, and plain text is not a template EventBridge accepts. The array renders as JSON.
    input_template = <<-EOT
      {
        "what": "<project> <status>",
        "build": "<build>",
        "log": "<logs>",
        "environment (CUSTOMER_ID is the gerp, TF_ACTION the action)": <env>,
        "where": "the run's state is the gerp's row on gerp-customers (status, aws_account_id, gateway_url, closes_on); the reason is in the log",
        "then": "a failed apply is re-run into the same account with StartBuild; a failed destroy leaves the row closing and the stack half down"
      }
    EOT
  }
}

# ─── the lambdas ───
# a raise anywhere in this account: `AWS/Lambda Errors` with no dimension is the account's sum
# (tower, the read api, the BFF, the optimizer, platform/operator, email). The log group names
# the function; a task made from the alarm queries the lines. One alarm, not one per function.
resource "aws_cloudwatch_metric_alarm" "operator_errors" {
  alarm_name          = "${local.stack_prefix}-operator-errors"
  alarm_description   = "a function in the operator account raised or timed out. Logs Insights over /aws/lambda/*: filter ispresent(errorType) or level = \"ERROR\" | stats count() by @log"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
  ok_actions          = [aws_sns_topic.ops_alerts.arn]
}

# the operator stacks' functions (tower, the read api, the BFF, the optimizer, platform/operator,
# email) count their caught failures into `gerp/app/operator`; one alarm on that sum
resource "aws_cloudwatch_metric_alarm" "operator_error_lines" {
  alarm_name          = "${local.stack_prefix}-operator-error-lines"
  alarm_description   = "an operator-stack function caught a failure and wrote it at ERROR. Logs Insights over the operator account's /aws/lambda/* groups: filter level = \"ERROR\" | stats count() by function, kind"
  namespace           = "gerp/app/operator"
  metric_name         = "ErrorLines"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
  ok_actions          = [aws_sns_topic.ops_alerts.arn]
}

# the early word: Account Factory is slow before the 900 s wall kills the run
resource "aws_cloudwatch_metric_alarm" "provision_duration" {
  alarm_name          = "${module.provision_customer.name}-slow"
  alarm_description   = "a vend has run past 80% of the provisioner's 900 s wall — Account Factory is slow; if the wall is hit the row stays provisioning and the account may exist without it"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = module.provision_customer.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 720000
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

# ─── the platform's room to vend ───
# Organizations publishes no usage metric, so bill_customer measures the two caps daily on its
# management session and publishes them here as percentages. A vend that meets either fails;
# 80% is where the request goes in, because the raise takes days.
resource "aws_cloudwatch_metric_alarm" "org_accounts_80pct" {
  alarm_name          = "${local.stack_prefix}-org-accounts-80pct"
  alarm_description   = "the org holds 80% of its account quota (L-E619E033; every account counts until permanently closed). Request the raise from the management account: aws service-quotas request-service-quota-increase --service-code organizations --quota-code L-E619E033 --desired-value <n>; it takes days"
  namespace           = "gerp/platform"
  metric_name         = "OrgAccountsUsedPercent"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
  ok_actions          = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "customers_ou_80pct" {
  alarm_name          = "${local.stack_prefix}-customers-ou-80pct"
  alarm_description   = "the customers OU holds 80% of Control Tower's 1,000 accounts per OU (not adjustable). Add a second customers OU in prod/platform/management, register it with Control Tower, and point provision_customer's CUSTOMERS_OU_MANAGED_NAME at it"
  namespace           = "gerp/platform"
  metric_name         = "CustomersOuUsedPercent"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
  ok_actions          = [aws_sns_topic.ops_alerts.arn]
}

# ─── the async invokes: no retry on the vend, the failure record on the topic ───

resource "aws_lambda_function_event_invoke_config" "provision_customer" {
  function_name          = module.provision_customer.name
  maximum_retry_attempts = 0 # a re-run would call ProvisionProduct again for the same purchase
  destination_config {
    on_failure {
      destination = aws_sns_topic.ops_alerts.arn
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "bill_customer" {
  function_name = module.bill_customer.name
  # Lambda's two retries stay: a bill booked on the second attempt is fine
  destination_config {
    on_failure {
      destination = aws_sns_topic.ops_alerts.arn
    }
  }
}

# the function's own role publishes the failure record; the build role publishes the retry
# record (an apply that passed on its second try after an IAM race, with the resource named)
resource "aws_iam_role_policy" "ops_alerts_destination" {
  for_each = {
    provision_customer = aws_iam_role.provision_customer.id
    bill_customer      = aws_iam_role.bill_customer.id
    codebuild          = aws_iam_role.codebuild.id
  }
  name = "ops-alerts-destination"
  role = each.value
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.ops_alerts.arn }]
  })
}

output "ops_alerts_topic_arn" {
  description = "Where a failed vend, closure, bill or build is published. 038's collector subscribes here."
  value       = aws_sns_topic.ops_alerts.arn
}

# ─── cross-account observability: every gerp's metrics and logs, readable here ───
#
# The sink is the operator account's side; each gerp account links to it (init_customer), and
# from then on this account's console, dashboards and GetMetricData read the gerp's metrics and
# Logs Insights its groups without assuming a role. Admitted by the org id, so a new vend links
# itself on its first apply.
resource "aws_oam_sink" "gerps" {
  name = "${local.stack_prefix}-gerps"
}

resource "aws_oam_sink_policy" "gerps" {
  sink_identifier = aws_oam_sink.gerps.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = ["oam:CreateLink", "oam:UpdateLink"]
      Resource  = "*"
      Condition = {
        "ForAllValues:StringEquals" = { "oam:ResourceTypes" = ["AWS::CloudWatch::Metric", "AWS::Logs::LogGroup"] }
        StringEquals                = { "aws:PrincipalOrgID" = local.org_ids }
      }
    }]
  })
}

output "oam_sink_arn" {
  value = aws_oam_sink.gerps.arn
}

# ─── gerp-ops: the person's window ───
#
# One dashboard in the operator account over every linked account. Every widget is a SEARCH
# with no account named — a monitoring account's SEARCH reaches every source account through
# the OAM links — so a vended gerp appears on its own once its link exists, and nothing here is
# edited per gerp. The account-level alarms are per account and stay in their accounts; the
# metrics they watch are the first two widgets.
resource "aws_cloudwatch_dashboard" "ops" {
  dashboard_name = "${local.stack_prefix}-ops"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 7
        properties = {
          title   = "raises — AWS/Lambda Errors, the account sums (one line per account)"
          view    = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{AWS/Lambda} MetricName=\"Errors\"', 'Sum', 300)", id = "e", label = "" }]]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 7
        properties = {
          title   = "caught failures — ErrorLines, one line per gerp (gerp/app/<gerp>)"
          view    = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('MetricName=\"ErrorLines\"', 'Sum', 300)", id = "l", label = "" }]]
        }
      },
      {
        type = "metric", x = 0, y = 7, width = 12, height = 7
        properties = {
          title   = "caught failures by kind (ErrorLinesByKind: function, kind, category)"
          view    = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{gerp/app,FunctionName,category,kind} MetricName=\"ErrorLinesByKind\"', 'Sum', 300)", id = "k", label = "" }]]
        }
      },
      {
        type = "metric", x = 12, y = 7, width = 12, height = 7
        properties = {
          title = "parked stream records (the -failed queues)"
          view  = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300, stat = "Maximum"
          # `failed` is a bare token (a substring match on the metric's names and values).
          # Quoted it is an exact value and matches nothing — the widget read empty until this.
          metrics = [[{ expression = "SEARCH('{AWS/SQS,QueueName} MetricName=\"ApproximateNumberOfMessagesVisible\" failed', 'Maximum', 300)", id = "q", label = "" }]]
        }
      },
      {
        type = "metric", x = 0, y = 14, width = 12, height = 7
        properties = {
          title   = "the fleet: invocations (the account sums)"
          view    = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{AWS/Lambda} MetricName=\"Invocations\"', 'Sum', 300)", id = "i", label = "" }]]
        }
      },
      {
        type = "metric", x = 12, y = 14, width = 12, height = 7
        properties = {
          title   = "the fleet: duration p95 ms (the account sums)"
          view    = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300
          metrics = [[{ expression = "SEARCH('{AWS/Lambda} MetricName=\"Duration\"', 'p95', 300)", id = "d", label = "" }]]
        }
      },
      {
        type = "metric", x = 0, y = 21, width = 24, height = 6
        properties = {
          title = "the gateways' 5xx (HTTP apis and the REST api, every account)"
          view  = "timeSeries", stacked = false, region = data.aws_region.current.id, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{AWS/ApiGateway,ApiId} MetricName=\"5xx\"', 'Sum', 300)", id = "h", label = "" }],
          [{ expression = "SEARCH('{AWS/ApiGateway,ApiName} MetricName=\"5XXError\"', 'Sum', 300)", id = "r", label = "" }]]
        }
      },
      {
        type = "alarm", x = 0, y = 27, width = 24, height = 3
        properties = {
          title = "the operator account's alarms"
          alarms = [aws_cloudwatch_metric_alarm.operator_errors.arn, aws_cloudwatch_metric_alarm.operator_error_lines.arn,
            aws_cloudwatch_metric_alarm.provision_duration.arn, aws_cloudwatch_metric_alarm.org_accounts_80pct.arn,
          aws_cloudwatch_metric_alarm.customers_ou_80pct.arn]
        }
      },
    ]
  })
}
