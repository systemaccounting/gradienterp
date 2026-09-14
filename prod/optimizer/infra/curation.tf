# ─── standards-corpus curation (weekly) ───
#
# EventBridge Scheduler → curate_invoke lambda → the hub runtime (curator mode). The hub sweeps
# `_contrib/`, verifies + cross-checks contributions, and promotes to root — the corpus's single
# root writer. The scheduled-poke shape is modules/schemas' canonical-pull.

data "archive_file" "curate_invoke" {
  type        = "zip"
  output_path = "${path.module}/.build/curate_invoke.zip"
  source {
    content  = file("${path.module}/../lambdas/curate_invoke/main.py")
    filename = "main.py"
  }
}

resource "aws_iam_role" "curate_invoke" {
  name = "${local.prefix}-curate-invoke"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "curate_invoke" {
  name = "${local.prefix}-curate-invoke"
  role = aws_iam_role.curate_invoke.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = "${aws_bedrockagentcore_agent_runtime.hub.agent_runtime_arn}*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:*"
      },
    ]
  })
}

module "curate_invoke" {
  source = "../../../modules/terraform/lambda"

  name             = "${local.prefix}-curate_invoke"
  role             = aws_iam_role.curate_invoke.arn
  filename         = data.archive_file.curate_invoke.output_path
  source_code_hash = data.archive_file.curate_invoke.output_base64sha256
  src_dir          = "prod/optimizer/lambdas/curate_invoke"
  timeout          = 900
  env_vars = {
    HUB_RUNTIME_ENDPOINT_ARN = aws_bedrockagentcore_agent_runtime_endpoint.hub.agent_runtime_endpoint_arn
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.curate_invoke
  to   = module.curate_invoke.aws_lambda_function.this
}


resource "aws_iam_role" "curation_scheduler" {
  name = "${local.prefix}-curation-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "curation_scheduler" {
  name = "invoke-curate"
  role = aws_iam_role.curation_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = module.curate_invoke.arn
    }]
  })
}

resource "aws_scheduler_schedule" "curation" {
  name                         = "${local.prefix}-standards-curation"
  schedule_expression          = "rate(7 days)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = module.curate_invoke.arn
    role_arn = aws_iam_role.curation_scheduler.arn
  }
}
