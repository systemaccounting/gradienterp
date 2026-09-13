############################################
# module contract — tower reads these from the deployed agent
############################################

output "agent_arn" {
  description = "AgentCore Runtime ARN. Tower uses for lifecycle actions (suspend, offboard)."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

# The encrypted uploads bucket + CMK (uploads.tf) — the render_frame `file`-field substrate. modules/storage
# consumes these to run its manage_storage tool over the same store (agent owns the bucket; storage manages it).
output "uploads_bucket" {
  description = "Name of the encrypted uploads bucket (render_frame `file` fields; consumed by modules/storage's manage_storage)."
  value       = var.uploads_bucket
}

output "uploads_kms_key_arn" {
  description = "ARN of the uploads bucket CMK — modules/storage needs kms on it (annotations inherit the object's SSE-KMS)."
  value       = var.uploads_kms_key_arn
}

output "agent_id" {
  description = "Human-readable identifier for logs + billing."
  value       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
}

output "runtime_endpoint_arn" {
  description = "AgentCore Runtime endpoint ARN — owner channels target this to invoke the agent."
  value       = aws_bedrockagentcore_agent_runtime_endpoint.this.agent_runtime_endpoint_arn
}

output "gateway_id" {
  description = "MCP gateway ID. Domain modules read this from SSM at /gradienterp/customers/<id>/agent/gateway_id to register their tool targets."
  value       = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_arn" {
  description = "MCP gateway ARN. Same SSM convention as gateway_id."
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "gateway_url" {
  description = "MCP gateway URL. Tower / openlyoperated.biz use to push signals into the agent."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "gateway_role_arn" {
  description = "IAM role identity Gateway uses when invoking tool lambdas. Domain modules use this as the principal in their per-lambda aws_lambda_permission."
  value       = aws_iam_role.agent_execution.arn
}

output "log_group_arn" {
  description = "CloudWatch log group ARN. Tower's metering lambda reads invocation counts from here."
  value       = aws_cloudwatch_log_group.agent.arn
}

output "execution_role_arn" {
  description = "IAM role the agent assumes when invoking lambdas / Bedrock."
  value       = aws_iam_role.agent_execution.arn
}

output "chat_url" {
  description = "The web chat Function URL — the gerp's shareable human front door. Empty when cognito isn't wired (chat disabled). Write onto the gerp-customers row at provision so the dashboard can deep-link to it."
  value       = local.chat_enabled == 1 ? aws_lambda_function_url.chat[0].function_url : ""
}

output "channel_endpoints" {
  description = "Per-channel inbound endpoints. Phase 6 populates sms/email/web; web is the chat Function URL once cognito is wired."
  value = {
    sms   = ""
    email = ""
    web   = local.chat_enabled == 1 ? aws_lambda_function_url.chat[0].function_url : ""
  }
}
