############################################
# Web chat — the gerp's own human front door to its agent.
#
# A Lambda behind a Function URL (AuthType=NONE), served from THIS account. The
# runtime lives here too, so the invoke is same-account: the chat lambda reads the
# runtime endpoint ARN as an in-module reference (NOT the agent_runtime_endpoint_arn
# var the consumer modules — inbox/calendar — thread in). That asymmetry is the tell:
# consumers poke the agent incidentally; the chat IS the agent's front door.
#
# The Function URL is public, so the lambda is the auth gate — it validates the
# operator-pool JWT itself (identity) and resolves role from this gerp's own records
# (owner via a local SSM stash; employee/customer/vendor via a contacts row). See
# modules/agent/lambdas/chat/main.py + modules/agent/TODO.md "web chat".
#
# Gated on cognito being wired (count): a standalone agent apply with no pool stays
# clean rather than provisioning an auth-less front door.
############################################

locals {
  chat_enabled  = var.cognito_user_pool_id != "" ? 1 : 0
  chat_name     = "${local.resource_prefix_dns}-chat"
  owner_sub_ssm = "/gradienterp/customers/${var.gerp_id}/owner_sub"
  # DDB tables follow the cross-module convention ${stack_prefix}-<module>-<gerp>-<resource>
  # (gerp-accounting-…-ledger, gerp-labor-…-worker). The agentcore-* prefix is reserved for
  # AgentCore-native resources (runtime/memory/gateway) whose name regexes require it.
  chats_table = "${var.stack_prefix}-agent-${replace(var.gerp_id, "_", "-")}-chats"
}

# Node lambda — zips the whole dir (index.mjs + index.html + node_modules). The
# agentcore SDK client is bundled (npm install must run before apply: locally by
# hand, in CI by the per_customer buildspec). Node because Lambda streams natively
# here (RESPONSE_STREAM Function URL); no Web Adapter. node_modules is the SDK clients.
# Per-user saved-chat index. The conversation events live in AgentCore Memory; this table
# just maps account_id → sessions (title + recency) so the client can list, sort, and title
# saved chats — and keep the list even after Memory's retention window expires the events.
# Also the access-control gate: the lambda only replays a session the caller has a row for.
resource "aws_dynamodb_table" "chats" {
  count        = local.chat_enabled
  name         = local.chats_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "account_id"
  range_key    = "session_id"

  attribute {
    name = "account_id"
    type = "S"
  }
  attribute {
    name = "session_id"
    type = "S"
  }

  tags = { gerp_id = var.gerp_id, module = "agent", "gerp:layer" = "session" }
}

resource "aws_iam_role" "chat" {
  count = local.chat_enabled
  name  = "${local.resource_prefix_snake}_chat"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = { gerp_id = var.gerp_id, module = "agent" }
}

resource "aws_iam_role_policy" "chat" {
  count = local.chat_enabled
  name  = "${local.resource_prefix_snake}_chat"
  role  = aws_iam_role.chat[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # invoke this gerp's own agent runtime (same account)
        Effect = "Allow"
        Action = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [
          aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn,
          aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn,
        ]
      },
      {
        # resolve a caller's role from a linked contact (Query the account-index)
        Effect = "Allow"
        Action = ["dynamodb:Query"]
        Resource = [
          "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.contacts_table_name}",
          "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.contacts_table_name}/index/account-index",
        ]
      },
      {
        # read the owner's account_id, stashed locally at provision
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter${local.owner_sub_ssm}"
      },
      {
        # render_frame: discover the sink lambdas (tagged agent_frame_sink) and invoke the one the
        # agent named with the user's values (which never pass through the agent). The tag is the
        # discovery marker; the code only invokes what the tag query returns.
        Effect   = "Allow"
        Action   = "tag:GetResources"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-*"
      },
      {
        # saved-chat index — list / upsert / delete the caller's own chat rows
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.chats[0].arn
      },
      {
        # saved chats: replay (ListEvents) + hard-delete (DeleteEvent) a chat's conversation events
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:ListEvents", "bedrock-agentcore:DeleteEvent"]
        Resource = aws_bedrockagentcore_memory.memory.arn
      },
      {
        # hard-delete also purges the S3SessionManager session (list under the prefix, then
        # batch-delete) so a deleted chat's full history + checkpoint is gone, not TTL-hidden
        Effect = "Allow"
        Action = ["s3:ListBucket", "s3:DeleteObject"]
        Resource = [
          aws_s3_bucket.sessions.arn,
          "${aws_s3_bucket.sessions.arn}/*",
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

module "chat" {
  count  = local.chat_enabled
  source = "../../terraform/lambda"

  name            = local.chat_name
  role            = aws_iam_role.chat[0].arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/agent/lambdas/chat.zip"
  src_dir         = "modules/agent/lambdas/chat"
  gerp_id         = var.gerp_id
  handler         = "index.handler"
  runtime         = "nodejs22.x"
  timeout         = 120 # an agent turn can run 20s+; Function URLs have no 30s API-GW ceiling
  memory          = 256
  env_vars = {
    AGENT_RUNTIME_ENDPOINT_ARN = aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn
    COGNITO_USER_POOL_ID       = var.cognito_user_pool_id
    COGNITO_CLIENT_ID          = var.cognito_client_id
    COGNITO_DOMAIN_PREFIX      = "${var.stack_prefix}-auth" # operator hosted-UI prefix domain
    CONTACTS_TABLE             = var.contacts_table_name
    OWNER_SUB_PARAM            = local.owner_sub_ssm
    CHATS_TABLE                = aws_dynamodb_table.chats[0].name
    MEMORY_ID                  = aws_bedrockagentcore_memory.memory.id
    MEMORY_ACTOR_ID            = "agent-session"                       # must match the container's AgentCoreMemoryStore.ACTOR_ID
    SESSIONS_BUCKET            = aws_s3_bucket.sessions.bucket         # purge the S3 session on chat delete
    BUSINESS_NAME              = try(local.customer.business_name, "") # shown in the chat header instead of the login email
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.chat[0]
  to   = module.chat[0].aws_lambda_function.this
}

# The shareable front door. AuthType=NONE — the lambda does its own JWT auth.
# RESPONSE_STREAM: the handler is awslambda.streamifyResponse, streaming NDJSON
# chunks to the browser (incremental once the container streams; one chunk while buffered).
resource "aws_lambda_function_url" "chat" {
  count              = local.chat_enabled
  function_name      = module.chat[0].name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM"
}

