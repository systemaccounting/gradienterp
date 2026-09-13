###############################################
# Control Tower OU enrollment.
#
# Registers our existing services + customers OUs with CT by enabling the
# AWSControlTowerBaseline on each. Side effects:
#   - all member accounts get the AWSControlTowerExecution role
#   - mandatory preventive controls applied (deny region-disable, deny CT
#     bucket tampering, etc)
#   - detective controls activate Config recorders in every member account
#     (these are NEW running costs — Config rules per resource per region)
#
# AWSControlTowerBaseline depends on IdentityCenterBaseline being enabled on
# the management account (CT did this automatically in phase 2). Parameter
# value is the enabledbaseline ARN of the IC baseline.
#
# Baseline identifiers are global (not per-account); same ARN works for any
# CT customer in us-east-1. AWSControlTowerBaseline ARN looked up via
# `aws controltower list-baselines`.
###############################################

locals {
  ct_baseline_aws_control_tower = "arn:aws:controltower:us-east-1::baseline/17BSJV3IGJ2QSGA2"
  # Baseline 5.0 is the version compatible with landing zone 4.0. The numbering
  # is offset — see the AWSControlTowerBaseline compatibility table in the CT
  # docs. Landing zone 3.2-3.3 used baseline 4.0; 4.0 jumped to baseline 5.0
  # because v4.0 dropped per-member Config Aggregation Authorizations.
  ct_baseline_version = "5.0"

  # IC baseline enabled by CT on the mgmt account during landing zone setup.
  # Looked up via `aws controltower list-enabled-baselines` — IdentityCenterBaseline
  # (baseline/LN25R72TTG6IGPTQ) targeting the mgmt account.
  ct_identity_center_enabled_baseline_arn = "arn:aws:controltower:us-east-1:${data.aws_caller_identity.current.account_id}:enabledbaseline/XAHNKO1VOWI6R50OA"
}

resource "aws_controltower_baseline" "services" {
  baseline_identifier = local.ct_baseline_aws_control_tower
  baseline_version    = local.ct_baseline_version
  target_identifier   = aws_organizations_organizational_unit.services.arn

  parameters {
    key   = "IdentityCenterEnabledBaselineArn"
    value = local.ct_identity_center_enabled_baseline_arn
  }
}

resource "aws_controltower_baseline" "customers" {
  baseline_identifier = local.ct_baseline_aws_control_tower
  baseline_version    = local.ct_baseline_version
  target_identifier   = aws_organizations_organizational_unit.customers.arn

  parameters {
    key   = "IdentityCenterEnabledBaselineArn"
    value = local.ct_identity_center_enabled_baseline_arn
  }
}

# The hubs OU is registered the same way, so Account Factory can vend into it.
resource "aws_controltower_baseline" "hubs" {
  baseline_identifier = local.ct_baseline_aws_control_tower
  baseline_version    = local.ct_baseline_version
  target_identifier   = aws_organizations_organizational_unit.hubs.arn

  parameters {
    key   = "IdentityCenterEnabledBaselineArn"
    value = local.ct_identity_center_enabled_baseline_arn
  }
}
