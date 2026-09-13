# ─── self-continuation — the agent grants itself another turn (TODO.md design note) ───
#
# The container's continue_later tool writes a PER-SESSION baton under one prefix (concurrent
# sessions in a warm threaded container must never share a baton); this S3 notification fires
# continue_poke, which reads the notified key and re-invokes the runtime on the SAME session
# after the turn ends. The owner's budget (GERP#continuation_max_turns settings row, set by
# asking the agent) is enforced HERE in code — the poke refuses past the cap, so a confused
# agent rewriting batons forever can't loop. One prefix, one door.

locals {
  continue_prefix = "state/continue/"
}

resource "aws_iam_role" "continue_poke" {
  name = "${local.resource_prefix_snake}_continue_poke"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "continue_poke" {
  name = "${local.resource_prefix_snake}_continue_poke"
  role = aws_iam_role.continue_poke.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # read the batons (the one prefix), nothing else in the cabinet
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.uploads_bucket_arn}/${local.continue_prefix}*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.uploads_kms_key_arn
      },
      {
        # the owner's budget knob
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
      },
      {
        # wake this gerp's own agent runtime (same account — the inbox poke_agent shape)
        Effect = "Allow"
        Action = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [
          "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:runtime/*",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:runtime-endpoint/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "continue_poke" {
  source = "../../terraform/lambda"

  name            = "${local.resource_prefix_snake}_continue_poke"
  role            = aws_iam_role.continue_poke.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/agent/lambdas/continue_poke.zip"
  src_dir         = "modules/agent/lambdas/continue_poke"
  gerp_id         = var.gerp_id
  timeout         = 120
  env_vars = {
    AGENT_RUNTIME_ENDPOINT_ARN = aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn
    UPLOADS_BUCKET             = var.uploads_bucket
    SETTINGS_TABLE             = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
    GERP_ID                    = var.gerp_id
    DEFAULT_MAX_TURNS          = "3"
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.continue_poke
  to   = module.continue_poke.aws_lambda_function.this
}


resource "aws_lambda_permission" "continue_poke_s3" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowS3BatonInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.continue_poke.name
  principal           = "s3.amazonaws.com"
  source_arn          = var.uploads_bucket_arn
}

# The uploads bucket's ONE notification config (a bucket singleton — additional watchers on
# this bucket must be added here, not in a second resource).
resource "aws_s3_bucket_notification" "uploads" {
  bucket = var.uploads_bucket
  lambda_function {
    lambda_function_arn = module.continue_poke.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = local.continue_prefix # per-session batons live under here
  }
  depends_on = [aws_lambda_permission.continue_poke_s3]
}

