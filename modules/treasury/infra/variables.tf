variable "gerp_id" {
  description = "logical identifier for the tenant this treasury stack belongs to. used as the per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "ledger_table_name" {
  description = "Name of the accounting module's ledger DDB table (pk = YYYY-MM month bucket). The distribution handler folds each instrument's prior DIVIDENDS_PAYABLE credits from it — the cap projection."
  type        = string
}

variable "rule_instances_table_name" {
  description = "Name of the modules/rules rule-instances DDB table. An instrument IS a row in it (pk `DISTRIBUTION#<id>`): `settlement` attaches one when an offer's funds land, and `distribution` runs what's attached (and scans the `DISTRIBUTION#` subjects to enumerate the instruments)."
  type        = string
}

variable "post_journal_entry_fn_arn" {
  description = "arn of the accounting module's post_journal_entry lambda. The distribution handler invokes it to post DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE."
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of the accounting module's post_journal_entry lambda. Passed as the POST_JOURNAL_ENTRY_FN env var."
  type        = string
}

variable "op_event_bus_arn" {
  description = "arn of the shared gerp-events EventBridge bus (operator account). The distribution handler PutEvents distribution.paid to it after the durable post — fire-and-forget."
  type        = string
}

variable "settings_table_name" {
  description = "Settings config table name (modules/settings/infra). The distribution handler reads GERP#openly_operated from it at cold start to gate publication."
  type        = string
}

variable "settings_table_arn" {
  description = "Settings config table ARN — the dynamodb:GetItem grant for the openly_operated cold-start read."
  type        = string
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "register_with_agent" {
  description = "Register the capital marketplace tools (propose_offer / accept_offer / record_capital_receipt / get_offers) as AgentCore gateway targets. Reads the gateway id/role from SSM (written by modules/agent), so the consumer must depends_on = [module.agent]. False keeps a standalone treasury apply clean (distribution + settlement only, no gateway)."
  type        = bool
  default     = false
}

variable "agreements_request_fn_arn" {
  description = "arn of the shared agreements request service. When set, the propose_offer gateway target points at it. Empty keeps the target on the module's own lambda."
  type        = string
  default     = ""
}

variable "agreements_accept_fn_arn" {
  description = "arn of the shared agreements accept service. When set, the accept_offer gateway target points at it. Empty keeps the target on the module's own lambda."
  type        = string
  default     = ""
}

variable "shared_agreements_table_name" {
  description = "The shared gerp-agreements table (modules/agreements). When set, the treasury tools read and stamp capital deals THERE (get_offers / get_holdings / record_capital_receipt / record_capital_outlay) — new deals live on the shared store once the targets repoint. Empty keeps them on the module's own table."
  type        = string
  default     = ""
}

variable "shared_agreements_table_arn" {
  description = "arn for the shared table's IAM grant on the tools role. Set together with shared_agreements_table_name."
  type        = string
  default     = ""
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
  description = "Register the agreement tools (propose_offer, accept_offer) on the gateway; their lambdas are the agreements module's. A bool the root sets, so the plan knows the tool set before those lambdas exist."
  type        = bool
  default     = false
}

variable "agreements_decline_fn_arn" {
  description = "arn of the shared agreements decline service. The decline_offer gateway target points at it."
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
