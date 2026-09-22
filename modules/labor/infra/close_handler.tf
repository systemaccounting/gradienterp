# labor close-handler — the module's one compute.
#
# Trigger: a DIRECT DynamoDB-stream → Lambda event-source-mapping off
# `time-entries`, with a stream filter so the handler only wakes on a close
# (status=closed in the NewImage). Chosen over a DDB-stream → EventBridge Pipe →
# lambda hop because:
#   - the consumer is in-module (labor's own handler) — there is no cross-module
#     fan-out to justify putting the event on the bus. (When pay-period / other
#     subscribers appear, add a Pipe emitting `labor.time_entry_closed`; the
#     stream stays the source of truth and this ESM is unaffected.)
#   - filtering at the ESM means the close-only predicate runs before the lambda
#     is even invoked — no per-record bus cost, no idle invocations on opens.
#   - it's one resource, no Pipe role / bus rule / target plumbing.
# This is the repo's first DDB-stream → handler wire; the Pipe path stays the
# documented next step for genuine fan-out (via contacts outputs).
#
# References the shared scaffold resources by name (the scaffold agent owns the
# table + vars): aws_dynamodb_table.time_entries.stream_arn,
# aws_dynamodb_table.worker, var.post_journal_entry_fn_{arn,name}.

locals {
  close_handler_name = "${local.prefix}-close-handler"
}

# ─── package ───

# ─── iam ───

resource "aws_iam_role" "close_handler" {
  name = local.close_handler_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "close_handler" {
  name = local.close_handler_name
  role = aws_iam_role.close_handler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # read the worker rate book at close
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = aws_dynamodb_table.worker.arn
      },
      {
        # the rule instances attached to CLOSE_SHIFT#<contact_id> — the wage accrual. Nothing
        # attached, nothing accrues: the handler doesn't know what an accrual is.
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}"
      },
      {
        # consume the time-entries stream
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeStream",
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:ListStreams",
        ]
        Resource = aws_dynamodb_table.time_entries.stream_arn
      },
      {
        # invoke accounting's post_journal_entry
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
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

module "close_handler" {
  source = "../../terraform/lambda"

  name            = local.close_handler_name
  role            = aws_iam_role.close_handler.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/labor/lambdas/close_handler.zip"
  src_dir         = "modules/labor/lambdas/close_handler"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    WORKER_TABLE          = aws_dynamodb_table.worker.name
    RULE_INSTANCES_TABLE  = var.rule_instances_table_name
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    CUSTOMER_ID           = var.gerp_id
    INTERNAL_BUS_NAME     = var.internal_bus_name # a record_metric row announces a product event here (modules/metrics)
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.close_handler
  to   = module.close_handler.aws_lambda_function.this
}


# ─── stream → lambda ───
#
# Filter: only deliver records whose NewImage has status=closed. The handler
# still re-checks the old→new transition (an INSERT already-closed, or a MODIFY
# that wasn't closed before) so a replayed/edited closed row doesn't double-post.

module "close_handler_stream" {
  source               = "../../terraform/stream"
  name                 = "${local.prefix}-close_handler"
  stream_arn           = aws_dynamodb_table.time_entries.stream_arn
  function_arn         = module.close_handler.arn
  role_name            = aws_iam_role.close_handler.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
  filter_patterns = [jsonencode({
    eventName = ["INSERT", "MODIFY"]
    dynamodb  = { NewImage = { status = { S = ["closed"] } } }
  })]
}

moved {
  from = aws_lambda_event_source_mapping.close_handler
  to   = module.close_handler_stream.aws_lambda_event_source_mapping.this
}

# ─── outputs ───

output "close_handler_fn_name" {
  value = module.close_handler.name
}

output "close_handler_fn_arn" {
  value = module.close_handler.arn
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

# a firm's record_metric row on this module's callsites announces on the firm's OWN bus
# (modules/metrics)
resource "aws_iam_role_policy" "close-handler-internal-bus" {
  name = "${local.prefix}-close-handler-internal-bus"
  role = aws_iam_role.close_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = var.internal_bus_arn
    }]
  })
}
