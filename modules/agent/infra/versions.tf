# Provider requirements. Module declares, doesn't configure — the root module
# (prod/customers.tf, once wired) owns provider blocks with per-customer aliases.
#
# AgentCore resources (aws_bedrockagentcore_*) landed in the AWS provider in
# late 2025 following the Oct 2025 GA. Pin to a recent minor so validate sees
# the resource schemas.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.65"
    }
    # the web-chat lambda (chat.tf) is packaged via archive_file
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
    # the wait between the browser recording role's policy and the browser that tests it
    time = {
      source  = "hashicorp/time"
      version = "~> 0.14"
    }
  }
}
