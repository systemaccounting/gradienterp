# One DynamoDB stream mapping, and what every mapping owns: the record-level retry and the queue a
# record lands on when it keeps failing. The handler returns `batchItemFailures` (`aws.stream_batch`),
# so one bad record retries alone — bisect splits the batch, `retries` bounds it, and the queue is
# where the record goes instead of blocking the shard for 24 hours. The queue's depth is the alarm
# ("a record the stream gave up on"); a person reads the record off it.

resource "aws_sqs_queue" "failed" {
  name                      = "${var.name}-failed"
  message_retention_seconds = 1209600 # 14 days, the maximum: a parked record waits for a person
}

resource "aws_lambda_event_source_mapping" "this" {
  event_source_arn                   = var.stream_arn
  function_name                      = var.function_arn
  starting_position                  = var.starting_position
  batch_size                         = var.batch_size
  maximum_batching_window_in_seconds = var.batching_window
  maximum_retry_attempts             = var.retries
  bisect_batch_on_function_error     = true
  function_response_types            = ["ReportBatchItemFailures"]

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.failed.arn
    }
  }

  dynamic "filter_criteria" {
    for_each = length(var.filter_patterns) > 0 ? [1] : []
    content {
      dynamic "filter" {
        for_each = var.filter_patterns
        content {
          pattern = filter.value
        }
      }
    }
  }
}

# the mapping sends to the queue as the function's role
resource "aws_iam_role_policy" "failed" {
  name = "${var.name}-failed"
  role = var.role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = aws_sqs_queue.failed.arn
    }]
  })
}

# a record the stream gave up on is waiting on the queue; the alarm stays in ALARM until it is
# redriven or removed — it is not done until it is done
resource "aws_cloudwatch_metric_alarm" "parked" {
  count               = var.ops_alerts_topic_arn != "" ? 1 : 0
  alarm_name          = "${var.name}-parked"
  alarm_description   = "a stream record ${var.name} gave up on is parked on ${aws_sqs_queue.failed.name}; read it and redrive or remove it"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.failed.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.ops_alerts_topic_arn]
  ok_actions          = [var.ops_alerts_topic_arn]
}
