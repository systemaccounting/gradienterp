# dispatch — automate subscribes to `automation.requested` on the firm's own bus.
#
# A firm's rule instance carrying `run_automation` sends it (`modules/rules/automation_rules.py`), from
# inside whichever callsite lambda the row is attached to. Nothing here changes when a firm attaches
# a script to a new callsite, moves it, or turns it off — the row is the whole configuration.
#
# ONE rule, matching two literals. The source is the rules engine rather than the emitting module,
# and the callsite rides in the detail, so a module gaining a react callsite needs no edit here.

resource "aws_cloudwatch_event_rule" "automation_requested" {
  count          = var.internal_bus_name == "" ? 0 : 1
  name           = "${local.prefix}-requested"
  description    = "A firm's callsite rule asked for one of its own scripts -> automate."
  event_bus_name = var.internal_bus_name
  event_pattern = jsonencode({
    source        = ["rules"]
    "detail-type" = ["automation.requested"]
  })
}

resource "aws_cloudwatch_event_target" "automate" {
  count          = var.internal_bus_name == "" ? 0 : 1
  rule           = aws_cloudwatch_event_rule.automation_requested[0].name
  event_bus_name = var.internal_bus_name
  arn            = module.fn["automate"].arn

  # The detail IS automate's payload — {script, params, rules} is exactly what its handler reads, so
  # there is no transformer and no shape lambda between the two.
  input_path = "$.detail"

  dead_letter_config { arn = aws_sqs_queue.undispatched[0].arn }
}

resource "aws_lambda_permission" "events_invoke_automate" {
  count = var.internal_bus_name == "" ? 0 : 1
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated statement
  # id lets the new grant exist before the old is removed; a call landing in that gap would be a bare
  # 403, and EventBridge does not retry those.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAutomationRequestedInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["automate"].name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.automation_requested[0].arn
}

# ─── the durable half ───
#
# EventBridge invokes a Lambda target ASYNCHRONOUSLY, so the rule's DLQ above covers delivery only. A
# handler that THROWS gets Lambda's two async retries and is then discarded unless the function has
# an on-failure destination. `automate` catches a script's own failure and logs it as an incident, so
# what lands here is the narrower case: the runner itself never got to run, or died before it could.

resource "aws_lambda_function_event_invoke_config" "automate" {
  count                  = var.internal_bus_name == "" ? 0 : 1
  function_name          = module.fn["automate"].name
  maximum_retry_attempts = 2
  # A script reacting to something that happened an hour ago is still worth running; a day later the
  # moment it was about has usually moved.
  maximum_event_age_in_seconds = 3600

  destination_config {
    on_failure { destination = aws_sqs_queue.undispatched[0].arn }
  }
}

resource "aws_sqs_queue" "undispatched" {
  count = var.internal_bus_name == "" ? 0 : 1
  name  = "${local.prefix}-undispatched"
  # A message here is a moment a firm asked to handle and nothing did — the invoice that issued and
  # never got its notice. Forensic, so it outlives the weekend it happened on.
  message_retention_seconds = 1209600 # 14 days, the maximum
  sqs_managed_sse_enabled   = true
}

resource "aws_iam_role_policy" "dispatch" {
  count = var.internal_bus_name == "" ? 0 : 1
  name  = "${local.prefix}-dispatch"
  role  = aws_iam_role.automate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # Lambda writes the failed-invocation record as the FUNCTION, so the grant is on its role.
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = aws_sqs_queue.undispatched[0].arn
    }]
  })
}

resource "aws_sqs_queue_policy" "undispatched" {
  count     = var.internal_bus_name == "" ? 0 : 1
  queue_url = aws_sqs_queue.undispatched[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # ...and EventBridge writes the undeliverable ones as itself, scoped to the one rule.
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.undispatched[0].arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.automation_requested[0].arn } }
    }]
  })
}

variable "internal_bus_name" {
  description = "Name of the firm's OWN event bus (modules/events). Empty = this gerp has no bus, so a callsite cannot hand off to a script and none of this is created."
  type        = string
  default     = ""
}
