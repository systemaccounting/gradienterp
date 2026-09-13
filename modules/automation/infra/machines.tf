# The machines kind: a reviewed ASL definition, deployed to Step Functions by the agent.
#
# Nothing here runs firm code. `automate` and `modules/cmd` execute scripts we hold; a machine is a
# definition HANDED to AWS, and what it may touch is decided by an execution role terraform writes.
# So the boundary is the same one the script kinds use — a role, not a check.

# ─── the role a machine runs as ───

resource "aws_iam_role" "machine" {
  name = "${local.prefix}-machine"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "machine" {
  name = "${local.prefix}-machine"
  role = aws_iam_role.machine.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The surface a script already reaches: this firm's own module lambdas, and `automate` for
        # a step that wants real code. A Task naming anything else is a Task the account refuses —
        # AccessDenied is the answer, so there is no allowlist to maintain and none to forget.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-*-${local.gerp}-*"
      },
      {
        # Step Functions delivers logs through the log-delivery API, none of which takes a resource
        # ARN — `PutResourcePolicy` on "*" reads alarming and is not optional. Without these the
        # machine's logging configuration is silently refused at create time.
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
    ]
    # deliberately absent: ssm (one GetParameter on the secrets path and a machine reads the vault),
    # any iam:*, and states:* — without that last one a running execution could create machines and
    # deploy machines from inside a running execution, going around the review that gates them.
  })
}

# One group for every machine. They are created at RUNTIME by the tool, so terraform cannot make a
# group per machine and each machine's logging configuration points here.
resource "aws_cloudwatch_log_group" "machines" {
  name              = "/aws/vendedlogs/states/${local.prefix}"
  retention_in_days = 90
}

# ─── the role the TOOL runs as ───
#
# Holds `states:` for this firm's own machines and `iam:PassRole` for the execution role above.
# What keeps it the ONLY path to a deployed machine is not a policy on this role but the prefix it
# reads from: unreviewed bytes cannot reach `approved/machines/` (bucket policy, refuses admin), and
# the tool takes no definition in its payload.

