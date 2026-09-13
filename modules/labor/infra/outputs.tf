output "worker_table" {
  description = "DDB worker table name. hash contact_id, range role; the rate book (rate, classification per role)."
  value       = aws_dynamodb_table.worker.name
}

output "time_entries_table" {
  description = "DDB time-entries table name. hash worker_id, range entry_id; durable open clock-ins."
  value       = aws_dynamodb_table.time_entries.name
}

output "worker_legal_table" {
  description = "DDB worker-legal table name. hash worker_id, range sk (role#type); typed JSON EAV."
  value       = aws_dynamodb_table.worker_legal.name
}

output "time_entries_stream_arn" {
  description = "time-entries stream ARN. the close-handler maps onto this to post the wages accrual on clock-out."
  value       = aws_dynamodb_table.time_entries.stream_arn
}

output "lambda_role_arn" {
  description = "execution role for the labor DDB tools. the close-handler reuses table + post_journal_entry access from the same module scope."
  value       = aws_iam_role.lambda.arn
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "lambda_arns" {
  description = "map of lambda logical name → ARN. agent/infra consumes this as `labor_lambda_arns`."
  value       = { for k, fn in module.fn : k => fn.arn }
}
