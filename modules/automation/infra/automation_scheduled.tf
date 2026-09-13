# scheduling — manage_automation (op: schedule) subscribes to `automation.scheduled` on the firm's own bus.
#
# The sibling of automation_requested.tf. A row carrying `add_scheduled_automation`
# (`modules/rules/dispatch_rules.py`) sends it from inside whichever callsite lambda the row is
# attached to, and the callsite gains no scheduler grant by having it: the announcement is all it
# makes, and the module that owns scheduling is what creates the schedule. That is also what keeps
# the approved-script check on the one path — a scheduled script is read out of `approved/` here
# exactly as a hand-scheduled one is.

resource "aws_cloudwatch_event_rule" "automation_scheduled" {
  count          = var.internal_bus_name == "" ? 0 : 1
  name           = "${local.prefix}-scheduled"
  description    = "A firm's callsite rule asked for one of its own scripts LATER -> manage_automation (op: schedule)."
  event_bus_name = var.internal_bus_name
  event_pattern = jsonencode({
    source        = ["rules"]
    "detail-type" = ["automation.scheduled"]
  })
}

resource "aws_cloudwatch_event_target" "schedule_automation" {
  count          = var.internal_bus_name == "" ? 0 : 1
  rule           = aws_cloudwatch_event_rule.automation_scheduled[0].name
  event_bus_name = var.internal_bus_name
  arn            = module.fn["manage_automation"].arn

  # The detail is the schedule op's whole payload — {script, schedule_expression, subject, params,
  # rules, one_shot, start_date, end_date}. A transformer cannot splice `op` INTO the detail, so it
  # wraps: the handler unwraps {"op": "schedule", "request": <detail>} in one line.
  input_transformer {
    input_paths    = { detail = "$.detail" }
    input_template = "{\"op\": \"schedule\", \"request\": <detail>}"
  }

  dead_letter_config { arn = aws_sqs_queue.undispatched[0].arn }
}

resource "aws_lambda_permission" "events_invoke_schedule_automation" {
  count = var.internal_bus_name == "" ? 0 : 1
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAutomationScheduledInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["manage_automation"].name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.automation_scheduled[0].arn
}

# A refusal here is the interesting case and it is not a delivery failure: a duplicate name (two
# records reaching the same status with no `names_it` to tell them apart) or a script that is not
# approved both return an error the handler chose. Those land in the function's own log as the
# outcome line `create_inc_from_log` files on. What reaches this queue is narrower — the handler
# never ran, or died before it could answer.
resource "aws_lambda_function_event_invoke_config" "schedule_automation" {
  count                        = var.internal_bus_name == "" ? 0 : 1
  function_name                = module.fn["manage_automation"].name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600

  destination_config {
    on_failure { destination = aws_sqs_queue.undispatched[0].arn }
  }
}

resource "aws_iam_role_policy" "dispatch_scheduled" {
  count = var.internal_bus_name == "" ? 0 : 1
  name  = "${local.prefix}-dispatch-scheduled"
  role  = aws_iam_role.schedules.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = aws_sqs_queue.undispatched[0].arn
    }]
  })
}
