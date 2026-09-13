output "ledger_table" {
  description = "DDB journal/ledger table name."
  value       = module.accounting.ledger_table
}

output "balances_table" {
  value = module.accounting.balances_table
}

output "report_bucket" {
  value = module.accounting.report_bucket
}

output "accounting_lambda_arns" {
  description = "Map of accounting lambda logical name → ARN. Tower stores these in the customers DDB row for downstream operator-side observability."
  value       = module.accounting.lambda_arns
}

output "accounting_lambda_functions" {
  value = module.accounting.lambda_functions
}

output "contacts_table" {
  description = "DDB contacts table name."
  value       = module.contacts.contacts_table
}

output "contacts_stream_arn" {
  description = "DDB Stream ARN for the contacts table."
  value       = module.contacts.contacts_stream_arn
}

output "contacts_lambda_arns" {
  description = "Map of contacts lambda logical name → ARN."
  value       = module.contacts.lambda_arns
}

output "contacts_lambda_functions" {
  value = module.contacts.lambda_functions
}

output "notes_table" {
  description = "DDB notes table name."
  value       = module.notes.notes_table
}

output "notes_stream_arn" {
  value = module.notes.notes_stream_arn
}

output "notes_lambda_arns" {
  value = module.notes.lambda_arns
}

output "notes_lambda_functions" {
  value = module.notes.lambda_functions
}

output "inventory_items_table" {
  description = "DDB items table name."
  value       = module.inventory.items_table
}

output "inventory_lambda_arns" {
  value = module.inventory.lambda_arns
}

output "inventory_lambda_functions" {
  value = module.inventory.lambda_functions
}

output "tasks_table" {
  description = "DDB tasks table name."
  value       = module.tasks.tasks_table
}

output "tasks_stream_arn" {
  value = module.tasks.tasks_stream_arn
}

output "tasks_lambda_arns" {
  value = module.tasks.lambda_arns
}

output "tasks_lambda_functions" {
  value = module.tasks.lambda_functions
}

output "calendar_schedule_group" {
  description = "EBS Scheduler group for this customer's schedules."
  value       = module.calendar.schedule_group_name
}

output "calendar_lambda_arns" {
  value = module.calendar.lambda_arns
}

output "calendar_lambda_functions" {
  value = module.calendar.lambda_functions
}

output "gateway_url" {
  description = "The gerp's HTTP API endpoint (module.server). The buildspec stashes it onto the gerp-customers row: the dashboard reads `ready` off its presence, and the BFF routes to the gerp through it."
  value       = module.server.api_endpoint
}

output "chat_url" {
  description = "The gerp's web-chat Function URL (module.agent). Empty until cognito is wired. The buildspec stashes this onto the gerp-customers row so the dashboard can deep-link."
  value       = module.agent.chat_url
}

output "runtime_endpoint_arn" {
  description = "The gerp agent's runtime endpoint ARN. The buildspec stashes it onto the gerp-customers row so the operator hub's ask_spoke can resolve + invoke this spoke (a2a metadata riding the instance registry)."
  value       = module.agent.runtime_endpoint_arn
}

output "portal_url" {
  description = "The owner portal base URL (portal lambda Function URL + capability slug) — the link the agent hands the owner; manage_storage put under pages/ returns per-page urls built on it."
  value       = module.storage.portal_url
}

output "playbook_kb_id" {
  description = "The gerp's Bedrock knowledge base the agent's search_guides reads; the build syncs modules/**/kb.md into it."
  value       = module.playbooks.knowledge_base_id
}

output "playbook_data_source_id" {
  description = "The KB's CUSTOM data source the guides are ingested into."
  value       = module.playbooks.data_source_id
}
