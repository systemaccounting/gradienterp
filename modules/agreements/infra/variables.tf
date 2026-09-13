variable "gerp_id" {
  description = "logical identifier for the tenant this agreements store belongs to. used as the per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "op_event_bus_arn" {
  description = "arn of the shared gerp-events EventBridge bus (operator account). request/accept PutEvents the addressed <kind>.proposed / <kind>.accepted to it — fire-and-forget."
  type        = string
}

variable "post_journal_entry_fn_arn" {
  description = "arn of the accounting module's post_journal_entry lambda. The settle money step (purchase.pay) posts through it."
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of the accounting module's post_journal_entry lambda. Passed as the POST_JOURNAL_ENTRY_FN env var."
  type        = string
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "register_with_agent" {
  description = "Grant the AgentCore gateway role invoke on the two service lambdas. The kinds' own modules register the TARGETS (their domain-shaped schemas point at these arns); this is only the lambda-side permission, granted once here instead of once per target. Reads the gateway role from SSM (written by modules/agent), so the consumer must depends_on = [module.agent]."
  type        = bool
  default     = false
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes tools as — module.agent.gateway_role_arn; the principal on each tool lambda's invoke permission."
  type        = string
  default     = ""
}

variable "rule_instances_table_name" {
  description = "The rules module's instance table: apply_inbound runs the firm's PROPOSAL#<kind> rows over an inbound proposal and acts on what they permit."
  type        = string
}

variable "items_table_name" {
  description = "The inventory items table: a rule answering a PO from the shelf reads each sku's on-hand cache from it."
  type        = string
}

variable "log_retention_days" {
  description = "How long each function's log group keeps its events; root-set (prod/per_customer)."
  type        = number
  default     = 90
}

variable "ops_alerts_topic_arn" {
  description = "The operator's ops topic each function's Errors alarm publishes to; empty = no alarm (local)."
  type        = string
  default     = ""
}

variable "directory_table_arn" {
  description = "gerp-directory, the operator's table naming every gerp's hub and hub bus arn; an addressed event is put on the recipient's hub read from here (modules/events). The replica in this region."
  type        = string
}

variable "hub_id" {
  description = "This gerp's hub (its region); `from_hub` on addressed events."
  type        = string
  default     = ""
}
