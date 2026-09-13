# ui — the gerp's web server (../AGENTS.md § ui; the public surface: modules/site).
#
# One Function-URL lambda (the UI lambda) serving agent-published pages from the cabinet bucket's `pages/`
# prefix, live `/data/*` reads, the version marker, and form intake into `submissions/`.
# The tasks-table stream also triggers it (a second entry point on the same function) to bump
# the marker so open pages refresh. The slug is the per-gerp capability: a random_id checked
# in code — wrong slug is the same 404 as a missing page.

terraform {
  required_providers {
    random = {
      source = "hashicorp/random"
    }
  }
}

variable "email_bucket" {
  description = "The gerp's inbound-mail bucket (modules/agent, email.tf). Empty when the email front door is off, in which case the portal simply has no inbox to browse. Read-only from here; raw .eml under in/."
  type        = string
  default     = ""
}

variable "tasks_table" {
  description = "DDB tasks table name — the portal's /data/tasks read (open-tasks-index)."
  type        = string
}

variable "tasks_stream_arn" {
  description = "Tasks table stream arn — task writes bump the portal version marker."
  type        = string
}

resource "random_id" "portal_slug" {
  byte_length = 16
  keepers     = { gerp = var.gerp_id } # stable per gerp; never rotates on apply
}

locals {
  tasks_table_arn = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.tasks_table}"
  portal_url      = "${trimsuffix(aws_lambda_function_url.ui.function_url, "/")}/${random_id.portal_slug.hex}"
}

resource "aws_iam_role" "ui" {
  name = "${local.prefix}-ui-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "ui" {
  name = "${local.prefix}-ui-lambda"
  role = aws_iam_role.ui.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        # The whole cabinet, not just pages/. It is the firm's own bucket and the portal is how
        # they look at it — documents, outputs, submissions, whatever the agent filed. ListBucket
        # is what makes a browsable page possible at all; without it a page can only show what the
        # agent baked into the HTML at write time.
        Effect = "Allow"
        # DeleteObjectVersion + ListBucketVersions are separate actions from their unversioned
        # counterparts, and both are needed for a delete that actually removes the bytes.
        Action = ["s3:GetObject", "s3:ListBucket", "s3:DeleteObject",
        "s3:ListBucketVersions", "s3:DeleteObjectVersion"]
        Resource = [local.storage_bucket_arn, "${local.storage_bucket_arn}/*"]
      },
      {
        # form intake (+ caption so find works) and the marker bump
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectAnnotation"]
        Resource = ["${local.storage_bucket_arn}/submissions/*", "${local.storage_bucket_arn}/state/portal.version"]
      },
      {
        # captions are S3 annotations, so a browsable listing reads them for titles and notes
        Effect   = "Allow"
        Action   = ["s3:GetObjectAnnotation", "s3:ListObjectAnnotations"]
        Resource = "${local.storage_bucket_arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.storage_kms_key_arn
      },
      {
        # /data/tasks — the open queue, same read manage_tasks query serves the agent
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = [local.tasks_table_arn, "${local.tasks_table_arn}/index/*"]
      },
      {
        # the stream trigger (marker bumps)
        Effect   = "Allow"
        Action   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
        Resource = var.tasks_stream_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
      ], var.email_bucket == "" ? [] : [
      {
        # inbound mail, so the owner can browse and clear what arrived. This bucket is NOT
        # versioned, so a delete here is permanent — unlike the cabinet, where prior versions
        # survive one.
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket", "s3:DeleteObject",
        "s3:ListBucketVersions", "s3:DeleteObjectVersion"]
        Resource = ["arn:aws:s3:::${var.email_bucket}", "arn:aws:s3:::${var.email_bucket}/in/*"]
      },
    ])
  })
}

module "ui" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-ui"
  role            = aws_iam_role.ui.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/storage/lambdas/ui.zip"
  src_dir         = "modules/storage/lambdas/ui"
  gerp_id         = var.gerp_id
  handler         = "index.handler"
  runtime         = "nodejs22.x"
  timeout         = 15
  env_vars = {
    STORAGE_BUCKET = var.storage_bucket
    EMAIL_BUCKET   = var.email_bucket
    PORTAL_SLUG    = random_id.portal_slug.hex
    TASKS_TABLE    = var.tasks_table
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.ui
  to   = module.ui.aws_lambda_function.this
}

resource "aws_lambda_function_url" "ui" {
  function_name      = module.ui.name
  authorization_type = "NONE" # the slug in the path is the capability; checked in code
}

resource "aws_lambda_permission" "ui_url" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix    = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.ui.name
  principal              = "*"
  function_url_auth_type = "NONE"
}

module "ui_tasks_stream" {
  source               = "../../terraform/stream"
  name                 = "${local.prefix}-ui-tasks"
  stream_arn           = var.tasks_stream_arn
  function_arn         = module.ui.arn
  role_name            = aws_iam_role.ui.name
  ops_alerts_topic_arn = var.ops_alerts_topic_arn
}

moved {
  from = aws_lambda_event_source_mapping.ui_tasks_stream
  to   = module.ui_tasks_stream.aws_lambda_event_source_mapping.this
}

output "portal_url" {
  description = "The owner portal base URL (function url + slug) — the capability link."
  value       = local.portal_url
}
