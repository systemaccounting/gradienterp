variable "gerp_id" {
  description = "logical identifier for the gerp instance. per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root."
  type        = string
  default     = "gerp"
}

variable "agent_runtime_endpoint_arn" {
  description = "This gerp's AgentCore runtime endpoint ARN (module.agent output). poke_agent invokes it to wake the firm's own agent on an inbound event."
  type        = string
}

variable "register_with_agent" {
  description = "Register get_inbound as an MCP tool on the customer's agent gateway (so the agent can read its inbox)."
  type        = bool
  default     = true
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
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
  description = "The firm's own bus (modules/events). The hub's spoke edge puts the events addressed to this gerp here; the consume rule below takes them to receive_inbound."
  type        = string
  default     = ""
}
