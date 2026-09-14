############################################
# Agent email front door — the gerp talks to its agent over email.
#
# Decentralized & in-account: SES receiving + sending live in THIS customer
# sub-account on a per-gerp subdomain (<gerp_id>.<parent>), verified here. Inbound
# mail lands as a raw .eml in this account's email bucket under in/; an
# ObjectCreated trigger runs the handler, which invokes the same-account agent
# runtime and SES-replies from the subdomain. Nothing transits an operator bucket.
#
# DNS is self-contained too: this module owns a Route53 hosted zone for the
# subdomain and writes all the SES records (MX, verify, DKIM, SPF, DMARC) into it
# in-account. The ONLY operator-side footprint is a single NS delegation record
# pointing the parent zone at this zone's nameservers (see output below) — so the
# module never needs a cross-account provider and stays standalone-applyable.
#
# Sandbox-as-allowlist: this account's SES is in the sandbox, so it can only reply
# to *verified* recipients. We verify owner_email below — which doubles as the
# allowlist (the agent answers, and replies to, the owner and no one else). No
# per-account production-access request, no separate allowlist store.
#
# Gated on agent_email_parent_domain (count): unset ⇒ no email front door, clean
# standalone apply. Assumes one gerp per sub-account (one SES receipt rule set).
############################################

locals {
  email_enabled     = var.agent_email_parent_domain != "" && try(local.customer.owner_email, "") != "" ? 1 : 0
  email_subdomain   = "${replace(var.gerp_id, "_", "-")}.${var.agent_email_parent_domain}"
  email_address     = "agent@${local.email_subdomain}"
  email_bucket      = "${var.stack_prefix}-agent-${replace(var.gerp_id, "_", "-")}-email-${data.aws_caller_identity.current.account_id}"
  email_dedup_table = "${var.stack_prefix}-agent-${replace(var.gerp_id, "_", "-")}-email"
  email_fn          = "${local.resource_prefix_dns}-email"
  owner_email       = try(local.customer.owner_email, "")
}

# ─── SES identity for the subdomain + DKIM (this account) ───
# The verification + DKIM tokens are output below; the per_customer root writes the DNS records into
# the operator's gradienterp.cloud zone (reusable delegation sets are account-scoped, so a per-gerp
# zone can't live here — records go straight in the operator zone instead).
resource "aws_ses_domain_identity" "email" {
  count  = local.email_enabled
  domain = local.email_subdomain
}

resource "aws_ses_domain_dkim" "email" {
  count  = local.email_enabled
  domain = aws_ses_domain_identity.email[0].domain
}

# Custom MAIL FROM (mail.<subdomain>) so the envelope return-path is the gerp's own domain → SPF
# aligns to From (not amazonses.com). Its MX + SPF records are written into the operator zone by the
# per_customer root. UseDefaultValue: fall back to amazonses.com if the MX isn't resolvable yet.
resource "aws_ses_domain_mail_from" "email" {
  count                  = local.email_enabled
  domain                 = aws_ses_domain_identity.email[0].domain
  mail_from_domain       = "mail.${local.email_subdomain}"
  behavior_on_mx_failure = "UseDefaultValue"
}

# ─── owner_email verified as a reply recipient (sandbox) == the allowlist ───
# Creating this fires SES's confirm email to the owner; status is pollable via
# GetIdentityVerificationAttributes (surfaced to the web client via the gateway).
resource "aws_ses_email_identity" "owner" {
  count = local.email_enabled
  email = local.owner_email
}

# ─── email bucket: raw .eml under in/, archived sends under out/ ───
resource "aws_s3_bucket" "email" {
  count  = local.email_enabled
  bucket = local.email_bucket
  tags   = { gerp_id = var.gerp_id, module = "agent", "gerp:layer" = "session" }

  # Same reason as the sessions bucket: a non-empty bucket fails the closure destroy at apply. Raw
  # inbound mail already expires at 14 days by the rule below — it is the delivery envelope, not a
  # record, and whatever it produced is in the books by then.
  force_destroy = true
}

