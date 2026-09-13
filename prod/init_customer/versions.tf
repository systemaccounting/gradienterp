terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.45"
    }
  }

  # Partial backend config — `key` is set per-customer at init time:
  #   terraform init -backend-config="key=<gerp_id>/init.tfstate"
  #
  # A SEPARATE key from per_customer's, and that separation is the entire point of this stack: a
  # statefile is the only real destroy boundary terraform has. Closing a gerp destroys
  # `per_customer` whole — no -target, no state rm — and everything here survives it.
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  default_tags {
    tags = { "gerp:stack" = "init_customer" }
  }
  region = var.aws_region

  assume_role {
    role_arn = "arn:aws:iam::${var.aws_account_id}:role/OperatorOrchestration"
  }
}
