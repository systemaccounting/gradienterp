############################################
# Agent session store — S3 backing for the Strands SessionManager.
#
# The container's StrandsEngine builds Agent(session_manager=S3SessionManager(...))
# against this bucket. It holds the agent's AUTHORITATIVE working session: full
# message history PLUS the interrupt checkpoint that render_form's pause/resume
# relies on (the message-only AgentCore Memory store can't hold a mid-turn pause).
#
# Co-located in the customer sub-account; the runtime reaches it under its own
# agent_execution role (same-account → identity policy in main.tf, no bucket
# policy). AgentCore Memory stays, demoted to the UI-transcript log the web chat
# replays from (see entrypoint.py + lambdas/chat/index.mjs loadHistory).
############################################

locals {
  sessions_bucket = "${var.stack_prefix}-agent-${replace(var.gerp_id, "_", "-")}-sessions-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "sessions" {
  bucket = local.sessions_bucket
  tags   = { gerp_id = var.gerp_id, module = "agent", "gerp:layer" = "session" }

  # Closing a gerp destroys this whole stack, and a bucket with a single object left in it fails
  # the destroy at APPLY — plan does not check. Session transcripts are working state, not the
  # customer's records: the export takes their books and documents from the store in
  # prod/init_customer, which is a different bucket and is deliberately not destroyed with this.
  force_destroy = true
}

# Sessions can carry conversation history and resumed form values (a render_form
# checkpoint round-trips the user's submitted fields through the message log).
# Treat it as private — block all public access.
resource "aws_s3_bucket_public_access_block" "sessions" {
  bucket                  = aws_s3_bucket.sessions.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Match the AgentCore Memory transcript horizon. Per-object age expiry; a single
# chat running continuously past the horizon is unrealistic, and the transcript
# is the durable replay record regardless — this prunes abandoned sessions.
resource "aws_s3_bucket_lifecycle_configuration" "sessions" {
  bucket = aws_s3_bucket.sessions.id
  rule {
    id     = "expire-sessions"
    status = "Enabled"
    filter {}
    expiration { days = var.memory_retention_days }
  }
}
