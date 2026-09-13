# ─── the vends queue: save-card sends, provision_customer consumes four at a time ───
#
# Control Tower runs five account operations at once, and a vend holds one for ~15 minutes. The
# owner app's BFF sends the provisioning payload here when a card lands; the mapping runs the
# provisioner on at most four messages at a time, so the fifth signup in a window waits on the
# queue with one Control Tower slot left for a hand-run. The row reads `queued` until the
# provisioner takes the message and writes `provisioning`.

resource "aws_sqs_queue" "vends_failed" {
  name                      = "tower-vends-failed"
  message_retention_seconds = 1209600 # 14 days: a parked vend waits for a person
}

resource "aws_sqs_queue" "vends" {
  name = "tower-vends"
  # six times the provisioner's 900 s wall (Lambda's rule for an SQS consumer): a consumer that
  # dies mid-vend surfaces the message 90 minutes later, when the account it started is long
  # ACTIVE and the provisioner's name check resumes it instead of vending a second
  visibility_timeout_seconds = 5400
  message_retention_seconds  = 1209600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.vends_failed.arn
    maxReceiveCount     = 3
  })
}

resource "aws_lambda_event_source_mapping" "vends" {
  event_source_arn        = aws_sqs_queue.vends.arn
  function_name           = module.provision_customer.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 4
  }
}

# the mapping polls and deletes as the function's role
resource "aws_iam_role_policy" "provision_customer_vends" {
  name = "tower-provision-customer-vends"
  role = aws_iam_role.provision_customer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.vends.arn
    }]
  })
}

# a vend the queue gave up on is parked; the alarm stays in ALARM until the message is redriven
# or removed
resource "aws_cloudwatch_metric_alarm" "vends_parked" {
  alarm_name          = "tower-vends-parked"
  alarm_description   = "a vend the queue gave up on is parked on tower-vends-failed; read it, then redrive it or remove it"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.vends_failed.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
  ok_actions          = [aws_sns_topic.ops_alerts.arn]
}

output "vends_queue_url" {
  value = aws_sqs_queue.vends.url
}
