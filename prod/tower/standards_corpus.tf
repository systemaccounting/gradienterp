# ─────────────────────────────────────────────────────────────────────────────
# Shared standards corpus.
#
# A STANDARD is the thing you apply; compliance is the outcome of applying it. This bucket holds
# the former. Standards are network property — the Ohio sales-tax rate is the same fact for every
# Ohio business, and gross margin means the same thing to every firm reporting under GAAP — so they
# live in ONE operator bucket, keyed by what they are, not who asked:
#
#   <scope>/<domain>[/<industry>].md          ← ROOT: the curated corpus
#   _contrib/<aws_account_id>/<same path>.md  ← a gerp's contributions
#
# `scope` is the population a standard governs, which generalizes jurisdiction: `us`, `ohio`, `ca`
# are jurisdictions; `gaap`, `ifrs` are standards bodies. Same key shape either way, because the
# question a reader asks is identical — "what governs me here". Examples:
#
#   us/labor/hiring.md            ← I-9, federal new-hire reporting
#   ca/labor/hiring.md            ← DE-4
#   ohio/tax/sales.md             ← the rate + filing cadence
#   gaap/analysis/gross-margin.md ← the definition an analysis must use to stay comparable
#   gaap/inventory/costing.md     ← FIFO / LIFO, and what each commits you to
#
# Statutes and professional conventions differ in AUTHORITY, not in kind — both are standards a
# firm applies and neither is about any particular firm. The one asymmetry worth encoding in a note:
# a statutory fact has ONE right answer, while a method may have legitimate VARIANTS (adjusted
# EBITDA is whatever the presenter says). A method note names the variants and what each commits
# you to; which one a firm elected is that firm's own record, not the corpus's.
#
# The write path is contribute-then-curate (the git model): every key is single-writer. A gerp's
# agent writes ONLY under its own _contrib/<its account id>/ — enforced here by
# ${aws:PrincipalAccount} in the bucket policy, so a compromised tenant can poison nothing but its
# own namespace. Root is written only by the operator-account curator (the hub agent's scheduled
# curation task), which verifies sources and promotes contributions. Readers GET the root key
# first; on miss they research and contribute.
#
# What NEVER lands here is anything about a particular business: which obligations apply to ME,
# which costing method I elected, whether I filed. That's the gerp's own cabinet
# (`compliance/_index.md` via manage_storage) and `modules/compliance` — the outcome side.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "standards_corpus" {
  bucket = "${local.stack_prefix}-standards-${local.operator_account_id}"
}

# corrections are rewrites; versioning is the audit trail
resource "aws_s3_bucket_versioning" "standards_corpus" {
  bucket = aws_s3_bucket.standards_corpus.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "standards_corpus" {
  bucket = aws_s3_bucket.standards_corpus.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "standards_corpus" {
  bucket                  = aws_s3_bucket.standards_corpus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "standards_corpus" {
  bucket = aws_s3_bucket.standards_corpus.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "OrgWideRead"
        Effect    = "Allow"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.standards_corpus.arn,
          "${aws_s3_bucket.standards_corpus.arn}/*",
        ]
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = local.org_ids
          }
        }
      },
      {
        # a tenant writes only its own contribution namespace — the prefix IS the writer's
        # account id, so the scoping is mechanical, org-wide, and needs no per-tenant edits
        Sid       = "ContribOwnPrefixOnly"
        Effect    = "Allow"
        Principal = "*"
        Action    = ["s3:PutObject"]
        Resource  = "${aws_s3_bucket.standards_corpus.arn}/_contrib/$${aws:PrincipalAccount}/*"
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = local.org_ids
          }
        }
      },
    ]
  })
}

# ─── the platform's own practice notes, from the repo ───
#
# Most of the corpus is agent-contributed then curator-promoted. These are different: the
# PLATFORM's recommended operating practices (scope `gerp`), authored in the repo so they are
# reviewable and arguable in public — a pull request is how you disagree with one. They upload
# to the same root keys agents read, so `get_standard("gerp/scheduling.md")` needs no special
# path. A firm electing a different practice records that as its own instruction; it never
# edits the note.

resource "aws_s3_object" "platform_standards" {
  # scope-dir files only — standards/README.md documents the dir, it is not a standard
  for_each = toset([
    for f in fileset("${path.module}/../../standards", "**/*.md") :
    f if length(split("/", f)) > 1
  ])

  bucket       = aws_s3_bucket.standards_corpus.id
  key          = each.value
  source       = "${path.module}/../../standards/${each.value}"
  etag         = filemd5("${path.module}/../../standards/${each.value}")
  content_type = "text/markdown"
}

output "standards_corpus_bucket" {
  description = "The shared standards corpus bucket. Agents read root, contribute under _contrib/<their account id>/; the hub curator verifies and promotes."
  value       = aws_s3_bucket.standards_corpus.bucket
}
