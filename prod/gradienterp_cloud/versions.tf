terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.65" }
    archive = { source = "hashicorp/archive", version = "~> 2.8" }
  }

  # State in the operator account's org state bucket (same as the other stacks).
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    key            = "gradienterp_cloud/terraform.tfstate"
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
    tags = { "gerp:stack" = "bff" }
  }
  region = "us-east-1"

  # Cross-account assume into the operator sub-account (this is an operator
  # singleton — the gerp-website front door). Base caller (management admin /
  # Identity Center) needs sts:AssumeRole on this role.
  assume_role {
    role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
  }
}
