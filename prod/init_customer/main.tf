# init_customer — the part of a gerp that outlives the gerp.
#
# Applied FIRST when a customer is provisioned, and destroyed LAST when they leave. Everything else
# a gerp is — the agent, the gateway, every domain module, all its tables — lives in
# `prod/per_customer` and is torn down at closure while this stack keeps standing.
#
# Why a separate stack rather than a module: `terraform destroy` destroys a STATEFILE. Module
# nesting is namespacing and does not survive it, and the alternatives — `-target` (which prunes
# what it does not name, silently) and `state rm` before every teardown (a manual step in a
# destructive path) — are both ways to lose a customer's books on the day someone is in a hurry.
#
# What has to be here is decided by one question: after the instance is gone, can the owner still
# download their export for fifteen days?
#
#   the bucket        the export objects, and the filing cabinet they were copied from
#   the CMK           SSE-KMS. Destroy the key and every object is permanently unreadable while
#                     the bucket sits there looking kept — the failure that looks like success
#   the reader role   what the download script assumes
#   export_gerp       the credential is ONE HOUR (role chaining caps at 3600s) and the window is
#                     fifteen days, so something must be able to issue a fresh one
#
# All of it is free at rest: object storage, a key, a role, and an idle lambda.
#
# `per_customer` finds these by NAME, with data sources — no remote state, no outputs to thread.
# Every name here is derivable from (stack_prefix, gerp_id, account), which is the same convention
# the BFF uses to reach a customer's export lambda. A missing init stack then fails at plan time in
# per_customer rather than producing a half-built gerp.
#
# Design and the closure sequence: modules/export/TODO.md § closure.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # repo-root config.json, the same read per_customer makes — the retention and the ops topic
  # every function owns (modules/terraform/lambda)
  config              = jsondecode(file("${path.module}/../../config.json"))
  gerp_dash           = replace(var.gerp_id, "_", "-")
  operator_account_id = local.config.OPERATOR_ACCOUNT_ID
  bucket              = "${var.stack_prefix}-agent-${local.gerp_dash}-uploads-${data.aws_caller_identity.current.account_id}"
  key_alias           = "alias/agentcore_${replace(var.gerp_id, "-", "_")}_uploads"
}

# ─── the document store ───
#
# Blobs for render_frame `file` fields and the general filing cabinet: documents, submissions,
# pages, scripts, outputs, and `exports/`. modules/storage files and captions objects here (the
# caption is an S3 annotation on the object); the browser PUTs bytes straight in via a presigned URL
# the chat lambda creates, so only the object KEY ever flows through a lambda body.

resource "aws_kms_key" "uploads" {
  description             = "agentcore ${local.gerp_dash} agent file-upload (legal/PII doc) encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  tags                    = { gerp_id = var.gerp_id, module = "agent" }
}

resource "aws_kms_alias" "uploads" {
  name          = local.key_alias
  target_key_id = aws_kms_key.uploads.key_id
}

resource "aws_s3_bucket" "uploads" {
  bucket = local.bucket
  tags   = { gerp_id = var.gerp_id, module = "agent", "gerp:layer" = "operational" }

  # the day-30 sweep destroys this stack, and a bucket holding a customer's whole filing cabinet is
  # never empty. Without this the final teardown fails and the account lingers.
  force_destroy = true
}

# history for agent-composed documents (manage_storage put — a compliance index, a filing JSON):
# every overwrite retains the prior version, so the cabinet's living documents carry their own
# audit trail without append semantics.
resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration {
    status = "Enabled"
  }
}

# S3 holds ONE lifecycle configuration per bucket, so every rule for this bucket lives here — a
# second resource pointed at the same bucket does not add rules, it REPLACES them, and two of them
# overwrite each other on alternating applies.
resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  # modules/export writes here. Long enough to download, short enough that a copy of the firm's
  # whole books is not left lying in the filing cabinet indefinitely.
  rule {
    id     = "expire-exports"
    status = "Enabled"
    filter {
      prefix = "exports/"
    }
    expiration {
      days = var.export_retention_days
    }
  }

  # Build scrap expires; owner content never does. A cmd-tool layer build (modules/cmd) drops a
  # ~15MB zip under layers/ on every run, and Lambda COPIES the content at PublishLayerVersion — so
  # once the layer version exists the zip is scrap, and nothing but a rule ever removes it.
  rule {
    id     = "expire-layer-build-scrap"
    status = "Enabled"
    filter {
      prefix = "layers/"
    }
    expiration {
      days = 7
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Default SSE-KMS with the per-customer CMK — objects encrypt at rest with no per-PUT header, so the
# presigned PUT just sends the file. The presigner's role carries the kms:GenerateDataKey grant.
resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.uploads.arn
    }
    bucket_key_enabled = true
  }
}

