###############################################
# AWS Control Tower service roles.
#
# Required pre-existing roles for the CreateLandingZone API. The CT console
# wizard creates these silently; via terraform/API they're our responsibility.
#
# All roles must live at the IAM path /service-role/ with the exact role names
# below — CT looks them up by name.
#
# v4.0 dropped the AWSControlTowerConfigAggregatorRoleForOrganizations role
# (CT migrated to a service-linked config aggregator), so it is not created here.
###############################################

# AWSControlTowerAdmin — primary CT service role. CT assumes this to manage
# the landing zone (stacksets, OU governance, drift detection).
resource "aws_iam_role" "control_tower_admin" {
  name = "AWSControlTowerAdmin"
  path = "/service-role/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "controltower.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "control_tower_admin_managed" {
  role       = aws_iam_role.control_tower_admin.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSControlTowerServiceRolePolicy"
}

resource "aws_iam_role_policy" "control_tower_admin_inline" {
  name = "AWSControlTowerAdminPolicy"
  role = aws_iam_role.control_tower_admin.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ec2:DescribeAvailabilityZones"
      Resource = "*"
    }]
  })
}

# AWSControlTowerCloudTrailRole — CloudTrail assumes this to publish CT's
# baseline CloudTrail logs to CloudWatch Logs.
resource "aws_iam_role" "control_tower_cloudtrail" {
  name = "AWSControlTowerCloudTrailRole"
  path = "/service-role/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "control_tower_cloudtrail_managed" {
  role       = aws_iam_role.control_tower_cloudtrail.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSControlTowerCloudTrailRolePolicy"
}

# AWSControlTowerStackSetRole — CloudFormation assumes this to deploy CT's
# baseline stacksets into enrolled member accounts via AWSControlTowerExecution.
resource "aws_iam_role" "control_tower_stackset" {
  name = "AWSControlTowerStackSetRole"
  path = "/service-role/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudformation.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "control_tower_stackset_inline" {
  name = "AWSControlTowerStackSetRolePolicy"
  role = aws_iam_role.control_tower_stackset.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = "arn:aws:iam::*:role/AWSControlTowerExecution"
    }]
  })
}
