# Provider requirements. Module declares, doesn't configure — the root module
# (prod/per_customer/main.tf) owns the provider already assumed into the
# customer sub-account in us-east-1.
#
# Amazon S3 Vectors (aws_s3vectors_*) and the S3_VECTORS storage type on
# aws_bedrockagent_knowledge_base landed in the AWS provider in late 2025.
# Pin to a recent minor (>= 6.45) so validate sees the resource schemas.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.43"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}
