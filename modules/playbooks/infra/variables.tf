############################################
# module contract — tower passes these in per customer via
# prod/per_customer/main.tf. The default aws provider is already assumed into
# the customer sub-account (us-east-1); this module declares no provider block.
#
# This module stands up a per-customer Amazon Bedrock Knowledge Base backed by
# Amazon S3 Vectors. It holds the agent's how-to playbooks; the agent retrieves
# from it. Content is pushed inline LATER by a separate script via the CUSTOM
# data source — this module provisions the KB but does NOT ingest.
############################################

variable "gerp_id" {
  description = "Logical identifier for this customer (resource-name suffix)."
  type        = string
}

variable "aws_region" {
  description = "AWS region for the customer's sub-account."
  type        = string
}

variable "operator_account_id" {
  description = "AWS account ID hosting the operator side. Mirrors the agent module's variable; kept for naming/cross-account parity. Default targets the live operator account."
  type        = string
  default     = "185369506315"
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}
