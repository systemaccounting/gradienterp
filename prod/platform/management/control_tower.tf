###############################################
# AWS Control Tower landing zone.
#
# Replaces the DIY landing zone we built in main.tf (SCPs, CloudTrail org-trail,
# IC config, hand-rolled Account Factory). CT takes over: baseline preventive
# controls, audit/log_archive accounts, account vending via Service Catalog (or
# AFT in phase 5), drift detection.
#
# Manifest format: landing zone v4.0 (April 2025). v4.0 dropped the
# `organizationStructure` block — customers define their own OUs, CT registers
# them. Our `services`/`customers` OUs (defined in main.tf) get registered in
# phase 3 via `aws_controltower_control` enrollment, not here.
#
# Apply takes ~60 min. STS credentials must have >= 60 min validity remaining
# at apply time or the operation aborts midway through landing zone setup.
#
# Audit + log_archive accounts must exist BEFORE this resource applies.
# Implicit dependency via aws_organizations_account.audit/log_archive .id refs.
###############################################

resource "aws_controltower_landing_zone" "this" {
  version = "4.0"

  # CT API requires these service roles to exist at /service-role/ before
  # CreateLandingZone is called.
  depends_on = [
    aws_iam_role_policy_attachment.control_tower_admin_managed,
    aws_iam_role_policy.control_tower_admin_inline,
    aws_iam_role_policy_attachment.control_tower_cloudtrail_managed,
    aws_iam_role_policy.control_tower_stackset_inline,
  ]

  manifest_json = jsonencode({
    # every region a gerp can be built in (config.json REGIONS, regions.tf); a region added
    # here is a landing-zone update, tens of minutes, and Control Tower's baseline (Config
    # recording) lands in it for every enrolled account
    governedRegions = local.governed_regions

    # IAM Identity Center integration. Already enabled in this account
    # (manual one-shot per AGENTS.md); CT integrates with the existing
    # instance and uses it for member-account access provisioning.
    accessManagement = {
      enabled = true
    }

    # Audit account hosts the security roles + Config aggregator.
    securityRoles = {
      enabled   = true
      accountId = aws_organizations_account.audit.id
    }

    # Config integration is required when accessManagement, securityRoles,
    # or backup are enabled. Aggregator lives in audit.
    config = {
      enabled   = true
      accountId = aws_organizations_account.audit.id
      configurations = {
        loggingBucket = {
          retentionDays = 365
        }
        accessLoggingBucket = {
          retentionDays = 365
        }
      }
    }

    # Centralized log target. CT creates a CloudTrail org-trail writing into
    # this account's S3 bucket. Our existing `gerp-org-trail` will be
    # redundant after this and gets retired in phase 4.
    centralizedLogging = {
      enabled   = true
      accountId = aws_organizations_account.log_archive.id
      configurations = {
        loggingBucket = {
          retentionDays = 365
        }
        accessLoggingBucket = {
          retentionDays = 365
        }
      }
    }

    # AWS Backup integration not used at POC scale.
    backup = {
      enabled = false
    }
  })
}
