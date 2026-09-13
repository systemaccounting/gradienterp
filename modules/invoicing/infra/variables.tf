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
  description = "arn of accounting's post_journal_entry lambda. issue_invoice / record_invoice_paid invoke it to post the invoice's journal entries (DR AR / CR revenue + tax; DR CASH / CR AR)."
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of accounting's post_journal_entry lambda. Passed as the POST_JOURNAL_ENTRY_FN env var."
  type        = string
}

variable "register_with_agent" {
  description = "Register invoicing lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "op_event_bus_arn" {
  description = "ARN of the shared gerp-events bus (operator account). accept_po PutEvents po.accepted here, addressed back to the buyer."
  type        = string
}

variable "internal_bus_name" {
  description = "The firm's own event bus (modules/events/infra). A rule attached to an invoice transition announces on it; the consuming module puts its rule on the same bus."
  type        = string
}

variable "internal_bus_arn" {
  description = "The same bus, for the events:PutEvents grant."
  type        = string
}

variable "schema_table_name" {
  description = "The per-customer registry table. Invoicing reads `invoice_tags` from it to refuse a tag the firm has not declared."
  type        = string
}

variable "items_table_name" {
  description = "Inventory's items table (modules/inventory/infra). create_from_template resolves each catalog key the template emits against it — name / unit / unit_price / revenue_account."
  type        = string
}

variable "rule_instances_table_name" {
  description = "Rule-instance DDB table (modules/rules/infra). Building an invoice queries it by the inventory key of every item it is selling — the instances attached there derive its taxes/fees. Nothing is taxed unless a rule is attached to it."
  type        = string
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "timezone" {
  description = "The gerp's IANA timezone, surfaced as GERP_TIMEZONE for `clock.py` — due dates are local calendar days. Default UTC = the behaviour before the clock module existed."
  type        = string
  default     = "UTC"
}

variable "agent_runtime_endpoint_arn" {
  description = "The gerp's AgentCore runtime endpoint. `on_incomplete_draft` pokes it when a POS pushes a ticket the protocol can't express yet — a line with no price or no revenue account. Empty ⇒ the stream consumer is not wired and an incomplete draft just sits."
  type        = string
  default     = ""
}

variable "server_api_id" {
  description = "The per-customer HTTP API (modules/server). The POS read path hangs off it — a till POSTs a ticket and polls for its state rather than blocking on the agent. Empty ⇒ no HTTP route."
  type        = string
  default     = ""
}

variable "server_api_execution_arn" {
  description = "Execution arn of that API, for the lambda invoke permission."
  type        = string
  default     = ""
}

variable "owner_authorizer_id" {
  description = "Owner JWT authorizer on the gerp API. Empty ⇒ the route falls back to AWS_IAM."
  type        = string
  default     = ""
}

variable "agreements_accept_fn_arn" {
  description = "arn of the shared agreements accept service. When set, the accept_po gateway target points at it (the schema stays invoicing's). Empty keeps the target on the module's own lambda."
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

variable "serve_web" {
  description = "Publish the invoice reads on the server api (server_api_id). A bool the root sets, so the plan knows the count before the api exists."
  type        = bool
  default     = false
}

variable "poke_agent" {
  description = "Wake the agent on an unpostable ticket (agent_runtime_endpoint_arn). A bool the root sets, so the plan knows the count before the runtime exists."
  type        = bool
  default     = false
}

variable "agreements_tools" {
  description = "Register the agreement tool (accept_po) on the gateway; its lambda is the agreements module's (agreements_accept_fn_arn). A bool the root sets, so the plan knows the tool set before that lambda exists."
  type        = bool
  default     = false
}

variable "agreements_decline_fn_arn" {
  description = "arn of the shared agreements decline service. The decline_po gateway target points at it."
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

variable "directory_table_arn" {
  description = "gerp-directory, the operator's table naming every gerp's hub and hub bus arn; an addressed event is put on the recipient's hub read from here (modules/events). The replica in this region."
  type        = string
}

variable "hub_id" {
  description = "This gerp's hub (its region); `from_hub` on addressed events."
  type        = string
  default     = ""
}
