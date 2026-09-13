###############################################
# Read-only role for the owner app's BFF (operator account): the org's account count against
# its quota, for the "accounts currently available" line on the landing and create screens.
# Organizations answers only from the management account, so the reader assumes in. Two reads,
# nothing else; trust scoped to the BFF's role.
###############################################

resource "aws_iam_role" "capacity_read" {
  name = "GerpCapacityRead"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${aws_organizations_account.operator.id}:role/gerp-cloud-bff" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "capacity_read" {
  name = "capacity-read"
  role = aws_iam_role.capacity_read.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CountAccounts"
        Effect   = "Allow"
        Action   = "organizations:ListAccounts"
        Resource = "*"
      },
      {
        Sid      = "ReadAccountQuota"
        Effect   = "Allow"
        Action   = "servicequotas:GetServiceQuota"
        Resource = "*"
      },
    ]
  })
}

output "capacity_read_role_arn" {
  description = "The read-only role the owner app's BFF assumes to count the org's accounts against its quota."
  value       = aws_iam_role.capacity_read.arn
}
