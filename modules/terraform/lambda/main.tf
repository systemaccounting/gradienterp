# One lambda function, and what every function owns: its log group with a retention, the metric
# filters on its ERROR lines, the artifact pin and the fleet tag. Called once per function
# with `for_each` over the caller's own map; the role, the map and every reference to the
# function stay with the caller (`module.fn["x"].arn`).
#
# The log group is declared here and the function depends on it, so the group is owned before
# the first invoke can create it unowned — a group Lambda creates itself has no retention and
# survives a destroy. No alarm lives here: a raise is counted by the account's dimensionless
# `AWS/Lambda Errors` and a caught failure by `gerp/app/<scope> ErrorLines`, and the ONE alarm
# on each is the root's (`prod/per_customer`, `prod/tower`). Alarms that are about the app live
# in the calling module's alarms.tf.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# code comes from the ARTIFACT BUCKET at the pinned latest version — push before apply
# (scripts/deploy.sh). Any applier (codebuild, a stale checkout) deploys bucket truth, never its
# local tree. A NEW function is push-then-apply: this read fails the plan until its artifact exists.
# Lambda takes its package from a bucket in its own region: the operator's artifact bucket is
# one per region (prod/tower regions.tf), the first region's under the bare name and every
# other's suffixed with the region; scripts/deploy.sh push writes them all
locals {
  artifact_bucket = data.aws_region.current.region == "us-east-1" ? var.artifact_bucket : "${var.artifact_bucket}-${data.aws_region.current.region}"
}

data "aws_s3_object" "artifact" {
  count  = var.artifact_key != "" ? 1 : 0
  bucket = local.artifact_bucket
  key    = var.artifact_key
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "this" {
  depends_on = [aws_cloudwatch_log_group.this]

  function_name = var.name
  description   = var.description
  role          = var.role
  handler       = var.handler
  runtime       = var.runtime
  timeout       = var.timeout
  memory_size   = var.memory
  # fleet marker + source mapping — one tag query enumerates deployables AND says what to zip
  tags = merge(var.tags, { "gerp:src-dir" = var.src_dir })

  # two shapes: the fleet's zip in the artifact bucket (deploy.sh push), or an archive_file the
  # applier builds (the operator stacks: tower, the BFF, the read api)
  s3_bucket         = var.artifact_key != "" ? local.artifact_bucket : null
  s3_key            = var.artifact_key != "" ? data.aws_s3_object.artifact[0].key : null
  s3_object_version = var.artifact_key != "" ? data.aws_s3_object.artifact[0].version_id : null
  filename          = var.filename != "" ? var.filename : null
  source_code_hash  = var.source_code_hash != "" ? var.source_code_hash : null
  layers            = var.layers

  environment {
    variables = merge(var.gerp_id != "" ? { GERP_ID = var.gerp_id, CUSTOMER_ID = var.gerp_id } : {}, var.env_vars)
  }

  # one JSON object per line — level, timestamp, requestId and the ids as top-level fields, what
  # every log backend parses on ingest. `aws.log` writes through the root logger with `extra`,
  # which the runtime spreads at the top level; a `print` of a JSON string lands as-is.
  logging_config {
    log_format            = "JSON"
    application_log_level = var.log_level
    system_log_level      = "WARN"
  }

  dynamic "dead_letter_config" {
    for_each = var.dead_letter_arn != "" ? [1] : []
    content {
      target_arn = var.dead_letter_arn
    }
  }
}

# ─── the failures the code caught ───
#
# The runtime's `Errors` metric counts raises. `ErrorLines` counts the lines the function wrote
# at ERROR — the failures it caught and answered (a 502, a parked stream record, a lost publish),
# which the runtime never sees. Two filters on the same pattern: one into the stack's own
# namespace (`gerp/app/<gerp>`, or `gerp/app/operator`) with no dimensions, which the ONE alarm
# per stack watches (`prod/per_customer`, `prod/tower`); one by function, `kind` and `category`
# for the count — a metric only exists once a line is written, so an idle function costs nothing.

locals {
  app_scope = var.gerp_id != "" ? var.gerp_id : "operator"
}

resource "aws_cloudwatch_log_metric_filter" "error_lines" {
  name           = "${var.name}-error-lines"
  log_group_name = aws_cloudwatch_log_group.this.name
  pattern        = "{ $.level = \"ERROR\" && $.function = \"*\" }"

  metric_transformation {
    namespace = "gerp/app/${local.app_scope}"
    name      = "ErrorLines"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "error_lines_by_kind" {
  name           = "${var.name}-error-lines-by-kind"
  log_group_name = aws_cloudwatch_log_group.this.name
  pattern        = "{ $.level = \"ERROR\" && $.kind = \"*\" }"

  metric_transformation {
    namespace  = "gerp/app"
    name       = "ErrorLinesByKind"
    value      = "1"
    unit       = "Count"
    dimensions = { FunctionName = "$.function", kind = "$.kind", category = "$.category" }
  }
}
