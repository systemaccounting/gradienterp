# export — the firm's records, written out so they can be taken away.
#
# Closing a gerp deletes it, and the gerp is where the books live. `export_gerp` writes every
# module's tables plus the filing cabinet under `exports/<timestamp>/` in the SAME agent-owned
# uploads bucket storage uses — no new bucket, same shape as modules/storage: agent owns it, this
# operates on it.
#
# It reads every other module's tables, which is the one place that happens. It never learns what
# any of them MEAN — the policy lives as an explicit table in the lambda, and tests/export asserts
# every tagged table in the account appears there. See modules/export/AGENTS.md.


variable "log_retention_days" {
  description = "How long each function's log group keeps its events; root-set (prod/per_customer)."
  type        = number
  default     = 90
}

variable "ops_alerts_topic_arn" {
  description = "The operator's ops topic each function's Errors alarm publishes to; empty = no alarm (local)."
  type        = string
  default     = ""
}

variable "gerp_id" {
  description = "Logical tenant identifier; per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "storage_bucket" {
  description = "Name of the encrypted uploads bucket (owned by modules/agent, `uploads.tf`). Exports land under `exports/` beside the documents they copy."
  type        = string
}

variable "storage_kms_key_arn" {
  description = "ARN of the uploads bucket CMK (modules/agent). Every object written or copied is SSE-KMS under it."
  type        = string
}

# How long an export lives is `export_retention_days` on modules/agent — S3 allows one lifecycle
# configuration per bucket and modules/agent owns this one, so a second resource here would not add
# the exports/ rule, it would replace agent's and be replaced back on the next apply.

variable "export_invoker_role_arn" {
  description = "The gerp-cloud BFF's role, allowed to invoke this gerp's export lambda cross-account. The account screen is how an owner takes their data out — and after closure it is the ONLY way, since the agent, the gateway and the runtime are destroyed while this lambda is deliberately kept. Empty leaves the tool agent-only."
  type        = string
  default     = ""
}


variable "closure_invoker_role_arn" {
  description = "The tower CodeBuild role, allowed to invoke this gerp's export lambda cross-account. Closure runs the export before destroying the instance (.codebuild/per-customer.yml, TF_ACTION=destroy) — the order is the point, since the export reads the live tables the destroy is about to take away. Empty leaves closure unable to export, which is a closure that should not run."
  type        = string
  default     = ""
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  prefix             = "${var.stack_prefix}-export-${replace(var.gerp_id, "_", "-")}"
  storage_bucket_arn = "arn:aws:s3:::${var.storage_bucket}"
  # every table in this gerp — the exporter reads across modules by design, and naming them
  # individually here would be a second policy list to drift from the one in the lambda
  tables_arn = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-*-${replace(var.gerp_id, "_", "-")}*"
}

# ─── iam ───

