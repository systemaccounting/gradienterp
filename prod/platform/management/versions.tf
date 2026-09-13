terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.44"
    }
  }

  # State lives in the operator account's s3 bucket; the management-account
  # caller assumes OrganizationAccountAccessRole to read/write. Backend blocks
  # don't accept variables, so the operator account ID + bucket name are
  # hardcoded — change here if the org or operator account is re-created.
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    key            = "platform/management/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
    assume_role = {
      role_arn = "arn:aws:iam::185369506315:role/OrganizationAccountAccessRole"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
