terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.65" }
  }

  # State in the operator account's org state bucket (same as the other prod stacks).
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    key            = "openlyoperated_biz/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
    assume_role = {
      role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
    }
  }
}

# Operator sub-account. us-east-1 is required — CloudFront viewer certs must live there,
# and the whole stack (S3 origin + distribution + records) sits in one region.
provider "aws" {

  # Every resource this root creates — including inside child modules — carries its stack, so
  # standing the stack up locally is one tag query instead of a hand-kept list. Applied here rather
  # than per resource so a resource added later cannot be missed. See scripts/tags.json.
  default_tags {
    tags = { "gerp:stack" = "openlyoperated_biz" }
  }
  region = "us-east-1"
  assume_role {
    role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
  }
}
