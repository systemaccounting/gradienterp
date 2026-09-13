###############################################
# IAM Identity Center — permission sets + user assignments.
#
# IC instance is a one-shot manual enable in the console (org-level service,
# only the management account can create it). This terraform manages everything
# downstream of that: permission sets, account assignments. Users themselves
# are created manually via the IC console (until/unless we plug in an external
# IdP); this dir looks the operator's user up by username via data source.
###############################################

data "aws_ssoadmin_instances" "this" {}

locals {
  # An org has at most one IC instance; auto-discover so the ARN/identity-store
  # ID don't get hardcoded.
  ic_instance_arn   = tolist(data.aws_ssoadmin_instances.this.arns)[0]
  identity_store_id = tolist(data.aws_ssoadmin_instances.this.identity_store_ids)[0]
}

# Look up the operator's admin user — created manually via IC console.
# Fails at plan time if the username doesn't exist; that's the right behavior.
data "aws_identitystore_user" "operator_admin" {
  identity_store_id = local.identity_store_id

  alternate_identifier {
    unique_attribute {
      attribute_path  = "UserName"
      attribute_value = var.operator_admin_username
    }
  }
}

###############################################
# OperatorAdmin permission set — full admin across the org.
###############################################

resource "aws_ssoadmin_permission_set" "operator_admin" {
  instance_arn     = local.ic_instance_arn
  name             = "OperatorAdmin"
  description      = "Full admin. Operator-only; not assigned to engineers or customers."
  session_duration = "PT12H"
}

resource "aws_ssoadmin_managed_policy_attachment" "operator_admin" {
  instance_arn       = local.ic_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.operator_admin.arn
  managed_policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

###############################################
# Account assignments — operator's user gets OperatorAdmin on every account.
###############################################

resource "aws_ssoadmin_account_assignment" "operator_admin_management" {
  instance_arn       = local.ic_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.operator_admin.arn

  principal_id   = data.aws_identitystore_user.operator_admin.user_id
  principal_type = "USER"

  target_id   = data.aws_caller_identity.current.account_id
  target_type = "AWS_ACCOUNT"
}

resource "aws_ssoadmin_account_assignment" "operator_admin_operator" {
  instance_arn       = local.ic_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.operator_admin.arn

  principal_id   = data.aws_identitystore_user.operator_admin.user_id
  principal_type = "USER"

  target_id   = aws_organizations_account.operator.id
  target_type = "AWS_ACCOUNT"
}

resource "aws_ssoadmin_account_assignment" "operator_admin_gradienterp" {
  instance_arn       = local.ic_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.operator_admin.arn

  principal_id   = data.aws_identitystore_user.operator_admin.user_id
  principal_type = "USER"

  target_id   = aws_organizations_account.gradienterp.id
  target_type = "AWS_ACCOUNT"
}
