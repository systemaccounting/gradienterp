variable "gerp_id" {
  description = "logical identifier for the tenant this labor stack belongs to. used as the per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "post_journal_entry_fn_arn" {
  description = "arn of the accounting module's post_journal_entry lambda. labor's close-handler invokes it to post the wages accrual (DR WAGES_EXPENSE / CR WAGES_PAYABLE)."
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of the accounting module's post_journal_entry lambda. passed to the close-handler as the POST_JOURNAL_ENTRY_FN env var."
  type        = string
}

variable "schema_table_name" {
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). manage_labor's put/update ops query it at cold start for field-name validation against the labor_fields registry."
  type        = string
}

variable "ledger_table_name" {
  description = "Name of the accounting module's ledger DDB table (pk = YYYY-MM month bucket). The pay_run trigger queries the period partition for the worker's WAGES_PAYABLE credits to total the gross to withhold on."
  type        = string
}

variable "rule_instances_table_name" {
  description = "Name of the modules/rules rule-instances DDB table. The pay run queries PAY_RUN#<contact_id> for the withholdings + employer taxes attached to a worker; the close-handler queries CLOSE_SHIFT#<contact_id> for the wage accrual. What a worker owes IS what is attached — there is no rule set anywhere else."
  type        = string
}

variable "rules_params_table_name" {
  description = "Name of the modules/rules rules-params DDB table. The pay run reads the GENERAL rows — the platform bracket TABLES (Pub 15-T, CA Method B) in force for the period, seeded from canonical S3 on a weekly schedule. The instance carries the worker's W-4; the world carries the brackets, so a new tax year is a canonical push, not a deploy."
  type        = string
}

variable "register_with_agent" {
  description = "Register labor lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "timezone" {
  description = "The gerp's IANA timezone. Reaches the lambdas as GERP_TIMEZONE, which `clock.py` uses to place naive wall-clock times. Default UTC = the behaviour before the module existed."
  type        = string
  default     = "UTC"
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

variable "internal_bus_name" {
  description = "The firm's own event bus (modules/events/infra). A record_metric row on this module's callsites announces a product event here."
  type        = string
}

variable "internal_bus_arn" {
  description = "The same bus, for the events:PutEvents grant."
  type        = string
}

variable "op_event_bus_arn" {
  description = "The hub's bus: `events.publish` puts the platform copy of a metrics event there when the firm is openly operated."
  type        = string
}
