variable "name" {
  description = "The function name, fully formed by the caller: <stack_prefix>-<module>-<gerp>-<fn>."
  type        = string
}

variable "role" {
  description = "The execution role arn. One per calling module, with its inline policies; the logs grant it needs is CreateLogStream/PutLogEvents on /aws/lambda/<name> (no CreateLogGroup — the group is owned here)."
  type        = string
}

variable "artifact_bucket" {
  description = "The operator's artifact bucket; the code is its latest version of `artifact_key`. Empty with `filename` set: an archive the applier built."
  type        = string
  default     = ""
}

variable "artifact_key" {
  description = "The zip's key in the artifact bucket, e.g. modules/inventory/lambdas/manage_stock.zip. Empty with `filename` set."
  type        = string
  default     = ""
}

variable "filename" {
  description = "An archive_file's output_path — the operator stacks' shape, where the applier builds the zip. Empty with `artifact_key` set."
  type        = string
  default     = ""
}

variable "source_code_hash" {
  description = "The archive_file's output_base64sha256, beside `filename`."
  type        = string
  default     = ""
}

variable "layers" {
  type    = list(string)
  default = []
}

variable "src_dir" {
  description = "The lambda's source dir, repo-relative — the `gerp:src-dir` tag deploy.sh walks."
  type        = string
}

variable "gerp_id" {
  description = "The gerp this function runs for; merged into the env as GERP_ID and CUSTOMER_ID. Empty for an operator function."
  type        = string
  default     = ""
}

variable "env_vars" {
  description = "The function's environment. Wins over the merged GERP_ID/CUSTOMER_ID on a key clash."
  type        = map(string)
  default     = {}
}

variable "handler" {
  type    = string
  default = "main.handler"
}

variable "runtime" {
  type    = string
  default = "python3.12"
}

variable "timeout" {
  type    = number
  default = 30
}

variable "memory" {
  type    = number
  default = 128
}

variable "description" {
  type    = string
  default = ""
}

variable "log_retention_days" {
  description = "How long the function's log group keeps its events. Root-set; 90 is what the groups declared before this module used."
  type        = number
  default     = 90
}



variable "dead_letter_arn" {
  description = "An SQS/SNS arn for the async failure record. Empty: none."
  type        = string
  default     = ""
}

variable "log_level" {
  description = "The lowest level the runtime keeps from the function's own lines (INFO, WARN, ERROR): the volume knob for a noisy function."
  type        = string
  default     = "INFO"
}

variable "tags" {
  description = "Tags beyond the fleet marker (a discovery tag another lambda queries for, such as agent_frame_sink)."
  type        = map(string)
  default     = {}
}
