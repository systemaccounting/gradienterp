variable "aws_region" {
  description = "Home region for the org. Sets where the management account talks to org-level services. us-east-1 because AgentCore Runtime is us-east-1 only at GA."
  type        = string
  default     = "us-east-1"
}

variable "operator_account_email" {
  description = "Root email for the operator sub-account (single services-OU account hosting tower, the shared bus, api.openlyoperated.biz, openlyoperated.biz, the public materialized store, etc.). Each AWS account requires a globally unique root email; aliases like ops+operator@yourdomain.tld work and are recoverable."
  type        = string
}

variable "gradienterp_account_email" {
  description = "Root email for the gradienterp customer sub-account — the operator's own openly-operated business books (gradienterp the company is itself a gradienterp user). Same uniqueness rule applies."
  type        = string
}

variable "operator_admin_username" {
  description = "Username of the operator's admin user in IAM Identity Center. Created manually via IC console before this terraform applies; this dir looks the user up by UserName and assigns OperatorAdmin across all accounts."
  type        = string
}

variable "audit_account_email" {
  description = "Root email for the Control Tower audit account (security/config aggregator). Created in the security OU directly under root, referenced by the landing zone manifest's securityRoles.accountId and config.accountId."
  type        = string
}

variable "log_archive_account_email" {
  description = "Root email for the Control Tower log archive account (centralized CloudTrail + Config logs). Created in the security OU directly under root, referenced by the landing zone manifest's centralizedLogging.accountId."
  type        = string
}