resource "aws_iam_role" "lambda" {
  name = "${local.prefix}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.prefix}-lambda"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # READ every table in this gerp. The one module that does, and only ever Scan/Query —
        # an exporter that could write would be a strange thing to hand a wildcard.
        Sid      = "ReadEveryTableInThisGerp"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:Query", "dynamodb:DescribeTable"]
        Resource = local.tables_arn
      },
      {
        # read the documents, write the export beside them. The tagging pair is not optional:
        # CopyObject copies an object's tags by default, so without GetObjectTagging on the source
        # every document copy is AccessDenied — with no mention of tags in the error.
        # The annotation reads carry the filing cabinet's captions, which are the only description
        # a document has (modules/storage: the caption lives ON the object, path is the index).
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:ListBucket",
          "s3:GetObjectTagging", "s3:PutObjectTagging",
          "s3:GetObjectAnnotation", "s3:ListObjectAnnotations", "s3:PutObjectAnnotation",
        ]
        Resource = [local.storage_bucket_arn, "${local.storage_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.storage_kms_key_arn
      },
      {
        # the work list is the one table this module WRITES: plan, then stamp each unit done.
        # Scoped to its own table rather than widening the read wildcard above.
        Sid    = "WriteOwnJobTable"
        Effect = "Allow"
        Action = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem",
        "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [aws_dynamodb_table.jobs.arn]
      },
      {
        # issue the download credential. Only this role, and only under the fixed session name.
        Sid      = "MintReaderCredentials"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-reader"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── the reader role: what a customer's machine downloads with ───
#
# Read on this gerp's exports and nothing else. Assumed by the export lambda under the FIXED
# session name `gerp-export`, which is what makes a download auditable: the org trail filters data
# events on `userIdentity.arn`, and advanced event selectors offer no substring match, so a
# per-export session name would make the matcher inexpressible.
#
# The session is ONE HOUR, not twelve: a lambda runs as a role, so assuming this one is role
# CHAINING, which STS caps at 3600s whatever MaxSessionDuration says. Fine — `aws s3 sync` resumes
# and the agent issues another when asked.

resource "aws_iam_role" "reader" {
  name                 = "${local.prefix}-reader"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = aws_iam_role.lambda.arn }
      Condition = {
        # the session name is load-bearing, so it is enforced rather than hoped for
        StringEquals = { "sts:RoleSessionName" = "gerp-export" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "reader" {
  name = "${local.prefix}-reader"
  role = aws_iam_role.reader.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the exports, and only the exports. Not the filing cabinet it sits beside.
        Sid      = "ReadExportsOnly"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${local.storage_bucket_arn}/exports/*"
      },
      {
        # `aws s3 sync` lists before it copies, and a List without the prefix condition would
        # enumerate every document in the cabinet
        Sid      = "ListExportsOnly"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = local.storage_bucket_arn
        Condition = {
          StringLike = { "s3:prefix" = ["exports/*", "exports/"] }
        }
      },
      {
        # the objects are SSE-KMS; without this a download is AccessDenied on every GET
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.storage_kms_key_arn
      },
    ]
  })
}

# ─── the work list ───
#
# One row per unit of work, written before anything runs, stamped `done_at` as each finishes.
# Lambda caps at 900s and a large gerp exceeds it; this makes that a chunk size rather than a
# ceiling — a resume asks only "which rows have no timestamp".
#
# `done`, never `started`: no leases, no locks, no distinguishing a crashed unit from a slow one,
# and redoing one that was secretly nearly finished costs one unit.

resource "aws_dynamodb_table" "jobs" {
  name         = "${local.prefix}-jobs"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "export_id"
  range_key    = "unit"

  attribute {
    name = "export_id"
    type = "S"
  }
  attribute {
    name = "unit"
    type = "S"
  }

  # the plan outlives the export it planned by exactly as long as it is useful — the export itself
  # ages out at export_retention_days, and a job row for a prefix that no longer exists is litter
  ttl {
    attribute_name = "expires_at"
    enabled        = false
  }
}

# ─── lambda ───

module "export_gerp" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-export_gerp"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/export/lambdas/export_gerp.zip"
  src_dir         = "modules/export/lambdas/export_gerp"
  gerp_id         = var.gerp_id
  timeout         = 900
  memory          = 512
  env_vars = {
    CUSTOMER_ID            = var.gerp_id
    STACK_PREFIX           = var.stack_prefix
    STORAGE_BUCKET         = var.storage_bucket
    EXPORT_READER_ROLE_ARN = aws_iam_role.reader.arn
    EXPORT_JOBS_TABLE      = aws_dynamodb_table.jobs.name
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.export_gerp
  to   = module.export_gerp.aws_lambda_function.this
}



# ─── the owner app's way in ───
#
# A resource policy naming one role, the same shape the card-saving pair uses. It matters most at
# CLOSURE: the teardown keeps the bucket, its CMK, the reader role and this lambda, and destroys the
# agent that used to call it. Without this grant the survivors are inert — the download credential
# is one hour, the window is fifteen days, and nothing would be able to issue a fresh one.

resource "aws_lambda_permission" "export_invoker" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  count = var.export_invoker_role_arn == "" ? 0 : 1

  statement_id_prefix = "AllowOwnerAppExport"
  action              = "lambda:InvokeFunction"
  function_name       = module.export_gerp.name
  principal           = var.export_invoker_role_arn
}

resource "aws_lambda_permission" "closure_invoker" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  count = var.closure_invoker_role_arn == "" ? 0 : 1

  statement_id_prefix = "AllowClosureExport"
  action              = "lambda:InvokeFunction"
  function_name       = module.export_gerp.name
  principal           = var.closure_invoker_role_arn
}

output "lambda_functions" {
  value = { export_gerp = module.export_gerp.name }
}

output "lambda_arns" {
  value = { export_gerp = module.export_gerp.arn }
}
