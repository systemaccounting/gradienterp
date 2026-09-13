output "organization_id" {
  description = "Org ID; consumed by prod/platform/operator/ for cross-account RAM shares + the EventBridge bus."
  value       = aws_organizations_organization.this.id
}

output "organization_root_id" {
  description = "Root ID; SCP / OU attachments reference this when targeting org-wide."
  value       = aws_organizations_organization.this.roots[0].id
}

output "services_ou_id" {
  value = aws_organizations_organizational_unit.services.id
}

output "customers_ou_id" {
  value = aws_organizations_organizational_unit.customers.id
}

output "operator_account_id" {
  description = "Single services-OU account hosting tower, the shared bus, api + biz frontend, the public materialized store. Cross-account terraform applies (prod/platform/operator/, prod/tower/, prod/api_openlyoperated/, prod/openlyoperated_biz/) all assume OrganizationAccountAccessRole into this account."
  value       = aws_organizations_account.operator.id
}

output "gradienterp_account_id" {
  description = "The operator's own openly-operated business books (gradienterp the company, running on gradienterp the platform). Treated identically to any other customer; not a privileged citizen."
  value       = aws_organizations_account.gradienterp.id
}

output "tower_provisioning_role_arn" {
  description = "Role ARN that operator-account lambdas assume to vend new customer sub-accounts via SC Account Factory. Associated with CT's AF portfolio so it can call ProvisionProduct."
  value       = aws_iam_role.tower_provisioning.arn
}

output "ct_af_portfolio_id" {
  description = "Service Catalog portfolio ID for CT's AWS Control Tower Account Factory Portfolio. Stable across CT updates."
  value       = "port-63cyoeboo5tey"
}

output "ct_af_product_id" {
  description = "Service Catalog product ID for CT's Account Factory product. Stable across CT updates (active provisioning artifact / template version is discovered at lambda runtime)."
  value       = "prod-zgoj6xklupj4s"
}

output "ct_af_path_id" {
  description = "Service Catalog launch path ID for the AF product via the AF portfolio. Required by ProvisionProduct call."
  value       = "lpv3-63cyoeboo5tey"
}

output "customers_ou_managed_name" {
  description = "ManagedOrganizationalUnit parameter format for SC AF ProvisionProduct calls vending into the customers OU. Format: 'name (ou-id)'."
  value       = "customers (${aws_organizations_organizational_unit.customers.id})"
}

output "hubs_ou_id" {
  value = aws_organizations_organizational_unit.hubs.id
}

output "hubs_ou_managed_name" {
  description = "The same format, for a hub's vend."
  value       = "hubs (${aws_organizations_organizational_unit.hubs.id})"
}

output "identity_store_id" {
  description = "The org's IAM Identity Center identity store — tower's update_owner_email renames the owner's user in it."
  value       = local.identity_store_id
}
