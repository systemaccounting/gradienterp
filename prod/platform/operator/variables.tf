variable "aws_region" {
  description = "Home region for operator-account resources. us-east-1 because AgentCore Runtime is us-east-1 only at GA."
  type        = string
  default     = "us-east-1"
}

variable "chat_callback_urls" {
  description = "Per-gerp web-chat Function URLs to register as callbacks on the gerp-cloud client so each gerp's chat can complete the hosted-UI login back to itself. Each gerp's chat_url (with trailing slash). At fleet scale, source from the gerp-customers table instead."
  type        = list(string)
  default     = []
}
