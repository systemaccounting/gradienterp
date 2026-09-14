variable "aws_region" {
  description = "Home region for operator-account resources. us-east-1 because AgentCore Runtime is us-east-1 only at GA."
  type        = string
  default     = "us-east-1"
}

