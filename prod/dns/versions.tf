terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.42" }
  }

  # State in the operator account's org state bucket (same as the other stacks).
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    key            = "dns/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
    assume_role = {
      role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  # Operator sub-account — DNS is an operator singleton (both domains).
  assume_role {
    role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
  }
}
