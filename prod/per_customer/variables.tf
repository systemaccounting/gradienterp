variable "aws_region" {
  description = "Customer sub-account region. us-east-1 to match the operator bus + AgentCore."
  type        = string
  default     = "us-east-1"
}

variable "gerp_id" {
  description = "Logical identifier for the tenant. Used as resource-name suffix and SSM lookup key (/gradienterp/customers/<gerp_id>)."
  type        = string
}

variable "aws_account_id" {
  description = "AWS sub-account ID for this customer. Provider assumes OperatorOrchestration role into this account (deployed by the customers-OU stackset; trusts the operator account)."
  type        = string
}

variable "sender_email" {
  description = "Operator-wide SES verified sender email. Not per-tenant — one SES identity serves all customers' outbound mail."
  type        = string
}

variable "chat_base_url" {
  description = "Operator-wide base URL for agent chat interview links. Not per-tenant."
  type        = string
}

variable "square_api_base" {
  description = "Square API base for this customer's payments lambdas. Default production; pass https://connect.squareupsandbox.com to test against Square Sandbox (the access token must match the environment)."
  type        = string
  default     = "https://connect.squareup.com"
}

variable "paypal_api_base" {
  description = "PayPal REST API base for this customer's payments lambdas. Default production; pass https://api-m.sandbox.paypal.com to test against PayPal Sandbox (the client_id/secret must match the environment)."
  type        = string
  default     = "https://api-m.paypal.com"
}


variable "timezone" {
  description = <<-EOT
    The gerp's IANA timezone (e.g. America/Los_Angeles). The business's clock: what "today",
    "this month" and a 7am shift mean for this customer. Canonical IANA names only — an alias
    may not resolve on every runtime.

    Provision-time config rather than an owner setting, for now. It is NOT yet used to convert
    stored timestamps (the platform stores instants and bounds periods in UTC — modules/clock); it is
    used so the agent knows the business's local date and can DISCLOSE which zone any time it
    quotes is in. Disclosure is the cheap correctness measure; conversion is the expensive one.
  EOT
  type        = string
  default     = "UTC"
}

variable "export_invoker_role_arn" {
  description = "The gerp-cloud BFF's role, allowed to invoke this gerp's export lambda cross-account so the owner can take their data out from the account screen. Unlike billing_invoker_role_arn this is set on EVERY gerp — every owner can leave — and it is what keeps the download working after closure, when the agent that used to create the links has been destroyed. Deliberate literal default, the artifact_bucket convention."
  type        = string
  default     = "arn:aws:iam::185369506315:role/gerp-cloud-bff"
}

