variable "gerp_id" {
  description = "logical identifier for the tenant. per-tenant resource-name suffix + SSM lookup key."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "post_journal_entry_fn_arn" {
  description = "arn of accounting's post_journal_entry lambda. record_receipt / record_payment invoke it to post the PO's journal entries (DR <lines> / CR AP; DR AP / CR CASH)."
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of accounting's post_journal_entry lambda. Passed as the POST_JOURNAL_ENTRY_FN env var."
  type        = string
}

variable "register_with_agent" {
  description = "Register purchasing lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "op_event_bus_arn" {
  description = "ARN of the shared gerp-events bus (operator account). request_quote PutEvents quote.requested here, addressed to the vendor's gerp."
  type        = string
}

variable "inbound_stream_arn" {
  description = "ARN of modules/inbox's -inbound table stream. apply_po_event subscribes (filtered to po.proposed/po.accepted) to stamp this firm's agreement row from inbound addressed events."
  type        = string
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "shipments_table_name" {
  description = "modules/shipping's per-customer table. get_pos READS it (never writes) so a PO answers with its delivery — the owner asks about one order, not two subledgers."
  type        = string
}

variable "update_stock_fn_name" { # inventory's manage_stock — the receipt cascade posts op=move
  description = "modules/inventory's update_stock. The receipt cascade (on_po_received) invokes it to move the shelf count — inventory owns that write; purchasing only tells it goods landed."
  type        = string
}

variable "agreements_request_fn_arn" {
  description = "arn of the shared agreements request service. When set, the create_po gateway target points at it (the schema stays purchasing's; the kind rides on the tool name). Empty keeps the target on the module's own lambda."
  type        = string
  default     = ""
}

variable "shared_agreements_table_name" {
  description = "The shared gerp-agreements table (modules/agreements) — the ONE store for an agreement of any kind. Required: purchasing had a second table of its own behind a fallback here, which meant one fact with two storage paths and a reader that had to know which."
  type        = string
}

variable "shared_agreements_table_arn" {
  description = "arn for the shared table's IAM grant on the tools role."
  type        = string
}

variable "gateway_id" {
  description = "The agent's gateway this module registers its tools on — module.agent.gateway_id. Read at plan as an input, never from SSM: a fresh account has no parameter to read yet."
  type        = string
  default     = ""
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes tools as — module.agent.gateway_role_arn; the principal on each tool lambda's invoke permission."
  type        = string
  default     = ""
}

variable "agreements_tools" {
  description = "Register the target-only agreement tool (return_quote) on the gateway; its lambda is the agreements module's request service. A bool the root sets, so the plan knows the tool set before that lambda exists."
  type        = bool
  default     = false
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
