###############################################
# notify_owner — the one operator lambda that writes to a gerp's owner.
#
#   tower-per-customer SUCCEEDED (TF_ACTION=apply) ──rule──▶ notify_owner {kind: ready} ──SES──▶ the owner
#   any operator process ──invoke {gerp_id, kind}──▶ notify_owner
#
# A message kind is a function in the lambda; the row's `notified_<kind>_at` stamp says it once.
###############################################

resource "aws_iam_role" "notify_owner" {
  name = "tower-notify-owner"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

data "aws_sesv2_email_identity" "sender" {
  email_identity = "gradienterp.cloud"
}

resource "aws_iam_role_policy" "notify_owner" {
  name = "tower-notify-owner"
  role = aws_iam_role.notify_owner.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = data.aws_sesv2_email_identity.sender.arn
      },
      {
        # the onboard gate reads the gerp's own sessions bucket and ledger, in its account;
        # the role assumed must sit in one of the admitted organizations
        Effect    = "Allow"
        Action    = ["sts:AssumeRole"]
        Resource  = "arn:aws:iam::*:role/OperatorOrchestration"
        Condition = { StringEquals = { "aws:ResourceOrgID" = local.org_ids } }
      },
      {
        # the sweep: every active row
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "notify_owner" {
  source = "../../modules/terraform/lambda"

  name            = "tower-notify-owner"
  role            = aws_iam_role.notify_owner.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/notify_owner.zip"
  src_dir         = "prod/tower/lambdas/notify_owner"
  timeout         = 15
  env_vars = {
    CUSTOMERS_TABLE = "${local.stack_prefix}-customers"
    SENDER_EMAIL    = "ops+sender@gradienterp.cloud"
    CONSOLE_URL     = "https://gradienterp.cloud/"
    ONBOARD_AFTER_S = "86400" # a day active with no conversation and no entry
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.notify_owner
  to   = module.notify_owner.aws_lambda_function.this
}

# the daily ask: every active gerp, the `onboard` gate and its stamp decide
resource "aws_cloudwatch_event_rule" "onboard_sweep" {
  name                = "tower-onboard-sweep"
  description         = "Once a day: a gerp active a day with no conversation and no entry is told its agent is waiting"
  schedule_expression = "cron(0 16 * * ? *)" # 09:00 Pacific
}

resource "aws_cloudwatch_event_target" "onboard_sweep" {
  rule      = aws_cloudwatch_event_rule.onboard_sweep.name
  target_id = "notify-owner-onboard"
  arn       = module.notify_owner.arn
  input     = jsonencode({ kind = "onboard" })
}

resource "aws_lambda_permission" "notify_owner_sweep" {
  statement_id  = "AllowOnboardSweep"
  action        = "lambda:InvokeFunction"
  function_name = module.notify_owner.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.onboard_sweep.arn
}

resource "aws_cloudwatch_event_rule" "build_succeeded" {
  name        = "tower-per-customer-succeeded"
  description = "tower-per-customer finished an apply: the owner is told their gerp is ready"
  event_pattern = jsonencode({
    source        = ["aws.codebuild"]
    "detail-type" = ["CodeBuild Build State Change"]
    detail = {
      "project-name" = [aws_codebuild_project.per_customer.name]
      "build-status" = ["SUCCEEDED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "build_succeeded" {
  rule      = aws_cloudwatch_event_rule.build_succeeded.name
  target_id = "notify-owner"
  arn       = module.notify_owner.arn
}

resource "aws_lambda_permission" "notify_owner_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.notify_owner.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.build_succeeded.arn
}
