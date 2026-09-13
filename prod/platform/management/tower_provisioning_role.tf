###############################################
# Cross-account role for tower's provision_customer lambda.
#
# Operator account assumes this role briefly during the account-vending step
# of customer provisioning. Service Catalog's Account Factory product (used
# by CT for account vending) can only be called from the management account
# or a principal explicitly associated with the AF portfolio. We add this
# role to the AF portfolio principals (below) and grant it the SC perms it
# needs. Account vending now happens via SC ProvisionProduct — Organizations
# CreateAccount is no longer needed.
#
# Trust scoped to the operator account root.
###############################################

resource "aws_iam_role" "tower_provisioning" {
  name = "TowerProvisioning"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${aws_organizations_account.operator.id}:root" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "tower_provisioning" {
  name = "tower-provisioning"
  role = aws_iam_role.tower_provisioning.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read-only Organizations APIs — used to look up the new account ID
        # by name after ProvisionProduct succeeds (SC AF doesn't return the
        # account ID directly in the record). ListAccountsForParent is
        # bill_customer's count of the customers OU.
        Sid    = "OrgsReadOnly"
        Effect = "Allow"
        Action = [
          "organizations:ListAccounts",
          "organizations:ListAccountsForParent",
          "organizations:DescribeAccount",
        ]
        Resource = "*"
      },
      {
        # the org's account quota (L-E619E033), read by bill_customer daily
        # against ListAccounts: Organizations publishes no usage metric, and a
        # raise is a request from this account that takes days.
        Sid      = "OrgAccountQuota"
        Effect   = "Allow"
        Action   = "servicequotas:GetServiceQuota"
        Resource = "*"
      },
      {
        # One invoice unit per gerp, created at provisioning, so AWS issues a
        # real invoice per customer each month instead of one consolidated bill.
        # That invoice's TotalAmount is what tower's bill_customer marks up.
        # Invoicing is a management-account API — this is why the call rides the
        # assumed session rather than running in operator. Free: AWSInvoicing has
        # no chargeable products.
        Sid    = "InvoiceUnitPerCustomer"
        Effect = "Allow"
        Action = [
          "invoicing:CreateInvoiceUnit",
          "invoicing:ListInvoiceUnits",
          "invoicing:GetInvoiceUnit",
          "invoicing:TagResource",
          # the read half, used by bill_customer: the summary carries the
          # TotalAmount the fee is computed from, the pdf is kept as evidence.
          "invoicing:ListInvoiceSummaries",
          "invoicing:GetInvoicePDF",
        ]
        Resource = "*"
      },
      {
        # the end of a closure: tower's close_account assumes this role for the one call only
        # management can make. DescribeAccount is what a re-run reads to see it already happened.
        Sid    = "CloseCustomerAccount"
        Effect = "Allow"
        Action = [
          "organizations:CloseAccount",
          "organizations:DescribeAccount",
        ]
        Resource = "*"
      },
      {
        Sid    = "ServiceCatalogProvisionAFProduct"
        Effect = "Allow"
        Action = [
          "servicecatalog:ProvisionProduct",
          "servicecatalog:DescribeRecord",
          "servicecatalog:DescribeProvisionedProduct",
          "servicecatalog:ListProvisioningArtifacts",
          "servicecatalog:ListLaunchPaths",
        ]
        Resource = "*"
      },
      {
        # an owner's changed login renames their Identity Center user (Account Factory made it from
        # SSOUserEmail): tower's update_owner_email assumes this role for the one store call
        Sid    = "OwnerEmailReachesIdentityCenter"
        Effect = "Allow"
        Action = [
          "identitystore:ListUsers",
          "identitystore:DescribeUser",
          "identitystore:UpdateUser",
        ]
        Resource = "*"
      },
      {
        # SC AF product internally calls Control Tower's CreateManagedAccount
        # (and related) APIs as the calling principal. Without these, the SC
        # ProvisionProduct call returns RECORD_FAILED with AccessDeniedException.
        Sid    = "ControlTowerAccountVending"
        Effect = "Allow"
        Action = [
          "controltower:CreateManagedAccount",
          "controltower:DescribeManagedAccount",
          "controltower:DeregisterManagedAccount",
        ]
        Resource = "*"
      },
    ]
  })
}

# Add TowerProvisioning role to CT's Account Factory portfolio principals so
# it can call ProvisionProduct against the AF product. Portfolio + product IDs
# are stable across CT updates (artifact ID changes — discovered at lambda
# runtime via ListProvisioningArtifacts).
resource "aws_servicecatalog_principal_portfolio_association" "tower_provisioning_af" {
  portfolio_id  = "port-63cyoeboo5tey"
  principal_arn = aws_iam_role.tower_provisioning.arn
}
