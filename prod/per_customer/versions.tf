terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65"
    }
  }

  # Partial backend config — `key` is set per-customer at init time:
  #   terraform init -backend-config="key=<gerp_id>/terraform.tfstate"
  # State bucket lives in operator account. No assume_role here — the caller
  # must already be running as an operator-account principal (codebuild role
  # in CI; assume-role into operator before terraform locally).
  backend "s3" {
    bucket         = "gradienterp-tfstate-185369506315"
    region         = "us-east-1"
    dynamodb_table = "gradienterp-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {

  # Every resource this root creates — including inside child modules — carries its stack, so
  # standing the stack up locally is one tag query instead of a hand-kept list. Applied here rather
  # than per resource so a resource added later cannot be missed. See scripts/tags.json.
  default_tags {
    tags = { "gerp:stack" = "per_customer" }
  }
  region = var.aws_region

  # Cross-account assume into the customer's sub-account via OperatorOrchestration.
  # Deployed to every customers-OU account by a service-managed CFN stackset
  # (see prod/platform/management/operator_trust_stackset.tf). Trusts the
  # operator account, which is where codebuild runs.
  assume_role {
    role_arn = "arn:aws:iam::${var.aws_account_id}:role/OperatorOrchestration"
  }
}