resource "aws_iam_role" "sfn" {
  name = "${local.prefix}-sfn"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "sfn" {
  name = "${local.prefix}-sfn"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Scoped to this firm's own machines by name. `list_state_machines` takes no resource, so it
        # is split out below.
        Effect = "Allow"
        Action = [
          "states:CreateStateMachine", "states:UpdateStateMachine", "states:DeleteStateMachine",
          "states:DescribeStateMachine", "states:StartExecution", "states:ListExecutions",
        ]
        Resource = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.prefix}-*"
      },
      {
        Effect = "Allow"
        Action = ["states:DescribeExecution", "states:StopExecution", "states:RedriveExecution",
        "states:GetExecutionHistory"]
        Resource = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:execution:${local.prefix}-*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["states:ListStateMachines", "sts:GetCallerIdentity"]
        Resource = "*"
      },
      {
        # CreateStateMachine hands the execution role to Step Functions, and handing a role is a
        # grant of its own. Scoped to the one role, so this cannot pass a more powerful one.
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.machine.arn
        Condition = { StringEquals = { "iam:PassedToService" = "states.amazonaws.com" } }
      },
      {
        # the approved definitions, and nothing else under automations/ — the same read-permission
        # gate the script runners use, which is why `create` takes no bytes in its payload
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.approved_prefix}machines/*"
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        # the on-failure destination is written by the FUNCTION's role, not by EventBridge — without
        # this the invoke config itself is refused at apply time
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.machine_status_dlq.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── a machine that fails tells someone ───
#
# A SCRIPT failing inside `automate` is logged and the incident path picks it up. A failure in the
# DEFINITION — no matching `Choice`, a `States.Runtime` error, a Task on a Resource the execution
# role refuses — touches no lambda of ours, so until this existed it told nobody. Observed on a
# probe: failed, redriven, failed again, and the incident path saw neither.
#
# Step Functions emits to the DEFAULT bus in the account the machine runs in, so this is one rule in
# the customer's own account covering every machine the firm ever deploys. None of the cross-account
# problem in `modules/events` applies — the operator bus is not involved.

resource "aws_cloudwatch_event_rule" "machine_status" {
  name        = "${local.prefix}-machine-status"
  description = "A state machine execution ended badly -> machine_failed."
  event_pattern = jsonencode({
    source        = ["aws.states"]
    "detail-type" = ["Step Functions Execution Status Change"]
    detail = {
      # NOT "ABORTED": a stop is always somebody's decision — `manage_machines delete`
      # retiring an automation, or a person in the console — and retiring one would otherwise open
      # an incident per execution it stopped.
      status = ["FAILED", "TIMED_OUT"]
      # this firm's machines only. The bus is the account's, and an account hosting one gerp today
      # is not a reason to report on anything that appears beside it.
      stateMachineArn = [{ prefix = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.prefix}-" }]
    }
  })
}

resource "aws_cloudwatch_event_target" "machine_failed" {
  rule = aws_cloudwatch_event_rule.machine_status.name
  arn  = module.fn["machine_failed"].arn

  # No input transformer: the whole event is wanted, because the handler falls back to
  # DescribeExecution when the event carries no cause, and a transformer would strip the arn it
  # needs to do that.
  dead_letter_config { arn = aws_sqs_queue.machine_status_dlq.arn }
}

resource "aws_sqs_queue" "machine_status_dlq" {
  name                      = "${local.prefix}-machine-status-dlq"
  message_retention_seconds = 1209600
}

resource "aws_lambda_permission" "events_invoke_machine_failed" {
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowMachineStatusInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["machine_failed"].name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.machine_status.arn
}

# The rule's DLQ covers DELIVERY. A handler that throws gets Lambda's async retries and is then
# discarded without this — and what it would discard is the only notice a firm gets that its
# automation is broken.
resource "aws_lambda_function_event_invoke_config" "machine_failed" {
  function_name                = module.fn["machine_failed"].name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600

  destination_config {
    on_failure { destination = aws_sqs_queue.machine_status_dlq.arn }
  }
}

# The reporter's log group is created by its lambda module (`module.fn["machine_failed"]`); the
# filter below attaches by name, and the module's group exists before the function does.
removed {
  from = aws_cloudwatch_log_group.machine_failed
  lifecycle {
    destroy = false
  }
}

resource "aws_cloudwatch_log_subscription_filter" "machine_failed" {
  name            = "${local.prefix}-machine-outcomes"
  log_group_name  = module.fn["machine_failed"].log_group
  filter_pattern  = "{ $.incident = \"*\" }"
  destination_arn = module.fn["create_inc_from_log"].arn

  depends_on = [aws_lambda_permission.logs_invoke_machine_failed]
}

resource "aws_lambda_permission" "logs_invoke_machine_failed" {
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowMachineOutcomesLogs"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["create_inc_from_log"].name
  principal           = "logs.amazonaws.com"
  source_arn          = "${module.fn["machine_failed"].log_group_arn}:*"
}

# Reads a failure cause and prints. Deliberately NOT the `sfn` role: that one can create state
# machines, and an event-triggered lambda should not carry the grant that deploys them just because
# it reports on them.
resource "aws_iam_role" "machine_reporter" {
  name = "${local.prefix}-machine-reporter"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "machine_reporter" {
  name = "${local.prefix}-machine-reporter"
  role = aws_iam_role.machine_reporter.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "states:DescribeExecution"
        Resource = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:execution:${local.prefix}-*:*"
      },
      {
        # the on-failure destination is written by the FUNCTION's role, not by EventBridge — without
        # this the invoke config itself is refused at apply time
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.machine_status_dlq.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# `manage_automation` reports whether an approved machine is DEPLOYED (op: list), and starts one
# on a schedule (op: schedule). Approval and deployment are two acts for this kind, so a list that
# cannot tell them apart is a list an owner cannot act on.
resource "aws_iam_role_policy" "read_machines" {
  name = "${local.prefix}-read-machines"
  role = aws_iam_role.schedules.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:ListStateMachines", Resource = "*" }]
  })
}

resource "aws_iam_role_policy" "scheduler_starts_machines" {
  name = "${local.prefix}-scheduler-machines"
  role = aws_iam_role.scheduler_target.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = "arn:aws:states:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.prefix}-*"
    }]
  })
}
