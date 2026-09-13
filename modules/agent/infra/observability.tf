############################################
# observability — CloudWatch log group for agent runtime output.
# OpenTelemetry export happens via env vars on the runtime container
# (see main.tf → aws_bedrockagentcore_agent_runtime.environment_variables).
#
# Log group name matches the AgentCore convention so Runtime writes to it
# automatically. Tower's metering lambda reads invocation counts from here.
############################################

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/bedrockagentcore/runtime/${var.gerp_id}"
  retention_in_days = var.log_retention_days

  tags = {
    gerp_id = var.gerp_id
    module  = "agent"
  }
}
