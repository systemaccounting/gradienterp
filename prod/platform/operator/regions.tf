# ─── the directory's replicas: their resource policies ───
#
# A global table's resource policy does not replicate: each replica (main.tf, `replica` blocks on
# gerp-directory) gets the same org-read policy through a provider alias for its region. The list
# is config.json REGIONS; a region added there is an alias and a block here.

provider "aws" {
  alias  = "eu_west_1"
  region = "eu-west-1"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_eu_west_1" {
  provider     = aws.eu_west_1
  resource_arn = "arn:aws:dynamodb:eu-west-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:eu-west-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}

provider "aws" {
  alias  = "eu_central_1"
  region = "eu-central-1"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_eu_central_1" {
  provider     = aws.eu_central_1
  resource_arn = "arn:aws:dynamodb:eu-central-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:eu-central-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}

provider "aws" {
  alias  = "eu_west_2"
  region = "eu-west-2"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_eu_west_2" {
  provider     = aws.eu_west_2
  resource_arn = "arn:aws:dynamodb:eu-west-2:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:eu-west-2:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}

provider "aws" {
  alias  = "ap_southeast_1"
  region = "ap-southeast-1"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_ap_southeast_1" {
  provider     = aws.ap_southeast_1
  resource_arn = "arn:aws:dynamodb:ap-southeast-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:ap-southeast-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}

provider "aws" {
  alias  = "ap_northeast_1"
  region = "ap-northeast-1"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_ap_northeast_1" {
  provider     = aws.ap_northeast_1
  resource_arn = "arn:aws:dynamodb:ap-northeast-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:ap-northeast-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}

provider "aws" {
  alias  = "ap_southeast_2"
  region = "ap-southeast-2"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_ap_southeast_2" {
  provider     = aws.ap_southeast_2
  resource_arn = "arn:aws:dynamodb:ap-southeast-2:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:ap-southeast-2:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}

provider "aws" {
  alias  = "ap_south_1"
  region = "ap-south-1"
  default_tags {
    tags = { "gerp:stack" = "platform" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

resource "aws_dynamodb_resource_policy" "directory_ap_south_1" {
  provider     = aws.ap_south_1
  resource_arn = "arn:aws:dynamodb:ap-south-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrganizationReads"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:GetItem", "dynamodb:BatchGetItem"]
      Resource  = "arn:aws:dynamodb:ap-south-1:${local.operator_account_id}:table/${aws_dynamodb_table.directory.name}"
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })

  depends_on = [aws_dynamodb_table.directory]
}
