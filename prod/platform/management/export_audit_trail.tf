# Downloads of a customer's data export, logged where the customer's own account closure cannot
# reach them.
#
# A gerp keeps running 15 billed days after closure so the owner can pull their export, then
# everything in that account is deleted — including any in-account record that the download
# happened. An ORGANIZATION trail writes to a bucket here in management, so its events outlive the
# member account they came from. That is the whole reason this exists.
#
# A SEPARATE trail rather than selectors on Control Tower's BaselineCloudTrail: that trail is CT's
# to manage, and terraform can only set selectors on a trail it owns. Trails are independent, so
# this one carries data events only (`include_management_events = false`) and duplicates nothing.
#
# Scoped to ONE IDENTITY, not to a prefix and not to everything. A gerp's agent reads documents all
# day; the event worth keeping happens about once per customer, ever. `modules/export` issues its
# download credential under the fixed session name `gerp-export`, and the reader role's trust policy
# enforces that name — so an assumed-role arn ending `/gerp-export` is exactly a customer pulling
# their export and nothing else.
#
# The name has to be FIXED because advanced event selectors offer Equals / StartsWith / EndsWith and
# no substring match. An assumed-role arn is `arn:aws:sts::<account>:assumed-role/<role>/<session>`,
# the account varies per gerp, so only a constant tail is matchable. WHICH export was downloaded
# comes from the object key on the event, not from the session name.

variable "export_audit_enabled" {
  description = "Log S3 data events for the export reader identity to an org trail in management. Off leaves a download unevidenced once the customer's account is deleted."
  type        = bool
  default     = true
}

locals {
  export_audit_bucket = "gerp-export-audit-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "export_audit" {
  count  = var.export_audit_enabled ? 1 : 0
  bucket = local.export_audit_bucket
}

resource "aws_s3_bucket_public_access_block" "export_audit" {
  count                   = var.export_audit_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.export_audit[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The record of who took their data has to outlive the dispute it would settle, not the download.
resource "aws_s3_bucket_lifecycle_configuration" "export_audit" {
  count  = var.export_audit_enabled ? 1 : 0
  bucket = aws_s3_bucket.export_audit[0].id

  rule {
    id     = "retain-seven-years"
    status = "Enabled"
    filter {}
    expiration { days = 2557 }
  }
}

resource "aws_s3_bucket_policy" "export_audit" {
  count  = var.export_audit_enabled ? 1 : 0
  bucket = aws_s3_bucket.export_audit[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.export_audit[0].arn
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        # BOTH paths, or CreateTrail rejects the policy: an org trail writes member accounts'
        # events under the ORGANIZATION id, and its own under the management ACCOUNT id. The org
        # path is the one that makes a closed member's history still readable here.
        Resource = [
          "${aws_s3_bucket.export_audit[0].arn}/AWSLogs/${aws_organizations_organization.this.id}/*",
          "${aws_s3_bucket.export_audit[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
        ]
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" } }
      },
    ]
  })
}

resource "aws_cloudtrail" "export_audit" {
  count                      = var.export_audit_enabled ? 1 : 0
  name                       = "gerp-export-audit"
  s3_bucket_name             = aws_s3_bucket.export_audit[0].id
  is_organization_trail      = true
  is_multi_region_trail      = true
  enable_log_file_validation = true

  advanced_event_selector {
    name = "export downloads only"

    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }
    # the whole point: one identity, org-wide. Every gerp's reader role ends in this session name.
    field_selector {
      field     = "userIdentity.arn"
      ends_with = ["/gerp-export"]
    }
  }

  depends_on = [aws_s3_bucket_policy.export_audit]
}

output "export_audit_bucket" {
  description = "Where export-download events land. Survives the member account they came from."
  value       = var.export_audit_enabled ? aws_s3_bucket.export_audit[0].id : ""
}
