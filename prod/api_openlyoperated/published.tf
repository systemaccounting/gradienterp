###############################################
# published — the gerp's openly_operated flip, mirrored onto its gerp-customers row.
#
#   gerp settings toggle ──publish──▶ shared bus ──rule (gerp.published|gerp.unpublished)──▶ this lambda
#   ──UpdateItem──▶ gerp-customers.published
#
# The directory, the read api and the stream's publisher read that one bit instead of asking a
# gerp per request. Provisioning stamps the create-time wish first.
###############################################

data "archive_file" "published" {
  type        = "zip"
  output_path = "${path.module}/.build/published.zip"

  source {
    content  = file("${path.module}/lambdas/published/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/../../modules/aws/aws.py")
    filename = "aws.py"
  }
}

resource "aws_iam_role" "published" {
  name = "${local.stack_prefix}-published"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "published" {
  name = "${local.stack_prefix}-published"
  role = aws_iam_role.published.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${local.operator_account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:*"
      },
    ]
  })
}

module "published" {
  source = "../../modules/terraform/lambda"

  name               = "${local.stack_prefix}-published"
  role               = aws_iam_role.published.arn
  filename           = data.archive_file.published.output_path
  source_code_hash   = data.archive_file.published.output_base64sha256
  src_dir            = "prod/api_openlyoperated/lambdas/published"
  timeout            = 10
  env_vars           = { CUSTOMERS_TABLE = "${local.stack_prefix}-customers" }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.published
  to   = module.published.aws_lambda_function.this
}


resource "aws_cloudwatch_event_rule" "published" {
  name           = "${local.stack_prefix}-published"
  description    = "A gerp's openly_operated flip, onto its row"
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  event_pattern  = jsonencode({ "detail-type" = ["gerp.published", "gerp.unpublished"] })
}

resource "aws_cloudwatch_event_target" "published" {
  rule           = aws_cloudwatch_event_rule.published.name
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  target_id      = "published"
  arn            = module.published.arn
}

resource "aws_lambda_permission" "published_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.published.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.published.arn
}
