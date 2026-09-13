############################################
# module contract — tower passes these in per customer via prod/customers.tf
#
# Tenant metadata (business_name, business_category, owner_email, owner_phone,
# reporting_schedule, policies) is NOT a variable. It lives in SSM Parameter
# Store at /gradienterp/customers/<gerp_id> as a JSON blob and this
# module reads it via a data source. See main.tf `local.customer`.
#
# Expected SSM JSON shape:
#   {
#     "business_name":      "Maria's Cafe",
#     "business_category":  "quick_serve_restaurant",
#     "owner_email":        "owner@example.com",
#     "owner_phone":        "+15555550100",
#     "reporting_schedule": "cron(0 9 1 * ? *)",
#     "openly_operated":    true,
#     "policies": {
#       "spend_threshold":           500,
#       "classification_confidence": 0.8
#     }
#   }
#
# `openly_operated` gates publication of per-business events to the public
# stream. Onboarding seeds it default-true for businesses (transparency-driven
# capital flow is the whole pitch — see README.md "why open by default for
# businesses") and default-false for private individuals running the ERP for
# personal use. When true, events publish with full per-customer attribution.
# When false, events stay inside the customer's sub-account; a tower-side
# anonymizer still contributes aggregate rollups (e.g. "N cafes, median margin
# X%") to the public feed, but per-customer drill-downs are 404.
# This module reads nothing directly from the flag — the tower publisher lambda
# (prod/tower/lambdas/, phase-7) checks `local.customer.openly_operated` before
# emitting. Schema documents it here so onboarding seeds it consistently.
############################################

variable "gerp_id" {
  description = "Logical identifier for this customer (SSM lookup key + resource-name suffix)."
  type        = string
}

############################################
# runtime / deployment — operator-set knobs, not tenant metadata
############################################

variable "aws_region" {
  description = "AWS region for the customer's sub-account."
  type        = string
}

variable "operator_account_id" {
  description = "AWS account ID hosting the operator-side ECR for the agent container image. Customer's AgentCore Runtime pulls from <operator_account_id>.dkr.ecr.<aws_region>.amazonaws.com/agentcore@<digest> (newest image, data.aws_ecr_image most_recent) via an org-scoped ECR resource policy. Default targets the live operator account."
  type        = string
  default     = "185369506315"
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable. Used to reconstruct the schemas DDB table name (the schemas module applies after agent, so its output isn't referenceable here)."
  type        = string
  default     = "gerp"
}

variable "model_id" {
  description = "Bedrock inference-profile ID the agent invokes. Default is Claude Sonnet 4.6 (us. cross-region profile) — fine for customer bookkeeping/inventory/sms agents. Override to `us.anthropic.claude-opus-4-7` for the operator agent (phase 8) where deprovisioning / bulk-action judgment justifies the cost. Verify available profiles with `aws bedrock list-inference-profiles --query 'inferenceProfileSummaries[?contains(inferenceProfileName, \"laude\")]'`."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "webhook_base_url" {
  description = "This customer's HTTP API gateway base URL (from modules/server/infra api_endpoint). Passed to the runtime as WEBHOOK_BASE_URL; read_instruction fills the {{ webhook_base_url }} placeholder in setup playbooks with it so the agent recites the right /webhooks/<provider> URL. Empty until server is wired through the composition root."
  type        = string
  default     = ""
}

variable "playbook_kb_id" {
  type        = string
  description = "Bedrock Knowledge Base id the agent retrieves playbooks from"
}

variable "standards_bucket" {
  type        = string
  default     = ""
  description = "Operator's shared standards-corpus bucket (org-readable; contributions land under _contrib/<this account id>/). Empty ⇒ the standards tools aren't registered."
}




############################################
# web chat — the gerp's human front door (modules/agent/lambdas/chat). Empty
# cognito_user_pool_id ⇒ no chat front door is provisioned (count-gated).
############################################

variable "cognito_user_pool_id" {
  description = "Operator Cognito pool ID. The chat lambda validates owner/employee JWTs against this pool's JWKS (identity). Empty disables the chat front door."
  type        = string
  default     = ""
}

variable "cognito_client_id" {
  description = "Operator Cognito app-client ID — the audience the chat lambda checks on id tokens (and client_id on access tokens)."
  type        = string
  default     = ""
}

variable "contacts_table_name" {
  description = "This gerp's contacts DDB table name. The chat lambda queries its account-index (hash account_id) to resolve a caller's role from the relationship flags. Passed as a constructed string by the composition root (NOT module.contacts's output — agent applies before contacts, which depends_on agent)."
  type        = string
  default     = ""
}

variable "agent_email_parent_domain" {
  description = "Operator-owned product domain under which this gerp's agent gets a subdomain mailbox (<gerp_id>.<parent>), e.g. \"agents.gradienterp.cloud\". The module owns an in-account Route53 zone for the subdomain + SES receive/send + the handler; the operator delegates the subdomain to the zone's nameservers (see the agent_email_delegation output). Empty disables the email front door (count-gated, like the chat one). owner_email must also be set in the customer SSM blob (it's the verified reply recipient = the allowlist)."
  type        = string
  default     = ""
}

variable "memory_retention_days" {
  description = "How long AgentCore Memory keeps per-session context before expiry."
  type        = number
  default     = 90
}


variable "log_retention_days" {
  description = "CloudWatch log group retention."
  type        = number
  default     = 30
}

variable "hub_runtime_endpoint_arn" {
  description = "The operator hub agent's runtime endpoint ARN (prod/optimizer output). Set ⇒ this spoke gets the in-process ask_hub tool + identity permission to InvokeAgentRuntime on the hub, so its owner can ask the hub to find a provider / coordinate across firms. Empty ⇒ no ask_hub tool. Cross-firm coordination is mediated hub-and-spoke; a spoke never invokes another spoke directly."
  type        = string
  default     = ""
}

variable "hub_role_arn" {
  description = "The operator hub agent's execution role ARN (prod/optimizer output). Set ⇒ this spoke attaches a resource policy on its own runtime + endpoint granting ONLY this role InvokeAgentRuntime, so the hub can ask_spoke this gerp. Empty ⇒ no inbound grant (the hub can't reach this spoke). The reciprocal of hub_runtime_endpoint_arn; both are wired together in per_customer."
  type        = string
  default     = ""
}


variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}


variable "timezone" {
  description = "The gerp's IANA timezone (e.g. America/Los_Angeles). Reaches the container as GERP_TIMEZONE: the agent renders \"today\" in it and names it whenever it states a time. Not a conversion layer: storage and period boundaries stay UTC, and the zone decides only where a civil boundary falls (modules/clock)."
  type        = string
  default     = "UTC"
}

variable "ops_alerts_topic_arn" {
  description = "The operator's ops topic each function's Errors alarm publishes to; empty = no alarm (local)."
  type        = string
  default     = ""
}

variable "ops_read_role" {
  description = "The operator gerp only: the name of the read-only role every gerp account holds (gerp-ops-read, prod/init_customer). Set ⇒ this agent gets the in-process read_fleet_logs tool and sts:AssumeRole on that role in any account of the organization, so its poked turn on an alarm task can read the gerp's logs. Empty ⇒ no tool."
  type        = string
  default     = ""
}

variable "org_ids" {
  description = "The organizations an investigator's read may reach into (with ops_read_role): this one and the ones config.json admits."
  type        = list(string)
  default     = []
}
