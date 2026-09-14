###############################################
# AWS Organizations management-account skeleton.
#
# Runs in the management account. Establishes the org,
# OUs, sub-accounts, SCPs, and the CloudTrail org-trail aggregator. Workloads
# deliberately do NOT live here — that's why we're creating sub-accounts.
#
# OU layout:
#   root
#     ├── management   (this account; never moves)
#     ├── services     (operator — single account hosting tower, bus, api+biz, public store)
#     └── customers    (per-customer sub-accounts, including the operator's own gradienterp books)
#
# Apply ordering:
#   1. terraform apply (this dir)              → org enabled, OUs, sub-accounts created
#   2. terraform apply prod/platform/operator/ → state bucket, IAM IC, EventBridge bus
#   3. terraform init -migrate-state           → move this state into the s3 backend
#
# Once an account is created via aws_organizations_account, AWS auto-creates
# OrganizationAccountAccessRole inside it. The management account assumes that
# role for cross-account terraform applies (see prod/per_customer/, tower/).
###############################################

data "aws_caller_identity" "current" {}

###############################################
# Organization
###############################################

resource "aws_organizations_organization" "this" {
  feature_set = "ALL"

  enabled_policy_types = [
    "SERVICE_CONTROL_POLICY",
    "TAG_POLICY",
  ]

  # Service-linked roles AWS needs in the management account for org-wide
  # services. Adding here is one-shot; removing requires manual cleanup.
  # config.amazonaws.com and member.org.stacksets.cloudformation.amazonaws.com
  # are auto-enabled by Control Tower during landing zone setup; we list them
  # explicitly so terraform doesn't try to revoke them on subsequent applies.
  # (Pre-launch CT check forbids config trusted access; CT enables it itself
  # once the landing zone exists.)
  aws_service_access_principals = [
    "cloudtrail.amazonaws.com",
    "config.amazonaws.com",
    "controltower.amazonaws.com",
    "member.org.stacksets.cloudformation.amazonaws.com",
    "sso.amazonaws.com",
    "ram.amazonaws.com",
  ]
}

###############################################
# OUs
###############################################

resource "aws_organizations_organizational_unit" "services" {
  name      = "services"
  parent_id = aws_organizations_organization.this.roots[0].id
}

resource "aws_organizations_organizational_unit" "customers" {
  name      = "customers"
  parent_id = aws_organizations_organization.this.roots[0].id
}

# Hubs: the accounts that route between gerps (prod/hub), one per region. No region pin — a hub
# is vended into the region it serves and holds no data.
resource "aws_organizations_organizational_unit" "hubs" {
  name      = "hubs"
  parent_id = aws_organizations_organization.this.roots[0].id
}

# Security OU holds Control Tower's audit + log_archive accounts. CT requires
# both service-integration accounts to live in the same OU directly under root.
# This OU is not subject to our region-pin SCP — CT applies its own preventive
# controls and managing both stacks would be redundant.
resource "aws_organizations_organizational_unit" "security" {
  name      = "security"
  parent_id = aws_organizations_organization.this.roots[0].id
}

###############################################
# Sub-accounts
#
# `aws_organizations_account` creates a NEW sub-account under a parent OU. Once
# created, AWS auto-provisions OrganizationAccountAccessRole inside the new
# account — that's how cross-account terraform applies reach in.
#
# Account closure is a separate process with a 90-day waiting period and must
# be initiated via the root user of the sub-account, not via terraform destroy.
# `close_on_deletion = true` initiates closure but doesn't wait for it.
###############################################

resource "aws_organizations_account" "operator" {
  # Single services-OU account. Hosts everything operator-singleton: tower
  # (signup, orchestrator, customers DDB, codebuild), the shared EventBridge
  # bus, Step Functions, the s3 state bucket + DDB lock table, IAM Identity
  # Center config, the api.openlyoperated.biz lambdas + gateway, the
  # openlyoperated.biz frontend (CloudFront + S3), the publisher lambda
  # that filters openly-operated events from the bus and writes to the
  # materialized public DDB ledger + S3 event archive (which also live here).
  # IAM enforces the control-plane / public-data-plane boundary inside the
  # account; splitting into separate accounts isn't justified at this scale.
  name      = "operator"
  email     = var.operator_account_email
  parent_id = aws_organizations_organizational_unit.services.id

  role_name         = "OrganizationAccountAccessRole"
  close_on_deletion = true

  lifecycle {
    ignore_changes = [role_name]
  }
}

resource "aws_organizations_account" "gradienterp" {
  # The operator's own books — gradienterp the company, running on gradienterp
  # the platform. A normal customer-OU sub-account: own agent, accounting,
  # inventory, journal. Public-facing books visible at openlyoperated.biz
  # alongside every other openly-operated customer's books — not a privileged
  # citizen, operator running on its own platform validates it the same way customers will use it.
  name      = "gradienterp"
  email     = var.gradienterp_account_email
  parent_id = aws_organizations_organizational_unit.customers.id

  role_name         = "OrganizationAccountAccessRole"
  close_on_deletion = true

  lifecycle {
    ignore_changes = [role_name]
  }
}

