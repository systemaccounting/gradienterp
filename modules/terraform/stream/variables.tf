variable "name" {
  description = "The mapping's name; the failure queue is `<name>-failed`."
  type        = string
}

variable "stream_arn" {
  type = string
}

variable "function_arn" {
  type = string
}

variable "role_name" {
  description = "The function's role, granted SendMessage on the failure queue."
  type        = string
}

variable "starting_position" {
  type    = string
  default = "LATEST"
}

variable "batch_size" {
  type    = number
  default = 100
}

variable "batching_window" {
  description = "Seconds to gather a batch before invoking."
  type        = number
  default     = 1
}

variable "retries" {
  description = "How many times one failing record is retried before it is parked on the queue."
  type        = number
  default     = 3
}

variable "filter_patterns" {
  description = "jsonencoded event patterns for the mapping's filter (any one matches); empty for every record."
  type        = list(string)
  default     = []
}

variable "ops_alerts_topic_arn" {
  description = "The topic the parked alarm publishes to; empty means no alarm."
  type        = string
  default     = ""
}
