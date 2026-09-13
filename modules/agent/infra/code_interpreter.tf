# ─────────────────────────────────────────────────────────────────────────────
# AgentCore Code Interpreter — the `analyze` tool's sandbox.
#
# The agent's primary surface for ANY quantitative question, financial or operational: it writes
# Python, this runs it, and the answer is computed rather than estimated. Managed sandbox (microVM),
# CloudTrail'd, billed per-second on actual CPU + peak memory — nothing idles.
#
# SANDBOX network mode: no public internet, but it CAN reach S3, which is the whole contract —
# read the firm's data, compute, write the artifact back. The execution role below is what "the
# firm's data" resolves to, and it is the security boundary: the sandbox holds these credentials,
# not the agent's.
#
# It reads the firm's own data, all of it. An earlier draft denied `uploads/` on the theory that it
# holds worker legal documents — wrong twice over. That prefix is where EVERY file-field upload lands,
# including the bank statement or lease the owner uploaded specifically to be analyzed; and the agent
# role already holds `GetObject` on the whole bucket anyway. Nothing about I-9 handling is enforced by
# IAM — `read_upload` presigns instead of fetching, so the restraint lives in the tool. A Deny here
# would have blocked real work while securing nothing.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # accounting's report bucket, by convention rather than by output: domain modules depend on the
  # agent (gateway registration), so reading an accounting output here closes a cycle. Mirrors how
  # standards_bucket is constructed rather than wired.
  report_bucket = "${var.stack_prefix}-accounting-${replace(var.gerp_id, "_", "-")}-report"
}

data "aws_iam_policy_document" "code_interpreter_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "code_interpreter" {
  name               = "${local.resource_prefix_dns}-code-interpreter"
  assume_role_policy = data.aws_iam_policy_document.code_interpreter_assume.json
}

data "aws_iam_policy_document" "code_interpreter" {
  # IN: the statements accounting writes, and everything in the cabinet — filed documents, uploaded
  # PDFs, prior analyses. If the firm has it, the firm's analysis can use it.
  statement {
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${local.report_bucket}",
      "arn:aws:s3:::${local.report_bucket}/*",
      var.uploads_bucket_arn,
      "${var.uploads_bucket_arn}/*",
    ]
  }

  # OUT: the artifact. Lands in the cabinet under analysis/, so manage_storage lists, reads and
  # presigns it like any other document the agent composed — no second retrieval surface.
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${var.uploads_bucket_arn}/analysis/*"]
  }

  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.uploads_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "code_interpreter" {
  name   = "${local.resource_prefix_dns}-code-interpreter"
  role   = aws_iam_role.code_interpreter.id
  policy = data.aws_iam_policy_document.code_interpreter.json
}

resource "aws_bedrockagentcore_code_interpreter" "analysis" {
  name               = "${local.resource_prefix_snake}_analysis"
  description        = "Financial and operational analysis sandbox for ${var.gerp_id}"
  execution_role_arn = aws_iam_role.code_interpreter.arn

  network_configuration {
    network_mode = "SANDBOX" # reaches S3, not the internet
  }
}

output "code_interpreter_id" {
  description = "The analysis sandbox the agent's `analyze` tool drives."
  value       = aws_bedrockagentcore_code_interpreter.analysis.code_interpreter_id
}
