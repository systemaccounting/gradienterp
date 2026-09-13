variable "hub_id" {
  description = "The hub's name in the platform's HUBS map; the region it serves (us-east-1)."
  type        = string
}

variable "aws_account_id" {
  description = "The hub's own account, vended into the hubs OU."
  type        = string
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}
