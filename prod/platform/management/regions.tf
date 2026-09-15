# ─── regions: a customers OU per region, pinned to it, governed by the landing zone ───
#
# A gerp is one stack applied in one region, and the region is an account's: the SCP on its OU
# denies region-aware actions anywhere else. `REGIONS` in config.json is the list, read here and
# by the create screen; the first entry keeps the OU named `customers` (main.tf) and every other
# gets `customers-<region>`. The landing zone governs every region in the list (control_tower.tf)
# so Account Factory can vend into each OU and Control Tower's baseline lands in each region.

locals {
  config  = jsondecode(file("${path.module}/../../../config.json"))
  regions = keys(local.config.REGIONS)
  # the landing zone returns its governed regions in its own order, whatever order they were sent
  # in, and a list in any other order plans as a change. The regions it already governs go in its
  # order; a new region goes after them, and joins this list once the update shows where AWS put it
  landing_zone_region_order = ["eu-west-1", "ap-southeast-2", "eu-central-1", "us-east-1", "ap-south-1", "ap-northeast-1", "ap-southeast-1", "eu-west-2"]
  governed_regions = concat(
    [for r in local.landing_zone_region_order : r if contains(local.regions, r)],
    [for r in local.regions : r if !contains(local.landing_zone_region_order, r)],
  )
  # the regions beyond the first, which main.tf's `customers` OU and `region_pin` already cover
  other_regions = [for r in local.regions : r if r != var.aws_region]
}

resource "aws_organizations_organizational_unit" "customers_region" {
  for_each = toset(local.other_regions)

  name      = "customers-${each.key}"
  parent_id = aws_organizations_organization.this.roots[0].id
}

data "aws_iam_policy_document" "scp_region_pin_region" {
  for_each = toset(local.other_regions)

  statement {
    effect      = "Deny"
    not_actions = local.region_pin_exceptions
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = [each.key]
    }
  }
}

resource "aws_organizations_policy" "region_pin_region" {
  for_each = toset(local.other_regions)

  name        = "region-pin-${each.key}"
  description = "Deny region-aware actions outside ${each.key}."
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.scp_region_pin_region[each.key].json

  depends_on = [aws_organizations_organization.this]
}

resource "aws_organizations_policy_attachment" "region_pin_region" {
  for_each = toset(local.other_regions)

  policy_id = aws_organizations_policy.region_pin_region[each.key].id
  target_id = aws_organizations_organizational_unit.customers_region[each.key].id
}

# registered with Control Tower the way the customers OU is, so Account Factory vends into it
resource "aws_controltower_baseline" "customers_region" {
  for_each = toset(local.other_regions)

  baseline_identifier = local.ct_baseline_aws_control_tower
  baseline_version    = local.ct_baseline_version
  target_identifier   = aws_organizations_organizational_unit.customers_region[each.key].arn

  parameters {
    key   = "IdentityCenterEnabledBaselineArn"
    value = local.ct_identity_center_enabled_baseline_arn
  }

  depends_on = [aws_controltower_landing_zone.this]
}

# the operator's orchestration role in every account of the OU (operator_trust_stackset.tf);
# an instance per OU so a change to one OU's stacks never touches another's
resource "aws_cloudformation_stack_set_instance" "operator_orchestration_customers_region" {
  for_each = toset(local.other_regions)

  stack_set_name = aws_cloudformation_stack_set.operator_orchestration.name

  deployment_targets {
    organizational_unit_ids = [aws_organizations_organizational_unit.customers_region[each.key].id]
  }

  # IAM is global, so one stack per account is enough — but the OU's region pin denies
  # CloudFormation anywhere but its own region, so the stack is deployed there
  stack_set_instance_region = each.key

  operation_preferences {
    failure_tolerance_percentage = 0
    max_concurrent_percentage    = 100
    region_concurrency_type      = "PARALLEL"
  }
}

output "customers_ous" {
  description = "Every region's customers OU as Account Factory's ManagedOrganizationalUnit parameter, 'name (ou-id)', keyed by region; the provisioner picks by the vend's region."
  value = merge(
    { (var.aws_region) = "customers (${aws_organizations_organizational_unit.customers.id})" },
    { for r, ou in aws_organizations_organizational_unit.customers_region : r => "${ou.name} (${ou.id})" },
  )
}
