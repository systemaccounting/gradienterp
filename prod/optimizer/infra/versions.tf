terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65"
    }
  }

  # Operator-account state — same bucket + lock as every other operator stack,
  # key is the optimizer leaf. Backend + provider assume OrganizationAccountAccessRole
  # in operator, which trusts management — so apply under AWS_PROFILE=default,
  # NOT operator-org (see prod/AGENTS.md §local apply).
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    key            = "optimizer/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
    assume_role = {
      role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
    }
  }
}

provider "aws" {

  # Every resource this root creates — including inside child modules — carries its stack, so
  # standing the stack up locally is one tag query instead of a hand-kept list. Applied here rather
  # than per resource so a resource added later cannot be missed. See scripts/tags.json.
  default_tags {
    tags = { "gerp:stack" = "optimizer" }
  }
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}
