############################################
# AgentCore Runtime + Gateway + Memory wiring, per customer.
#
# Auth model (see modules/agent/AGENTS.md):
#   Everything service-to-service is IAM. Cross-account callers (operator
#   tower, phase-7 cross-customer dispatcher) assume a role in the customer
#   sub-account, then invoke the runtime. The container talks to its sibling
#   Gateway using the same agent_execution role it already runs as — no
#   separate service-account credentials, no JWT dance.
#
#   Cognito is user-stuff (owner sign-in via the phase-6 web UI / mobile).
#   It lives in modules/agent/infra/channels.tf when phase 6 lands — NOT
#   here. This file intentionally provisions zero Cognito resources.
#
# Build order (terraform resolves via depends_on):
#   IAM (trust + execution policy, incl. InvokeGateway self-permission)
#     → AgentCore Memory
#       → AgentCore Agent Runtime (references ECR image)
#         → AgentCore Runtime Endpoint
#         → AgentCore Gateway (authorizer_type = AWS_IAM)
#           → one AgentCore Gateway Target per lambda (for_each)
# EventBridge rule for the reporting cron sits parallel; fires the get_statement suite writer.
############################################

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── tenant metadata (SSM lookup, operator-seeded at onboard time) ───
#
# Operator provisions per-tenant config via `aws ssm put-parameter` against
# /gradienterp/customers/<gerp_id> BEFORE `terraform apply` on this
# module. The blob is a JSON object decoded into `local.customer`. Keys this
# module reads: business_name, business_category, owner_email, owner_phone,
# reporting_schedule, policies (see variables.tf for the shape).

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}"
}

# Platform-wide registries — JSON files in modules/schemas/data/ baked into
# the agent container at docker build (Dockerfile COPYs them into
# /app/registries/). No env-var bloat (AgentCore caps env at 4KB; combined
# registries don't fit). Operator-approved registry changes are file edits in
# modules/schemas/data/ + re-build-and-push the agent image.

locals {
  customer = jsondecode(data.aws_ssm_parameter.customer.value)

  # AgentCore name regexes differ per resource:
  #   gateway / gateway_target: ^([0-9a-zA-Z][-]?){1,100}$        (hyphens, no _)
  #   memory:                   ^[a-zA-Z][a-zA-Z0-9_]{0,47}$      (underscores, no -, ≤48 chars)
  #   agent_runtime (observed): same as memory — starts with letter, no hyphens
  # We maintain both forms and pick per-resource.
  resource_prefix_dns   = "agentcore-${replace(var.gerp_id, "_", "-")}"
  resource_prefix_snake = "agentcore_${replace(var.gerp_id, "-", "_")}"
}

############################################
# IAM — execution role for the agent runtime
############################################

data "aws_iam_policy_document" "agent_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    # Confused-deputy guard per AWS docs: AgentCore Gateway (and Runtime) will
    # fail AssumeRole without these SourceAccount/SourceArn conditions.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "agent_execution" {
  name               = "${local.resource_prefix_snake}_execution"
  assume_role_policy = data.aws_iam_policy_document.agent_trust.json

  tags = {
    gerp_id = var.gerp_id
    module  = "agent"
  }
}

