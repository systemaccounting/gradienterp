terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65"
    }
  }

  # State stored in the operator account's own s3 bucket (created by this dir's
  # first apply, then this backend block was added). Backend blocks don't accept
  # variables, so the operator account ID + bucket name are hardcoded.
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    key            = "platform/operator/terraform.tfstate"
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
    tags = { "gerp:stack" = "platform" }
  }
  region = var.aws_region

  # Cross-account assume into the operator sub-account.
  # OrganizationAccountAccessRole is auto-created in every sub-account by
  # AWS Organizations; the management-account caller (admin user / Identity
  # Center session) needs sts:AssumeRole on this role ARN.
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}