# Control Tower service-integration accounts. CT's manifest references these
# by ID for securityRoles, config aggregator, and centralized logging targets.
# Both go in the security OU directly under root per CT's "service integration
# accounts must share an OU directly under root" rule.

resource "aws_organizations_account" "audit" {
  name      = "audit"
  email     = var.audit_account_email
  parent_id = aws_organizations_organizational_unit.security.id

  role_name         = "OrganizationAccountAccessRole"
  close_on_deletion = true

  lifecycle {
    ignore_changes = [role_name]
  }
}

resource "aws_organizations_account" "log_archive" {
  name      = "log_archive"
  email     = var.log_archive_account_email
  parent_id = aws_organizations_organizational_unit.security.id

  role_name         = "OrganizationAccountAccessRole"
  close_on_deletion = true

  lifecycle {
    ignore_changes = [role_name]
  }
}

###############################################
# SCPs — service control policies
#
# Attached at OU/root level; enforced for every account in scope (excluding
# the management account, which SCPs cannot restrict).
###############################################

# Deny leaving the organization (accidental or adversarial).
data "aws_iam_policy_document" "scp_deny_leave_org" {
  statement {
    effect    = "Deny"
    actions   = ["organizations:LeaveOrganization"]
    resources = ["*"]
  }
}

resource "aws_organizations_policy" "deny_leave_org" {
  name        = "deny-leave-organization"
  description = "Prevent child accounts from leaving the org."
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.scp_deny_leave_org.json

  # Org enable lags policy creation — without depends_on, terraform parallelizes
  # them and CreatePolicy fails with AWSOrganizationsNotInUseException.
  depends_on = [aws_organizations_organization.this]
}

resource "aws_organizations_policy_attachment" "deny_leave_org_root" {
  policy_id = aws_organizations_policy.deny_leave_org.id
  target_id = aws_organizations_organization.this.roots[0].id
}

# Region pin: deny region-aware actions outside us-east-1. Global services
# (IAM, Org, CloudFront, Route 53, STS, Support, tag, Global Accelerator) are
# excepted because they don't honor aws:RequestedRegion. `bedrock:*` is also
# excepted: cross-region inference profiles (`us.anthropic.claude-...`) route
# transparently to us-east-1/us-east-2/us-west-2 by design, and the region
# pin would otherwise fire on the failover legs. AgentCore itself is
# `bedrock-agentcore:*` (separate service principal) and stays pinned.
# the actions a pin never denies, in every pin (this OU's, each other region's customers OU's,
# the services OU's): global services, which do not honor aws:RequestedRegion, and the few calls
# a gerp makes into another region on purpose
locals {
  region_pin_exceptions = [
    "iam:*",
    "organizations:*",
    "cloudfront:*",
    "route53:*",
    "support:*",
    "sts:*",
    "tag:*",
    "globalaccelerator:*",
    "bedrock:*",
    "aws-marketplace:*", # the model agreement a vend makes is a Marketplace call, global like bedrock's profiles
    "s3:Get*",           # a gerp reads the operator's us-east-1 buckets (artifacts, canonical schemas,
    "s3:List*",          # the tfstate) from its own region; writes stay pinned
    "events:PutEvents",  # an addressed event is put on the recipient hub's bus, in the recipient's region
  ]
}

data "aws_iam_policy_document" "scp_region_pin" {
  statement {
    effect      = "Deny"
    not_actions = local.region_pin_exceptions
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = ["us-east-1"]
    }
  }
}

resource "aws_organizations_policy" "region_pin" {
  name        = "region-pin-us-east-1"
  description = "Deny region-aware actions outside us-east-1."
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.scp_region_pin.json

  depends_on = [aws_organizations_organization.this]
}

# the services OU (the operator) works in every region a gerp can be built in: the artifact
# bucket, the ops sink and the hub build per region are its (prod/tower regions.tf); pinned to
# that list, denied everywhere else
data "aws_iam_policy_document" "scp_region_pin_platform" {
  statement {
    effect      = "Deny"
    not_actions = local.region_pin_exceptions
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = local.regions
    }
  }
}

resource "aws_organizations_policy" "region_pin_platform" {
  name        = "region-pin-platform"
  description = "Deny region-aware actions outside the regions a gerp can be built in (config.json REGIONS)."
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.scp_region_pin_platform.json

  depends_on = [aws_organizations_organization.this]
}

resource "aws_organizations_policy_attachment" "region_pin_services" {
  policy_id = aws_organizations_policy.region_pin_platform.id
  target_id = aws_organizations_organizational_unit.services.id
}

resource "aws_organizations_policy_attachment" "region_pin_customers" {
  policy_id = aws_organizations_policy.region_pin.id
  target_id = aws_organizations_organizational_unit.customers.id
}

# Note: the protect-cloudtrail SCP and the gerp-org-trail CloudTrail
# (with its mgmt-account S3 bucket) were removed when Control Tower took over
# in May 2026. CT manages its own trail in the log_archive account and
# protects it via baseline preventive controls.
