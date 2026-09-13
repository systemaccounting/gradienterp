############################################
# The encrypted document store — consumed here, OWNED by `prod/init_customer`.
#
# Blobs for render_frame `file` fields and the general filing cabinet. modules/storage owns the
# `manage_storage` tool that files / finds / moves / captions objects here; the caption lives on each
# object as an S3 annotation. The browser PUTs bytes straight to the bucket via a presigned URL the
# chat lambda creates — never through the agent or the lambda body; only the object KEY flows back.
#
# The bucket and its CMK used to live in this module. They moved because they have to OUTLIVE it:
# closing a gerp destroys `prod/per_customer` — this module with it — while the customer's export
# stays downloadable for fifteen days. A module boundary is not a destroy boundary; a separate
# statefile is. See modules/export/TODO.md § closure.
#
# Passed in rather than looked up here so this module stays applyable on its own and the lookup
# happens once, in the root stack.
############################################

variable "uploads_bucket" {
  description = "Name of the encrypted document store, created by prod/init_customer. Deliberately an input: it outlives this module, so this module must not own it."
  type        = string
}

variable "uploads_bucket_arn" {
  description = "ARN of the same bucket."
  type        = string
}

variable "uploads_kms_key_arn" {
  description = "ARN of the bucket's CMK (prod/init_customer). Every object is SSE-KMS under it, so a role that reads or writes one needs a grant on this key too."
  type        = string
}
