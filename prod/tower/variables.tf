variable "aws_region" {
  description = "Operator account region. us-east-1 to match the bus + AgentCore."
  type        = string
  default     = "us-east-1"
}

variable "tfstate_bucket" {
  description = "Name of the operator's tfstate bucket. CodeBuild needs r/w on this for backend state."
  type        = string
  default     = "gradienterp-tfstate-185369506315"
}

variable "tfstate_lock_table" {
  description = "Name of the DDB lock table for terraform backend."
  type        = string
  default     = "gradienterp-tfstate-lock"
}

variable "seller_gerp" {
  description = "The gerp that sells hosting — gradienterp's own instance. bill_customer books the AWS cost and issues the fee here."
  type        = string
  default     = "gradienterp"
}

variable "seller_storage_bucket" {
  description = "Bucket in the seller's account where AWS's own invoices are kept beside the entries that book them."
  type        = string
  default     = "gerp-agent-gradienterp-uploads-867637277314"
}

variable "bedrock_model_ids" {
  description = "Base model ids every vended account gets a Marketplace agreement for. Has to name the model under whatever inference profile per_customer applies (modules/agent/infra model_id: `us.anthropic.claude-sonnet-4-6` is a profile over `anthropic.claude-sonnet-4-6`)."
  type        = list(string)
  default     = ["anthropic.claude-sonnet-4-6"]
}

variable "bedrock_use_case_form" {
  description = "The Anthropic use-case form put in every vended account — the operator's own statement, since the operator is the party with the AWS relationship. Read the one on file with `aws bedrock get-use-case-for-model-access`."
  type        = map(string)
  default = {
    companyName         = "gradient erp"
    companyWebsite      = "https://gradienterp.cloud"
    intendedUsers       = "1"
    industryOption      = "Software as a Service"
    otherIndustryOption = ""
    useCases            = "enterprise resource planning"
  }
}

