variable "gerp_id" {
  description = "Logical instance identifier. The same value per_customer is applied with — every name in this stack is derived from it, which is how per_customer finds these by data source."
  type        = string
}

variable "aws_account_id" {
  description = "The customer's AWS sub-account. The provider assumes OperatorOrchestration into it."
  type        = string
}

variable "aws_region" {
  description = "AWS region for the customer's sub-account."
  type        = string
  default     = "us-east-1"
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`), from repo-root config.json. Must match what per_customer is applied with or the data-source lookups there miss."
  type        = string
  default     = "gerp"
}

variable "export_retention_days" {
  description = "How long an export in the bucket's exports/ prefix lives. The CLOSURE export is deleted with this whole stack at the end of the 15-day window; this governs the on-demand ones, taken against a gerp that keeps going — each a full copy of the books on the customer's own storage bill, and a snapshot that does not honour later deletions."
  type        = number
  default     = 30
}

variable "export_invoker_role_arn" {
  description = "The gerp-cloud BFF's role, allowed to invoke this gerp's export lambda cross-account so the owner can take their data out from the account screen. Set on every gerp — every owner can leave — and it is what keeps the download working after closure, when the agent that used to create the links has been destroyed. Deliberate literal default, the artifact_bucket convention."
  type        = string
  default     = "arn:aws:iam::185369506315:role/gerp-cloud-bff"
}

variable "closure_invoker_role_arn" {
  description = "The tower CodeBuild role that runs a closure. It invokes this gerp's export lambda to completion before destroying prod/per_customer, so the firm has its books before its instance goes. Deliberate literal default, the artifact_bucket convention."
  type        = string
  default     = "arn:aws:iam::185369506315:role/tower-per-customer-codebuild"
}