# The browser PUTs cross-origin (from the chat Function URL) straight to S3 via the presigned URL,
# and a portal page's fetch() of `/s3?key=` follows the redirect to a presigned GET — in both the
# URL signature is the auth; CORS just lets the page issue it. One configuration per bucket, so both
# rules live here.
resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    max_age_seconds = 3000
  }
}

# ─── taking the data out ───
#
# The lambda only. Its registration on the agent gateway is `modules/export/gateway`, instantiated
# by per_customer — the gateway dies at closure and this must not, so the tool exists exactly as
# long as the agent does while the lambda outlives both.

module "export" {
  source               = "../../modules/export/infra"
  log_retention_days   = try(local.config.LOG_RETENTION_DAYS, 90)
  ops_alerts_topic_arn = try(local.config.OPS_ALERTS_TOPICS[var.aws_region], local.config.OPS_ALERTS_TOPIC_ARN, "") # this region's

  gerp_id             = var.gerp_id
  stack_prefix        = var.stack_prefix
  storage_bucket      = aws_s3_bucket.uploads.bucket
  storage_kms_key_arn = aws_kms_key.uploads.arn

  # the owner app's door to the export — and after closure the only one, since the agent that used
  # to create the links is gone
  export_invoker_role_arn = var.export_invoker_role_arn

  # and the closure path — CodeBuild runs the export before it destroys per_customer
  closure_invoker_role_arn = var.closure_invoker_role_arn
}

# ─── gerp-ops-read: the operator reads this account's logs, metrics, alarms and parked records ───
#
# What an investigator needs and nothing more: the lines (Logs Insights and filter), the metrics,
# the alarms, the failed queues' messages and a table's description. No data reads, no writes.
# Assumed from the operator account — by `issue_collector` when an alarm becomes a task, by the
# operator gerp's agent on its poke, by a person with the operator profile at a terminal. Lives
# here (not per_customer) so a stopped gerp's account can still be read.
resource "aws_iam_role" "ops_read" {
  name = "${var.stack_prefix}-ops-read"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # every investigator is a principal here: the operator account (a person, the collector),
      # and the operator gerp's agent runtime, which lives in the operator gerp's own account
      Principal = { AWS = [
        "arn:aws:iam::${local.operator_account_id}:root",
        "arn:aws:iam::${local.config.SELLER_ACCOUNT_ID}:role/agentcore_${replace(local.config.SELLER_GERP, "-", "_")}_execution",
      ] }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ops_read" {
  name = "${var.stack_prefix}-ops-read"
  role = aws_iam_role.ops_read.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery", "logs:FilterLogEvents",
          "logs:GetLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams",
          "cloudwatch:DescribeAlarms", "cloudwatch:DescribeAlarmHistory", "cloudwatch:ListMetrics",
          "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
        "sqs:ListQueues", "dynamodb:ListTables"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:ReceiveMessage"]
        Resource = "arn:aws:sqs:${data.aws_region.current.id}:${var.aws_account_id}:${var.stack_prefix}-*-failed"
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:DescribeTable"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${var.aws_account_id}:table/${var.stack_prefix}-*"
      },
    ]
  })
}

output "ops_read_role_arn" {
  value = aws_iam_role.ops_read.arn
}

# ─── the link to the operator's observability sink ───
#
# This account's metrics and log groups become readable in the operator account (dashboards,
# GetMetricData, Logs Insights across accounts) — the person's window (the `gerp-ops`
# dashboard) and the investigator's read. The sink is tower's in this region (regions.tf); its arn
# rides config.json OAM_SINKS by region. No entry = no link.
resource "aws_oam_link" "operator" {
  count           = try(local.config.OAM_SINKS[var.aws_region], "") != "" ? 1 : 0
  label_template  = "$AccountName"
  resource_types  = ["AWS::CloudWatch::Metric", "AWS::Logs::LogGroup"]
  sink_identifier = local.config.OAM_SINKS[var.aws_region]
}
