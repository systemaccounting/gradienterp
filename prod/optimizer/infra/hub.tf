############################################
# The hub agent — operator-account AgentCore runtime, broker mode.
#
# Reuses the shared operator agent image (prod/tower/agent_image.tf's `agentcore` ECR) in a
# GATEWAY-LESS configuration: its only tools are the in-process find_profiles (invokes the reader
# lambda) + ask_spoke (resolves a gerp's runtime_endpoint_arn off gerp-customers, then
# InvokeAgentRuntime). AGENT_MODE=broker loads prompts/broker.md; no GATEWAY_URL → the engine skips
# MCP. Same account as the ECR (local image pull); reaches every spoke cross-account via SigV4.
#
# The real trust gate is spoke-side: each spoke's runtime resource policy grants THIS hub role
# (added in prod/per_customer). The hub's identity policy below allows InvokeAgentRuntime on
# runtime/* — a spoke that never granted the hub still rejects it. Org-scoping the identity side is
# a hardening follow-up (prod/optimizer/TODO.md § security).
############################################

locals {
  hub_model_id  = "us.anthropic.claude-sonnet-4-6"      # proven in-org; bump to opus for cross-firm judgment later
  hub_image_tag = "v69"                                 # + standards-corpus tools (curator mode) — build/push before applying this
  hub_name      = "${local.stack_prefix}_optimizer_hub" # runtime name regex: letter-start, underscores, ≤48
}

resource "aws_cloudwatch_log_group" "hub" {
  name              = "/aws/bedrockagentcore/runtime/${local.prefix}-hub"
  retention_in_days = 30
}

# ─── execution role ───

resource "aws_iam_role" "hub" {
  name = "${local.prefix}-hub"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guard — AgentCore Runtime fails AssumeRole without both conditions.
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.operator_account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${local.operator_account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "hub" {
  name = "${local.prefix}-hub"
  role = aws_iam_role.hub.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Invoke the Bedrock model; auto-subscribe to the model agreement on first invoke
        # (the operator account's own agreement — the org FTU form covers approval). The operator
        # account has never used Bedrock, so it completes the Marketplace subscription on first
        # invoke — which needs ViewSubscriptions alongside Subscribe.
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"]
        Resource = "*"
      },
      {
        # find_profiles tool → the registry reader lambda.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = module.find_profiles.arn
      },
      {
        # ask_spoke resolves a gerp's runtime_endpoint_arn off the instance registry.
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = local.customers_table_arn
      },
      {
        # ask_spoke talks to spokes. Identity side is broad (runtime/* covers a runtime AND its
        # endpoint ARNs — invoke authorizes against the endpoint); the spoke-side resource policy on
        # each endpoint is the real gate (per_customer grants only this role). Org-scope is a TODO.
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:*:runtime/*"
      },
      {
        # Curator of the shared standards corpus (same-account bucket): reads everything
        # including _contrib, writes root. Tenants can only write their own _contrib prefix
        # (bucket policy); this role is the single root writer.
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${local.stack_prefix}-standards-${local.operator_account_id}",
          "arn:aws:s3:::${local.stack_prefix}-standards-${local.operator_account_id}/*",
        ]
      },
      {
        # Pull the shared agent image (same account as the ECR).
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = "arn:aws:ecr:${data.aws_region.current.region}:${local.operator_account_id}:repository/agentcore"
      },
      {
        # AgentCore delivers runtime logs to a service-managed group named after the runtime
        # (gerp_optimizer_hub), not the hyphenated one below — so allow create+write across the
        # runtime log-group namespace, else log delivery silently fails.
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:log-group:/aws/bedrockagentcore/runtime/*"
      },
    ]
  })
}

# ─── runtime + endpoint ───

resource "aws_bedrockagentcore_agent_runtime" "hub" {
  agent_runtime_name = local.hub_name
  description        = "gradientERP coordination hub — cross-firm matchmaker (broker mode)"
  role_arn           = aws_iam_role.hub.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${local.operator_account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com/agentcore:${local.hub_image_tag}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = {
    AWS_REGION         = data.aws_region.current.region
    AWS_DEFAULT_REGION = data.aws_region.current.region
    AGENT_MODE         = "broker" # loads prompts/broker.md; not owner-facing (no integrations fragment)
    BUSINESS_NAME      = "the gradientERP hub"

    # StrandsEngine on the model alone — no GATEWAY_URL, so the engine runs gateway-less
    # (in-process tools only). A FileSessionManager under /tmp holds each request's session.
    BEDROCK_MODEL_ID = local.hub_model_id
    SESSIONS_DIR     = "/tmp/hub-sessions"

    # the two coordination tools (gated on these being set):
    FIND_PROFILES_FN = module.find_profiles.name # find_profiles
    CUSTOMERS_TABLE  = local.customers_table     # ask_spoke resolves endpoints here

    # the hub is the compliance-corpus CURATOR: it sees _contrib and its contribute writes ROOT
    # (the corpus's only root writer). The weekly curation schedule pokes it (curation.tf).
    STANDARDS_BUCKET     = "${local.stack_prefix}-standards-${local.operator_account_id}"
    STANDARDS_WRITE_ROOT = "1"
  }

  tags = {
    module = "optimizer"
    role   = "hub"
  }

  depends_on = [
    aws_iam_role_policy.hub,
    aws_cloudwatch_log_group.hub,
  ]
}

resource "aws_bedrockagentcore_agent_runtime_endpoint" "hub" {
  name             = "${local.hub_name}_endpoint"
  agent_runtime_id = aws_bedrockagentcore_agent_runtime.hub.agent_runtime_id
  # AgentCore does NOT auto-track latest — pin the endpoint to the current runtime version.
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.hub.agent_runtime_version
}

# ─── inbound trust: any spoke in the org may InvokeAgentRuntime on the hub (ask_hub) ───
#
# Cross-account InvokeAgentRuntime with a qualifier authorizes against BOTH the runtime arn AND the
# endpoint arn, and put-resource-policy requires the policy Resource to EXACTLY match resource_arn —
# so one policy per arn. Principal "*" scoped by aws:PrincipalOrgID keeps it to org spokes without a
# per-spoke update (spokes come and go; the hub grant is set once). The spoke side is the reciprocal,
# specific grant (modules/agent grants only this hub role on the spoke's own arns).
locals {
  hub_arns    = [aws_bedrockagentcore_agent_runtime.hub.agent_runtime_arn, aws_bedrockagentcore_agent_runtime_endpoint.hub.agent_runtime_endpoint_arn]
  hub_arn_key = { runtime = 0, endpoint = 1 }
}

resource "aws_bedrockagentcore_resource_policy" "hub_inbound" {
  for_each     = local.hub_arn_key
  resource_arn = local.hub_arns[each.value]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowOrgSpokesAskHub"
      Effect    = "Allow"
      Principal = { AWS = "*" }
      Action    = "bedrock-agentcore:InvokeAgentRuntime"
      Resource  = local.hub_arns[each.value]
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })
}

# ─── outputs (spokes consume these in prod/per_customer) ───

output "hub_runtime_endpoint_arn" {
  description = "The hub's runtime endpoint ARN — spokes set it as HUB_RUNTIME_ENDPOINT_ARN for the ask_hub tool."
  value       = aws_bedrockagentcore_agent_runtime_endpoint.hub.agent_runtime_endpoint_arn
}

output "hub_role_arn" {
  description = "The hub execution role — each spoke's runtime resource policy grants InvokeAgentRuntime to this principal."
  value       = aws_iam_role.hub.arn
}
