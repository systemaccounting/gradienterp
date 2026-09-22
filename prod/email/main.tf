# SES inbound email forwarding for gradienterp.cloud — recreates the Squarespace aliases
# (ops@, payment@, help@, …) as a catch-all → a personal inbox, off Squarespace and in TF.
# Records land in the gradienterp.cloud Route53 zone (prod/dns); they're inert until the
# domain's nameservers are pointed at Route53, at which point SES verification completes.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# The zone is owned by prod/dns; we only add records to it.
data "aws_route53_zone" "cloud" {
  name = "gradienterp.cloud."
}

locals {
  config    = jsondecode(file("${path.module}/../../config.json"))
  domain    = "gradienterp.cloud"
  prefix    = "gerp-mail"
  s3_prefix = "inbound/"
  # Reserved local-part prefix for automated tests (tests/mailbox). The catch-all still RECEIVES and
  # stores these to S3 — the forwarder just doesn't relay them to the personal inbox.
  no_forward_prefix = "test+"
}

# ── SES domain identity + DKIM (verified via the DNS records below) ──────────
resource "aws_ses_domain_identity" "cloud" {
  domain = local.domain
}

resource "aws_ses_domain_dkim" "cloud" {
  domain = aws_ses_domain_identity.cloud.domain
}

# ── DNS in the gradienterp.cloud zone (inert until NS is pointed at Route53) ──
resource "aws_route53_record" "ses_verify" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = "_amazonses.${local.domain}"
  type    = "TXT"
  ttl     = 600
  records = [aws_ses_domain_identity.cloud.verification_token]
}

resource "aws_route53_record" "ses_dkim" {
  count   = 3
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = "${aws_ses_domain_dkim.cloud.dkim_tokens[count.index]}._domainkey.${local.domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_ses_domain_dkim.cloud.dkim_tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_route53_record" "mx" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = local.domain
  type    = "MX"
  ttl     = 600
  records = ["10 inbound-smtp.${data.aws_region.current.region}.amazonaws.com"]
}

resource "aws_route53_record" "spf" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = local.domain
  type    = "TXT"
  ttl     = 600
  records = ["v=spf1 include:amazonses.com ~all"]
}

# Strict alignment; SES Easy DKIM signs d=gradienterp.cloud so forwards pass. No `rua`
# (would put a personal address in the repo). Tighten to p=reject once verified clean.
resource "aws_route53_record" "dmarc" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = "_dmarc.${local.domain}"
  type    = "TXT"
  ttl     = 600
  records = ["v=DMARC1; p=quarantine; adkim=s; aspf=s"]
}

# ── S3 bucket for the raw inbound messages (SES writes here, Lambda reads) ────
resource "aws_s3_bucket" "mail" {
  bucket = "${local.prefix}-inbound-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_lifecycle_configuration" "mail" {
  bucket = aws_s3_bucket.mail.id
  rule {
    id     = "expire-raw"
    status = "Enabled"
    filter {}
    expiration { days = 7 } # forwarded already; no reason to keep raw copies around
  }
}

resource "aws_s3_bucket_policy" "mail" {
  bucket = aws_s3_bucket.mail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSESPuts"
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.mail.arn}/*"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

# ── Forward destination — value set out-of-band (personal address, kept out of TF) ──
resource "aws_ssm_parameter" "forward_to" {
  name  = "/gradienterp/email/forward_to"
  type  = "String"
  value = "set-me@example.com"
  lifecycle {
    ignore_changes = [value] # real value via `aws ssm put-parameter --overwrite`
  }
}

# ── Forwarder Lambda ─────────────────────────────────────────────────────────
data "archive_file" "forwarder" {
  type        = "zip"
  source_file = "${path.module}/forwarder/main.py"
  output_path = "${path.module}/forwarder.zip"
}

# ─── SMTP sending identity ───
#
# A gerp sends through its firm's own mail server (modules/agent § mail.tf), and for gradienterp
# that server is SES itself: `email-smtp.<region>.amazonaws.com` is an ordinary SMTP host, so no
# special case is needed for the one firm whose mail already lives here.
#
# SMTP authenticates with a credential rather than a role, which is what lets a gerp in its own
# sub-account reach this account's verified `gradienterp.cloud` identity. IAM could not express
# that without cross-account trust; a username and password need only the network.
#
# The USER is terraform's; the ACCESS KEY is not. A key created here would sit in state, and the
# SMTP password is derived from it — same reason the plaid credentials are made out of band.
# Create it with `aws iam create-access-key`, derive the password (scripts/ses_smtp_password.py),
# and put it straight into the gerp's secret store.
resource "aws_iam_user" "smtp" {
  name = "gradienterp-ses-smtp"
  tags = { purpose = "SES SMTP credential for gerp outbound mail" }
}

resource "aws_iam_user_policy" "smtp" {
  name = "ses-send"
  user = aws_iam_user.smtp.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # SendRawEmail is what the SMTP interface calls under the hood — the only thing this
      # principal may do, on the identities this account has verified.
      Effect   = "Allow"
      Action   = ["ses:SendRawEmail"]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role" "forwarder" {
  name = "${local.prefix}-forwarder"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "forwarder" {
  name = "${local.prefix}-forwarder"
  role = aws_iam_role.forwarder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.mail.arn}/*" },
      { Effect = "Allow", Action = "ses:SendRawEmail", Resource = "*" },
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.forward_to.arn },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*" },
    ]
  })
}

module "forwarder" {
  source = "../../modules/terraform/lambda"

  name             = "${local.prefix}-forwarder"
  role             = aws_iam_role.forwarder.arn
  filename         = data.archive_file.forwarder.output_path
  source_code_hash = data.archive_file.forwarder.output_base64sha256
  src_dir          = "prod/email/forwarder"
  timeout          = 30
  memory           = 256
  env_vars = {
    MAIL_BUCKET      = aws_s3_bucket.mail.id
    MAIL_PREFIX      = local.s3_prefix
    FORWARD_TO_PARAM = aws_ssm_parameter.forward_to.name
    # local-part prefix reserved for automated tests — received and stored, never forwarded.
    # `tests/mailbox` reads the S3 copy; this only suppresses the personal-inbox hop.
    NO_FORWARD_PREFIX = local.no_forward_prefix
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.forwarder
  to   = module.forwarder.aws_lambda_function.this
}


resource "aws_lambda_permission" "ses" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowSESInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.forwarder.name
  principal           = "ses.amazonaws.com"
  source_account      = data.aws_caller_identity.current.account_id
}

# ── SES receipt rule set: store to S3 (pos 1) then invoke the forwarder (pos 2) ──
resource "aws_ses_receipt_rule_set" "main" {
  rule_set_name = "${local.prefix}-rules"
}

resource "aws_ses_active_receipt_rule_set" "main" {
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
}

resource "aws_ses_receipt_rule" "forward" {
  name          = "forward-to-inbox"
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  recipients    = [local.domain] # catch-all for the domain
  enabled       = true
  scan_enabled  = true

  s3_action {
    bucket_name       = aws_s3_bucket.mail.id
    object_key_prefix = local.s3_prefix
    position          = 1
  }

  lambda_action {
    function_arn    = module.forwarder.arn
    invocation_type = "Event"
    position        = 2
  }

  depends_on = [aws_s3_bucket_policy.mail, aws_lambda_permission.ses]
}

output "ses_verification_pending" {
  description = "SES domain verification completes once the gradienterp.cloud NS are pointed at Route53."
  value       = aws_ses_domain_identity.cloud.verification_token
}
