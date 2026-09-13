# collection — payments subscribes to `collection.requested` on the firm's own bus.
#
# A firm's rule instance on `INVOICE#<status>` (`modules/payments/collection_rules.py`) sends it,
# from inside invoicing's `issue_invoice`. Nothing here changes when a firm turns collection on, off,
# or moves it to a different status.
#
# The per-target `aws_lambda_permission` is a deliberate cost, and a second target module pays it
# again — EventBridge pushes, so the executor names who may push to it. The alternative, a queue per
# target, long-polls at rest and AWS bills the empty receives, per tenant, forever.

resource "aws_cloudwatch_event_rule" "collection_requested" {
  name           = "${local.prefix}-collection-requested"
  description    = "A firm's invoice-transition rule asked for a charge -> charge_saved_method."
  event_bus_name = var.internal_bus_name
  event_pattern = jsonencode({
    source        = ["invoicing"]
    "detail-type" = ["collection.requested"]
  })
}

resource "aws_cloudwatch_event_target" "charge_saved_method" {
  rule           = aws_cloudwatch_event_rule.collection_requested.name
  event_bus_name = var.internal_bus_name
  arn            = module.fn["charge_saved_method"].arn

  # Covers DELIVERY only — see the invoke config below for the half that matters.
  dead_letter_config { arn = aws_sqs_queue.undelivered.arn }
}

resource "aws_lambda_permission" "events_invoke_charge" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated statement
  # id lets the new grant exist before the old is removed; a call landing in that gap would be a bare
  # 403, and EventBridge does not retry those.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowCollectionRequestedInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["charge_saved_method"].name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.collection_requested.arn
}

# ─── the durable half ───
#
# EventBridge invokes a Lambda target ASYNCHRONOUSLY, so the rule's DLQ above covers delivery only.
# A handler that THROWS is Lambda's business: two async retries, not raisable, then discarded unless
# the function has an on-failure destination. Measured on this rule — a deliberately-failing event
# produced three errors, `FailedInvocations` of zero, and an empty EventBridge DLQ.

resource "aws_lambda_function_event_invoke_config" "charge_saved_method" {
  function_name          = module.fn["charge_saved_method"].name
  maximum_retry_attempts = 2
  # An hour-old invoice is still worth collecting; a day-old one wants a person.
  maximum_event_age_in_seconds = 3600

  destination_config {
    on_failure { destination = aws_sqs_queue.undelivered.arn }
  }
}

# One queue per gerp. Both halves above land here, and a second target module shares it.
resource "aws_sqs_queue" "undelivered" {
  name = "${local.prefix}-undelivered"
  # Long, because the value is forensic. A message here is an invoice that was issued and never
  # charged, and the answer to why has to outlive the weekend it happened on.
  message_retention_seconds = 1209600 # 14 days, the maximum
  sqs_managed_sse_enabled   = true
}

resource "aws_iam_role_policy" "collection" {
  name = "${local.prefix}-collection"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # Lambda writes the failed-invocation record as the FUNCTION, so the grant is on its role.
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = aws_sqs_queue.undelivered.arn
    }]
  })
}

resource "aws_sqs_queue_policy" "undelivered" {
  queue_url = aws_sqs_queue.undelivered.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # ...and EventBridge writes the undeliverable ones as itself, scoped to the one rule.
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.undelivered.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.collection_requested.arn } }
    }]
  })
}

output "undelivered_queue_url" {
  description = "Charges that were asked for and never happened. Draining it says which invoices."
  value       = aws_sqs_queue.undelivered.url
}