# The owner portal's `/s3?key=…&bucket=email` answers with a presigned GET on this bucket's own
# endpoint (modules/storage/lambdas/ui), and a page that fetch()es a message follows the redirect
# cross-origin. The URL signature is the auth; CORS just lets the page read it.
resource "aws_s3_bucket_cors_configuration" "email" {
  count  = local.email_enabled
  bucket = aws_s3_bucket.email[0].id
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "email" {
  count  = local.email_enabled
  bucket = aws_s3_bucket.email[0].id
  # The landing zone is transient — a message is filed into its mailbox within seconds, and
  # anything left is a failed run worth keeping briefly to look at.
  rule {
    id     = "expire-landing-zone"
    status = "Enabled"
    filter { prefix = "inbound/" }
    expiration { days = 7 }
  }

  # Mail to an address this firm never declared. Parked rather than dropped so the owner can look
  # — a customer who guessed wrong is in here along with the scrapes — then gone.
  rule {
    id     = "expire-spam"
    status = "Enabled"
    filter { prefix = "in/spam/" }
    expiration { days = 30 }
  }

  # Everything else in in/<mailbox>/ has NO expiry, deliberately. This bucket used to be a
  # transport — mail arrived, poked a session, the raw bytes were spare, and a blanket 14-day
  # rule was right. With mailboxes it is storage: in/billing/ holds the firm's correspondence,
  # and a shred on a timer would quietly eat it. Deleting is the owner's, through the portal.
}

resource "aws_s3_bucket_policy" "email" {
  count  = local.email_enabled
  bucket = aws_s3_bucket.email[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSESInbound"
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.email[0].arn}/inbound/*"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_s3_bucket_notification" "email" {
  count  = local.email_enabled
  bucket = aws_s3_bucket.email[0].id
  lambda_function {
    lambda_function_arn = module.email[0].arn
    events              = ["s3:ObjectCreated:*"]
    # SES lands mail in inbound/; the handler files it into in/<mailbox>/. Two top-level
    # prefixes on purpose — filing into the WATCHED one would re-trigger this lambda on its own
    # output, forever.
    filter_prefix = "inbound/"
  }
  depends_on = [aws_lambda_permission.email_s3]
}

# ─── dedup (SES redelivery / double ObjectCreated): conditional put, TTL away ───
resource "aws_dynamodb_table" "email_dedup" {
  count        = local.email_enabled
  name         = local.email_dedup_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "message_id"

  attribute {
    name = "message_id"
    type = "S"
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  tags = { gerp_id = var.gerp_id, module = "agent", "gerp:layer" = "session" }
}

# ─── handler: read .eml → guard → invoke same-account agent → SES reply ───
resource "aws_iam_role" "email" {
  count = local.email_enabled
  name  = "${local.resource_prefix_snake}_email"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
  tags = { gerp_id = var.gerp_id, module = "agent" }
}

resource "aws_iam_role_policy" "email" {
  count = local.email_enabled
  name  = "${local.resource_prefix_snake}_email"
  role  = aws_iam_role.email[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # read the landing zone, write + clear it as mail is filed into its mailbox
      { Effect = "Allow", Action = ["s3:GetObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.email[0].arn}/inbound/*" },
      { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.email[0].arn}/in/*" },
      { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.email[0].arn}/out/*" },
      { Effect = "Allow", Action = "ses:SendRawEmail", Resource = "*" },
      {
        # invoke this gerp's own agent runtime (same account)
        Effect = "Allow"
        Action = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [
          aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn,
          aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn,
        ]
      },
      { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = aws_dynamodb_table.email_dedup[0].arn },
      # the mailbox list — which local parts this firm accepts, and which one wakes the agent
      { Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*" },
    ]
  })
}

module "email" {
  count  = local.email_enabled
  source = "../../terraform/lambda"

  name            = local.email_fn
  role            = aws_iam_role.email[0].arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/agent/lambdas/email.zip"
  src_dir         = "modules/agent/lambdas/email"
  gerp_id         = var.gerp_id
  timeout         = 120 # an agent turn can run 20s+
  memory          = 512
  env_vars = {
    EMAIL_BUCKET               = aws_s3_bucket.email[0].id
    DEDUP_TABLE                = aws_dynamodb_table.email_dedup[0].name
    AGENT_RUNTIME_ENDPOINT_ARN = aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn
    AGENT_ADDRESS              = local.email_address
    ALLOWLIST                  = local.owner_email # comma-separated when employees are added
    MAX_ATTACHMENT_MB          = "5"
    SETTINGS_TABLE             = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.email[0]
  to   = module.email[0].aws_lambda_function.this
}

resource "aws_lambda_permission" "email_s3" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  count               = local.email_enabled
  statement_id_prefix = "AllowS3Invoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.email[0].name
  principal           = "s3.amazonaws.com"
  source_arn          = aws_s3_bucket.email[0].arn
}

# ─── SES receipt rule: catch-all on the subdomain → S3 in/ (this account) ───
resource "aws_ses_receipt_rule_set" "email" {
  count         = local.email_enabled
  rule_set_name = "${local.resource_prefix_dns}-email"
}

resource "aws_ses_active_receipt_rule_set" "email" {
  count         = local.email_enabled
  rule_set_name = aws_ses_receipt_rule_set.email[0].rule_set_name
}

resource "aws_ses_receipt_rule" "email" {
  count         = local.email_enabled
  name          = "to-agent"
  rule_set_name = aws_ses_receipt_rule_set.email[0].rule_set_name
  recipients    = [local.email_subdomain] # catch-all for this gerp's subdomain
  enabled       = true
  scan_enabled  = true

  s3_action {
    bucket_name       = aws_s3_bucket.email[0].id
    object_key_prefix = "inbound/"
    position          = 1
  }

  depends_on = [aws_s3_bucket_policy.email]
}

# ─── outputs: the per_customer root writes these as records into the operator's gradienterp.cloud zone ───
output "email_bucket" {
  description = "The inbound-mail bucket (raw .eml under in/). Empty when the email front door is off. modules/storage's portal reads it so the owner can browse what arrived."
  value       = local.email_enabled == 1 ? local.email_bucket : ""
}

output "agent_email_address" {
  description = "Where the owner emails this gerp's agent."
  value       = local.email_enabled == 1 ? local.email_address : ""
}

output "agent_email_dns" {
  description = "SES verification + DKIM tokens for the subdomain; the per_customer root turns these into records in the operator zone."
  value = local.email_enabled == 1 ? {
    subdomain          = local.email_subdomain
    verification_token = aws_ses_domain_identity.email[0].verification_token
    dkim_tokens        = aws_ses_domain_dkim.email[0].dkim_tokens
  } : null
}

