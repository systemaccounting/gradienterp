# ─── the artifact bucket — lambda deploys without terraform ───
#
# TF owns shape, S3 owns bytes. scripts/deploy.sh pushes each fleet lambda's
# .build zip (produced by the module's own archive_file — the single build
# recipe) to a VERSIONED stable key = "<gerp:src-dir>.zip", annotates the
# version with provenance {src_dir, src_sha256, built_at} + agent-readable
# release notes, then fans update-function-code across tenant accounts.
# Deployed state is NEVER recorded here — it's one get-function call away
# (CodeSha256), and mirroring queryable state is how records drift.
#
# Org-scoped read: tenant deploy fans pull the zip cross-account, and a gerp's
# agent reads the release annotation to decide/explain a self-update
# (modules/agent/TODO.md § tool deployment artifacts). Writes stay operator-only.

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.stack_prefix}-artifacts-${local.operator_account_id}"
  force_destroy = true # artifacts re-push from .build zips; safe to drop on teardown
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # annotations inherit this; org read needs no KMS grants
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# keep-last-N via age: noncurrent versions (superseded artifacts) expire after 60
# days — and their annotations die with them, so the 1k/object cap self-manages.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 60
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgRead"
      Effect    = "Allow"
      Principal = "*"
      Action = [
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:GetObjectAttributes",
        "s3:GetObjectVersionAttributes",
        "s3:GetObjectAnnotation",
        "s3:ListObjectAnnotations",
        "s3:GetObjectTagging", # data.aws_s3_object reads tags at plan
        "s3:GetObjectVersionTagging",
        "s3:ListBucket",
        "s3:ListBucketVersions",
      ]
      Resource = [
        aws_s3_bucket.artifacts.arn,
        "${aws_s3_bucket.artifacts.arn}/*",
      ]
      Condition = {
        StringEquals = { "aws:PrincipalOrgID" = local.org_ids }
      }
    }]
  })
}

output "artifacts_bucket" {
  description = "Versioned lambda-artifact bucket (stable key per function = <gerp:src-dir>.zip; provenance + release annotations per version). Pushed by scripts/deploy.sh; org-read for tenant deploy fans + agent release-notes reads."
  value       = aws_s3_bucket.artifacts.id
}