data "aws_iam_policy_document" "agent_execution" {
  # invoke Bedrock foundation models (Claude)
  statement {
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"] # scope to specific model arn once we pin the model region/profile
  }

  # the operator gerp's investigator read: gerp-ops-read in any account of the organization
  dynamic "statement" {
    for_each = var.ops_read_role != "" ? [1] : []
    content {
      actions   = ["sts:AssumeRole"]
      resources = ["arn:aws:iam::*:role/${var.ops_read_role}"]
      condition {
        test     = "StringEquals"
        variable = "aws:ResourceOrgID"
        values   = var.org_ids
      }
    }
  }

  # one data point per turn of what it cost in tokens (gerp/agent namespace only)
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["gerp/agent"]
    }
  }

  # Marketplace auto-subscribe on first model invoke. AWS-Bedrock model access
  # cascade (Anthropic FTU form submitted org-wide via scripts/bedrock-authorize-anthropic-org.sh)
  # gives every member account FTU approval; aws-marketplace:Subscribe lets
  # this role auto-subscribe to the specific model agreement on first invoke —
  # eliminates the per-account create-foundation-model-agreement step.
  statement {
    actions   = ["aws-marketplace:Subscribe"]
    resources = ["*"]
  }

  # NOTE: lambda:InvokeFunction is intentionally NOT here.
  # Per phase 4c, gateway dispatches to tool lambdas under the GATEWAY's role
  # (which is currently this same agent_execution role — see role_arn on
  # aws_bedrockagentcore_gateway.this). The grant is resource-based per
  # lambda and lives in the domain modules (`aws_lambda_permission` next to
  # each `aws_lambda_function`). Keeps "agent module knows which tools exist"
  # out of this file.

  # write to the agent's CloudWatch log group
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.agent.arn}:*"]
  }

  # the browse_* tools' MANAGED browser — AgentCore Browser sessions (remote
  # chromium, CDP over SigV4 websocket). Default browser (aws.browser.v1),
  # nothing provisioned; sessions are consumption-billed and sandboxed AWS-side.
  statement {
    actions = [
      "bedrock-agentcore:StartBrowserSession",
      "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:ListBrowserSessions",
      "bedrock-agentcore:StopBrowserSession",
      "bedrock-agentcore:UpdateBrowserStream",
      "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:ConnectBrowserLiveViewStream",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:aws:browser/aws.browser.v1",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:aws:browser/aws.browser.v1/*",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:browser/aws.browser.v1",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:browser/aws.browser.v1/*",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:browser-custom/*",
      # a session started WITH a profile authorizes against the profile arn too
      "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:browser-profile/*",
    ]
  }

  # Read per-customer registry DDB at boot — entrypoint.py builds the prompt's
  # registry section from these rows — and the pinned metric queries every turn; the metrics
  # usage table for the queries this firm ran last. Table arns constructed deterministically
  # to avoid a cross-module data dependency on module outputs.
  statement {
    actions = ["dynamodb:Query"]
    resources = [
      "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-schema-${replace(var.gerp_id, "_", "-")}",
      "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-metrics-${replace(var.gerp_id, "_", "-")}-usage",
    ]
  }

  # The settings config table, three ways: the email tool reads the caller's notification address
  # (`USER#<account_id>`, GetItem); the prompt's dynamic tail Queries `INSTRUCTION#` and
  # `MEMORY#<account_id>#` every turn; `remember`/`forget`/`instruct` write and delete those rows.
  # Arn constructed deterministically (same as SCHEMA_TABLE) — no cross-module data dependency on
  # modules/settings outputs.
  #
  # Not scoped to those sk prefixes, because it can't be: IAM's dynamodb:LeadingKeys condition
  # constrains the PARTITION key, and every row here shares one (`gerp_id`). So this also lets the
  # agent write `GERP#openly_operated`. That is a real widening and an acceptable one — the same
  # role already posts journal entries and moves money, so a guard on a settings row would secure
  # nothing it can't reach by a shorter path.
  statement {
    actions = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [
      "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}",
    ]
  }

  # AgentCore Memory event API — real ops (verified at 4b deploy):
  #   create_event / list_events / get_event / delete_event for raw conversation
  #   turns; memory_record ops are for *extracted* summaries/facts produced by
  #   memory strategies — distinct API, distinct permissions.
  statement {
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:DeleteEvent",
      # Record API kept for future memory-strategy integration:
      "bedrock-agentcore:GetMemoryRecord",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    resources = [aws_bedrockagentcore_memory.memory.arn]
  }

  # pull the container from operator's shared ECR (cross-account, allowed by
  # the operator's ECR resource policy with aws:PrincipalOrgID condition).
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = ["arn:aws:ecr:${var.aws_region}:${var.operator_account_id}:repository/agentcore"]
  }

  # Self-permission: container invokes its own sibling Gateway.
  # The Gateway is configured with authorizer_type = AWS_IAM below, so this
  # role's identity is what gets validated. Wildcarded on action for now —
  # tighten to the specific invoke actions once the AgentCore IAM reference
  # stabilizes.
  statement {
    actions   = ["bedrock-agentcore:*"]
    resources = [aws_bedrockagentcore_gateway.this.gateway_arn]
  }

  # Web Search Tool connector. The gateway's service role (this role) is checked per-request for
  # InvokeWebSearch on the service-owned tool ARN; InvokeGateway is already covered by the grant
  # above (gateway-scoped). The target itself is `web_search.tf`.
  statement {
    actions   = ["bedrock-agentcore:InvokeWebSearch"]
    resources = ["arn:aws:bedrock-agentcore:${data.aws_region.current.region}:aws:tool/web-search.v1"]
  }

  # browse_fill reads an owner secret (added in chat via collect_secret) from this gerp's secret
  # store to fill a portal password by reference. SecureString decryption = GetParameter
  # + kms:Decrypt; the secrets use the default aws/ssm key, so scope Decrypt via the SSM service.
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/secrets/*"]
  }
  # modules/mcp: the vendor gateway's url and the firm's client, read by path when a turn mounts
  # the vendors' tools
  statement {
    actions = ["ssm:GetParameter", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/mcp",
      "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/mcp/*",
    ]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }

  # Retrieve agent how-to / setup playbooks from the per-customer Bedrock
  # Knowledge Base. The search_guides tool issues bedrock:Retrieve against this
  # gerp's KB.
  statement {
    actions   = ["bedrock:Retrieve"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${var.playbook_kb_id}"]
  }

  # The shared standards corpus (operator bucket, org-readable by bucket policy — cross-account
  # S3 needs BOTH sides to allow). Reads bucket-wide; writes only this account's own contribution
  # prefix — same scoping the bucket policy enforces via ${aws:PrincipalAccount}. The in-process
  # tools also STS-resolve the account id (sts:GetCallerIdentity needs no grant).
  dynamic "statement" {
    for_each = var.standards_bucket != "" ? [1] : []
    content {
      actions = ["s3:GetObject", "s3:ListBucket"]
      resources = [
        "arn:aws:s3:::${var.standards_bucket}",
        "arn:aws:s3:::${var.standards_bucket}/*",
      ]
    }
  }
  dynamic "statement" {
    for_each = var.standards_bucket != "" ? [1] : []
    content {
      actions   = ["s3:PutObject"]
      resources = ["arn:aws:s3:::${var.standards_bucket}/_contrib/${data.aws_caller_identity.current.account_id}/*"]
    }
  }

  # Read/write the agent's own session bucket (S3SessionManager). Same-account,
  # so this identity grant is the whole story — no bucket policy. ListBucket lets
  # Strands enumerate a session's objects; the object actions cover restore + persist.
  statement {
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.sessions.arn,
      "${aws_s3_bucket.sessions.arn}/*",
    ]
  }

  # Read documents uploaded via render_frame `file` fields (I-9 / ID scans) from the
  # encrypted uploads bucket. The read_upload tool presigns a GET for a key recorded
  # on a worker-legal row; GetObject + kms:Decrypt on the uploads CMK let the presigner
  # serve the decrypted blob (the bytes never enter the agent). The chat lambda owns the
  # PUT side (chat.tf); this is the symmetric read grant.
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${var.uploads_bucket_arn}/*"]
  }
  # the standards copy-down layer: get_standard caches a shared-corpus hit into the cabinet's
  # standards/ prefix and contribute_standard writes the business's own copy there — writes are
  # prefix-scoped; everything else still arrives via presigned PUTs (chat lambda), never this role.
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${var.uploads_bucket_arn}/standards/*"]
  }
  # browse_screenshot stores page evidence (filing confirmations, order receipts) under its own
  # prefix — same posture as the standards prefix: this role writes exactly where its tools
  # write, nothing wider. read_upload presigns the read side; email attaches the bytes.
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${var.uploads_bucket_arn}/uploads/browse/*"]
  }
  # the self-continuation batons — continue_later reads the count floor and rewrites its own
  # session's key under the one prefix (continuation.tf watches it); prefix grant, nothing wider
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.uploads_bucket_arn}/state/continue/*"]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.uploads_kms_key_arn]
  }
  # drive the analysis sandbox (`analyze`). The runtime starts/invokes/stops sessions; it does NOT
  # hold the sandbox's own data credentials — those are the interpreter's execution role
  # (code_interpreter.tf), which is what keeps the PII carve-out meaningful.
  statement {
    actions = [
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:InvokeCodeInterpreter",
      "bedrock-agentcore:StopCodeInterpreterSession",
      "bedrock-agentcore:GetCodeInterpreterSession",
    ]
    resources = [
      aws_bedrockagentcore_code_interpreter.analysis.code_interpreter_arn,
      "${aws_bedrockagentcore_code_interpreter.analysis.code_interpreter_arn}/*",
    ]
  }

  # the email tool sends from the agent's own verified subdomain identity — the same mailbox the
  # email handler replies from — so this is a plain same-account send, no cross-account auth.
  # ses:SendEmail's IAM resource is evaluated against all identities involved (incl. the sandbox
  # recipient), so scope to "*" like the repo's other SES senders. Gated on the email feature.
  dynamic "statement" {
    for_each = local.email_enabled == 1 ? [1] : []
    content {
      actions   = ["ses:SendEmail", "ses:SendRawEmail"] # raw = the attachment path (MIME)
      resources = ["*"]
    }
  }

  # Registries are baked into the agent container image at docker build (the
  # Dockerfile COPYs modules/schemas/data/*.json into /app/registries/). No
  # IAM, no env vars, no S3 — the data is on disk inside the container.
  # Operator-approved registry changes are file edits + re-build-and-push.

  # ask_hub — cross-account InvokeAgentRuntime on the operator hub. Identity side; the hub's runtime
  # resource policy is the reciprocal grant (prod/optimizer). Scoped to the hub runtime ARN (the
  # endpoint arn's runtime prefix) + its endpoints. Gated on the hub arn being wired.
  dynamic "statement" {
    for_each = var.hub_runtime_endpoint_arn != "" ? [1] : []
    content {
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]
      resources = [
        split("/runtime-endpoint/", var.hub_runtime_endpoint_arn)[0],
        "${split("/runtime-endpoint/", var.hub_runtime_endpoint_arn)[0]}/*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "agent_execution" {
  name   = "${local.resource_prefix_snake}_execution"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.agent_execution.json
}

############################################
# AgentCore Memory — persistent session + long-term memory per customer
############################################

resource "aws_bedrockagentcore_memory" "memory" {
  name                  = "${local.resource_prefix_snake}_memory"
  description           = "Memory store for ${local.customer.business_name}'s agent"
  event_expiry_duration = var.memory_retention_days

  tags = {
    gerp_id = var.gerp_id
    module  = "agent"
  }
}

# Strategies (summarization / semantic extraction) are their own resource —
# aws_bedrockagentcore_memory_strategy. Added in phase 4a-ii once the agent
# loop actually reads/writes session history.

############################################
# AgentCore Identity — OAuth2 credential provider
############################################
# Not provisioned here. `aws_bedrockagentcore_oauth2_credential_provider` is
# for outbound tool calls that need the owner's OAuth token (GitHub, Slack,
# Salesforce, etc.) — one per integration, added when a tool actually needs it.
# Inbound auth is IAM (see header comment), not OAuth.

############################################
# AgentCore Agent Runtime — the actual agent, container from ECR
############################################

# newest image in the operator repo — the SAME reconciler pattern as the lambda artifact
# bucket: `deploy.sh image` pushes + CLI-updates the runtime (fast path); an apply from ANY
# runner moves the runtime forward to this digest, never backward. immutable version
# tags stay the history; most_recent replaces a moving "latest" tag the repo forbids.
data "aws_ecr_image" "agentcore" {
  registry_id     = var.operator_account_id
  repository_name = "agentcore"
  most_recent     = true
}

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = local.resource_prefix_snake
  description        = "Agent for ${local.customer.business_name} (${local.customer.business_category})"
  role_arn           = aws_iam_role.agent_execution.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${var.operator_account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/agentcore@${data.aws_ecr_image.agentcore.image_digest}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC" # AgentCore Runtime manages the VPC; no customer-managed VPC here
  }

  # container serves plain HTTP `POST /invocations` on :8080 (FastAPI + uvicorn).
  # server_protocol = MCP would expect the container to BE an MCP server on /mcp;
  # we're an invocations-style agent, not an MCP server. HTTP is right.
  protocol_configuration {
    server_protocol = "HTTP"
  }

  # No authorizer_configuration block — callers (cross-account dispatcher,
  # channel lambdas) invoke via standard AWS SigV4 with IAM permissions to
  # bedrock-agentcore:InvokeAgentRuntime on this runtime's ARN. Owner-facing
  # JWT validation, when it lands, happens at the channel-lambda layer, not
  # here — the runtime only sees SigV4-authenticated calls.

  environment_variables = {
    BROWSER_ID         = aws_bedrockagentcore_browser.this.browser_id         # browse_* drives the RECORDED custom browser
    BROWSER_PROFILE_ID = aws_bedrockagentcore_browser_profile.this.profile_id # persistent cookies — portal logins survive across sessions
    AWS_REGION         = data.aws_region.current.region
    AWS_DEFAULT_REGION = data.aws_region.current.region
    AGENT_MODE         = "bookkeeper" # onboarding flow triggered separately for new customers

    # The business's clock. The agent renders today's date in it and NAMES it whenever it states a
    # time, so a reader can tell which zone a number is in. Deliberately not a conversion layer:
    # storage and period boundaries are still UTC (modules/clock), and disclosure is what keeps that
    # honest until they aren't.
    GERP_TIMEZONE     = var.timezone
    BUSINESS_NAME     = local.customer.business_name
    BUSINESS_CATEGORY = local.customer.business_category

    # StrandsEngine selector: BEDROCK_MODEL_ID + GATEWAY_URL set → the container
    # uses Bedrock for inference and Gateway MCP (SigV4) for tool dispatch.
    # Unsetting either falls back to AnthropicEngine (which requires /repo
    # mount + ANTHROPIC_API_KEY, neither present in prod) and would crash on
    # the first /invocations call — so these MUST be set here.
    BEDROCK_MODEL_ID = var.model_id
    GATEWAY_URL      = aws_bedrockagentcore_gateway.this.gateway_url

    # SESSIONS_BUCKET selects S3SessionManager — the agent's authoritative
    # session + interrupt checkpoint. MEMORY_ID is now just the UI-transcript
    # mirror the web chat replays from (entrypoint.py demotes it).
    SESSIONS_BUCKET = aws_s3_bucket.sessions.bucket
    MEMORY_ID       = aws_bedrockagentcore_memory.memory.id

    # Encrypted uploads bucket for render_frame `file` fields. The chat lambda presigns
    # PUTs here (browser → S3); the in-process read_upload tool presigns GETs so the agent
    # can hand the owner a short-lived download link for a stored doc (I-9 / ID scan) whose
    # key lives on a worker-legal row. Bytes never transit the agent.
    UPLOADS_BUCKET = var.uploads_bucket

    # email tool: the agent emails from its OWN address (the verified subdomain identity the email
    # handler replies from). "email me" resolves to the caller's notification address in the settings
    # table (below); a named recipient goes where the user directs. Empty AGENT_ADDRESS (email feature
    # off) ⇒ the tool isn't registered.
    AGENT_ADDRESS = local.email_enabled == 1 ? local.email_address : ""

    # the settings config table (modules/settings) — the email tool reads the caller's notification
    # address from USER#<account_id>; CUSTOMER_ID is the partition key. Name constructed like
    # SCHEMA_TABLE (same shape modules/settings/infra creates), so no cross-module wiring.
    CUSTOMER_ID    = var.gerp_id
    SETTINGS_TABLE = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"

    # entrypoint.py queries this DDB at boot to build the prompt's registry
    # section. Table name is deterministic — same construction as
    # modules/schemas/infra creates it.
    SCHEMA_TABLE = "${var.stack_prefix}-schema-${replace(var.gerp_id, "_", "-")}"

    # the prompt's dynamic tail carries the metric queries this firm pinned (registry rows) and
    # the ones it ran last (modules/metrics' usage table). Name constructed like SCHEMA_TABLE.
    USAGE_TABLE = "${var.stack_prefix}-metrics-${replace(var.gerp_id, "_", "-")}-usage"

    # entrypoint's search_guides tool retrieves how-to playbooks from this
    # gerp's Bedrock Knowledge Base at turn-time. WEBHOOK_BASE_URL fills the
    # {{ webhook_base_url }} placeholder in those docs with this customer's API
    # gateway endpoint.
    PLAYBOOK_KB_ID   = var.playbook_kb_id
    WEBHOOK_BASE_URL = var.webhook_base_url

    # This gerp's secret store (same path manage_secret writes to). browse_fill resolves a
    # `secret:<name>` field value through it, so a portal password is filled without the value
    # entering the conversation.
    SECRET_PARAM_PREFIX = "/gradienterp/customers/${var.gerp_id}/secrets"

    # ask_hub tool: reach the operator's coordination hub for cross-firm requests the owner can't
    # fulfill from its own books ("find me a technician"). Empty ⇒ the tool isn't registered.
    HUB_RUNTIME_ENDPOINT_ARN = var.hub_runtime_endpoint_arn

    # read_fleet_logs tool (the operator gerp only): the role every gerp account holds for an
    # investigator's reads. Empty ⇒ the tool isn't registered.
    OPS_READ_ROLE = var.ops_read_role

    # the shared standards corpus (operator bucket): root-first reads, contributions under this
    # account's own _contrib prefix, promoted by the hub curator. Empty ⇒ tools not registered.
    STANDARDS_BUCKET    = var.standards_bucket
    CODE_INTERPRETER_ID = aws_bedrockagentcore_code_interpreter.analysis.code_interpreter_id
    ANALYSIS_BUCKET     = var.uploads_bucket  # artifacts land in the cabinet under analysis/
    REPORT_BUCKET       = local.report_bucket # statements — the sandbox's primary input
  }

  # AgentCore tag values are restricted to [A-Za-z0-9_.:/=+-@ ] — business_name
  # may contain apostrophes and other disallowed chars, so it stays out of tags.
  # Look up tenant metadata via SSM (see `local.customer`) when you need the
  # human-readable name; tags are for resource identity only.
  tags = {
    gerp_id = var.gerp_id
    module  = "agent"
  }

  depends_on = [
    aws_iam_role_policy.agent_execution,
    aws_bedrockagentcore_memory.memory,
    aws_cloudwatch_log_group.agent,
  ]
}

############################################
# Runtime endpoint — the invokable URL the owner's channels call into
############################################

resource "aws_bedrockagentcore_agent_runtime_endpoint" "this" {
  name             = "${local.resource_prefix_snake}_endpoint"
  agent_runtime_id = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
  # Pinning to the current runtime version: AgentCore does NOT auto-track latest.
  # Without this, the endpoint stays on whatever version existed at endpoint-creation
  # time even as the runtime revs forward through terraform applies.
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.this.agent_runtime_version
}

# ─── inbound trust: the operator hub may InvokeAgentRuntime on this spoke (ask_spoke) ───
#
# The reciprocal of the ask_hub identity grant above. Cross-account InvokeAgentRuntime with a
# qualifier authorizes against BOTH the runtime arn AND the endpoint arn, and put-resource-policy
# requires the policy Resource to EXACTLY match resource_arn — so one policy per arn. Scoped to the
# specific hub role (not the org): only the hub reaches spokes; spokes never invoke each other.
# Gated on hub_role_arn — a spoke with no hub wired grants nothing.
locals {
  spoke_arns = var.hub_role_arn == "" ? {} : {
    runtime  = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
    endpoint = aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn
  }
}

resource "aws_bedrockagentcore_resource_policy" "hub_inbound" {
  for_each     = local.spoke_arns
  resource_arn = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowOptimizerHubInvoke"
      Effect    = "Allow"
      Principal = { AWS = var.hub_role_arn }
      Action    = "bedrock-agentcore:InvokeAgentRuntime"
      Resource  = each.value
    }]
  })
}

############################################
# AgentCore Gateway — MCP surface that exposes the customer's lambdas as tools
############################################

resource "aws_bedrockagentcore_gateway" "this" {
  name        = "${local.resource_prefix_dns}-gateway"
  description = "MCP gateway for ${local.customer.business_name} — wraps accounting + inventory lambdas as agent tools"
  role_arn    = aws_iam_role.agent_execution.arn

  protocol_type   = "MCP"
  authorizer_type = "AWS_IAM"
  # authorizer_configuration intentionally omitted — only required for CUSTOM_JWT.
  # Caller (the agent_execution role, same sub-account) is authenticated via SigV4.

  tags = {
    gerp_id = var.gerp_id
    module  = "agent"
  }
}

############################################
# Gateway discovery — published via SSM for domain modules.
#
# Domain modules (modules/accounting/infra/, modules/inventory/infra/, etc.)
# read these to register their own gateway targets and to pin their lambda
# permission principal. This module owns the gateway resource; domain modules
# own their tool-surface (lambda + target + lambda_permission) end-to-end.
############################################

resource "aws_ssm_parameter" "gateway_id" {
  name  = "/gradienterp/customers/${var.gerp_id}/agent/gateway_id"
  type  = "String"
  value = aws_bedrockagentcore_gateway.this.gateway_id
}

resource "aws_ssm_parameter" "gateway_arn" {
  name  = "/gradienterp/customers/${var.gerp_id}/agent/gateway_arn"
  type  = "String"
  value = aws_bedrockagentcore_gateway.this.gateway_arn
}

resource "aws_ssm_parameter" "gateway_url" {
  name  = "/gradienterp/customers/${var.gerp_id}/agent/gateway_url"
  type  = "String"
  value = aws_bedrockagentcore_gateway.this.gateway_url
}

resource "aws_ssm_parameter" "gateway_role_arn" {
  # The IAM role identity Gateway uses when invoking tool lambdas. Domain
  # modules use this as the principal in their per-lambda aws_lambda_permission.
  name  = "/gradienterp/customers/${var.gerp_id}/agent/gateway_role_arn"
  type  = "String"
  value = aws_iam_role.agent_execution.arn
}

resource "aws_ssm_parameter" "runtime_endpoint_arn" {
  # Downstream modules that poke the agent (modules/tasks tasks_poke) read this —
  # they apply after agent, so a direct module ref would cycle through their depends_on.
  name  = "/gradienterp/customers/${var.gerp_id}/agent/runtime_endpoint_arn"
  type  = "String"
  value = aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn
}

# Scheduled report generation lives with the lambda it fires (accounting's
# get_statement suite writer). The accounting module already provisions a customer-owned
# cron on `local.customer.reporting_schedule` beside it. Per 4c,
# the agent module doesn't reach into other modules' lambdas.
