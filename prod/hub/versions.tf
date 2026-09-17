terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65"
    }
  }

  # Partial backend config — `key` is set per hub at init time:
  #   terraform init -backend-config="key=hubs/<hub_id>/terraform.tfstate"
  # State bucket lives in the operator account; the caller runs as an operator-account principal
  # (the tower-hub codebuild role, or an assumed role locally).
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  default_tags {
    tags = { "gerp:stack" = "hub" }
  }
  region = var.aws_region

  # The hub's account is vended into the hubs OU; OperatorOrchestration is deployed there by the
  # trust stackset and trusts the operator account, where the build runs.
  assume_role {
    role_arn = "arn:aws:iam::${var.aws_account_id}:role/OperatorOrchestration"
  }
}
