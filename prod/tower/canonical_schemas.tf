###############################################
# Operator-side canonical registry bucket.
#
# Holds canonical JSON for chart_of_accounts, contact_fields, calendar_fields,
# and future registries. Customer agents (running in customer accounts) read
# from here cross-account when their weekly cron fires the canonical-pull
# prompt — they read, diff against own DDB, surface to owner, merge approved
# entries via the write_schema tool (op: merge).
#
# Source of truth in repo: modules/schemas/data/*.json. Operator (human or
# operator-agent) updates the JSON when promoting a pattern; a publishing
# step uploads the latest version into this bucket. New customers see the
# updated content at their first weekly cron tick.
#
# Bucket policy is org-scoped: any principal in the org can read. Lets every
# customer's read_schema (canonical) / seed_schema lambda pull cross-account
# without per-customer IAM plumbing.
###############################################

resource "aws_s3_bucket" "canonical_schemas" {
  bucket        = "${local.stack_prefix}-canonical-${local.operator_account_id}"
  force_destroy = true # canonical JSON is re-uploaded from modules/schemas/data/; safe to drop on replace/teardown
}

resource "aws_s3_bucket_versioning" "canonical_schemas" {
  bucket = aws_s3_bucket.canonical_schemas.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "canonical_schemas" {
  bucket = aws_s3_bucket.canonical_schemas.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "canonical_schemas" {
  bucket                  = aws_s3_bucket.canonical_schemas.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "canonical_schemas" {
  bucket = aws_s3_bucket.canonical_schemas.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# Org-scoped read policy — any principal in the org can s3:GetObject /
# s3:ListBucket. Customer agents' lambdas (read_schema,
# seed_schema) consume this without per-customer plumbing.
resource "aws_s3_bucket_policy" "canonical_schemas" {
  bucket = aws_s3_bucket.canonical_schemas.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgWideRead"
      Effect    = "Allow"
      Principal = "*"
      Action = [
        "s3:GetObject",
        "s3:ListBucket",
      ]
      Resource = [
        aws_s3_bucket.canonical_schemas.arn,
        "${aws_s3_bucket.canonical_schemas.arn}/*",
      ]
      Condition = {
        StringEquals = {
          "aws:PrincipalOrgID" = local.org_ids
        }
      }
    }]
  })
}

# ─── publish canonical content from repo ───
#
# The JSON files in modules/schemas/data/ are the source of truth. Upload
# them on every tower apply so the bucket reflects current canonical. Versioning
# on the bucket gives us per-edit history for rollback.

resource "aws_s3_object" "canonical_chart_of_accounts" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "chart_of_accounts.json"
  source = "${path.module}/../../modules/schemas/data/chart_of_accounts.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/chart_of_accounts.json")

  content_type = "application/json"
}

# the GENERAL canonical rule params (platform tax tables + rates) — seed_schema seeds
# these into each customer's rules-params table, so a tax-year update is a canonical push
# + re-seed, not a code deploy to every tenant's stack.
resource "aws_s3_object" "canonical_rule_params" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "rule_params.json"
  source = "${path.module}/../../modules/schemas/data/rule_params.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/rule_params.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_contact_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "contact_fields.json"
  source = "${path.module}/../../modules/schemas/data/contact_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/contact_fields.json")

  content_type = "application/json"
}

# Deliberately EMPTY (`{}`). The file's presence is what makes `invoice_tags` a registry a firm can
# extend into; no tag ships canonical, because shipping one industry's vocabulary as everyone's
# default is the way to get this wrong. Canonical is a PROMOTION: `registry.extended` announces every
# firm-authored tag, so a tag becomes canonical when n firms independently arrived at it.
resource "aws_s3_object" "canonical_invoice_tags" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "invoice_tags.json"
  source = "${path.module}/../../modules/schemas/data/invoice_tags.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/invoice_tags.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_calendar_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "calendar_fields.json"
  source = "${path.module}/../../modules/schemas/data/calendar_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/calendar_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_labor_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "labor_fields.json"
  source = "${path.module}/../../modules/schemas/data/labor_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/labor_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_note_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "note_fields.json"
  source = "${path.module}/../../modules/schemas/data/note_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/note_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_ledger_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "ledger_fields.json"
  source = "${path.module}/../../modules/schemas/data/ledger_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/ledger_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_task_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "task_fields.json"
  source = "${path.module}/../../modules/schemas/data/task_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/task_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_asset_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "asset_fields.json"
  source = "${path.module}/../../modules/schemas/data/asset_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/asset_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_shipping_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "shipping_fields.json"
  source = "${path.module}/../../modules/schemas/data/shipping_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/shipping_fields.json")

  content_type = "application/json"
}

resource "aws_s3_object" "canonical_item_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "item_fields.json"
  source = "${path.module}/../../modules/schemas/data/item_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/item_fields.json")

  content_type = "application/json"
}

# profile_fields is the OPERATOR-side profile registry schema (gerp-profiles) — read by the
# gerp-website BFF + the optimizer hub, NOT seeded into per-customer registries (deliberately
# absent from modules/schemas/lambdas/seed_schema REGISTRIES). Published here so operator-account
# consumers read one canonical source and the field set + match-keys evolve by re-upload.
resource "aws_s3_object" "canonical_profile_fields" {
  bucket = aws_s3_bucket.canonical_schemas.id
  key    = "profile_fields.json"
  source = "${path.module}/../../modules/schemas/data/profile_fields.json"
  etag   = filemd5("${path.module}/../../modules/schemas/data/profile_fields.json")

  content_type = "application/json"
}

output "canonical_bucket_name" {
  value       = aws_s3_bucket.canonical_schemas.id
  description = "Operator-side canonical registry bucket. Customer agents pull from here on weekly cron."
}
