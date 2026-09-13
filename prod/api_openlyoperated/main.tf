###############################################
# api.openlyoperated.biz — publication path.
#
# Bus → rule → firehose → s3 archive. The rule is the gate (no publisher
# lambda). Customer modules emit events with detail.openly_operated set at
# emit time; the rule pattern matches true; firehose batches and writes
# raw json gzip-compressed to s3, partitioned by date.
#
# Read path (api gateway + lambdas + materialized statements bucket +
# athena workgroup + custom domain) lands in follow-up commits — see
# this dir's TODO.md.
###############################################

###############################################
# Pull the shared bus from prod/platform/operator/
###############################################

locals {
  config              = jsondecode(file("${path.module}/../../config.json"))
  stack_prefix        = local.config.STACK_PREFIX
  operator_account_id = local.config.OPERATOR_ACCOUNT_ID
}

data "terraform_remote_state" "operator" {
  backend = "s3"
  config = {
    bucket = "gradienterp-tfstate-185369506315"
    key    = "platform/operator/terraform.tfstate"
    region = "us-east-1"
    assume_role = {
      role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
    }
  }
}

###############################################
# Event archive bucket — append-only, source of truth for the public feed
###############################################

resource "aws_s3_bucket" "archive" {
  bucket        = "${local.stack_prefix}-events-archive-${local.operator_account_id}"
  force_destroy = true # POC only — no real published events yet. Remove before the public feed carries real data (this is the append-only source of truth).
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

###############################################
# Firehose — buffers events, writes gzip json to s3 partitioned by date
###############################################

resource "aws_iam_role" "firehose" {
  name = "${local.stack_prefix}-firehose-events-archive"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "firehose_s3" {
  role = aws_iam_role.firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ]
      Resource = [
        aws_s3_bucket.archive.arn,
        "${aws_s3_bucket.archive.arn}/*",
      ]
    }]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "events_archive" {
  name        = "${local.stack_prefix}-events-archive"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.archive.arn
    buffering_size      = 5
    buffering_interval  = 60
    compression_format  = "GZIP"
    prefix              = "events/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "errors/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/!{firehose:error-output-type}/"
  }
}

###############################################
# Publication rule on the shared bus — the gate
###############################################

resource "aws_cloudwatch_event_rule" "publication" {
  name           = "${local.stack_prefix}-publication"
  description    = "Routes openly_operated=true events to the public archive"
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name

  event_pattern = jsonencode({
    detail = {
      openly_operated = [true]
    }
  })
}

resource "aws_iam_role" "events_to_firehose" {
  name = "${local.stack_prefix}-events-to-firehose"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "events_to_firehose" {
  role = aws_iam_role.events_to_firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "firehose:PutRecord",
        "firehose:PutRecordBatch",
      ]
      Resource = aws_kinesis_firehose_delivery_stream.events_archive.arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "publication_to_firehose" {
  rule           = aws_cloudwatch_event_rule.publication.name
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  arn            = aws_kinesis_firehose_delivery_stream.events_archive.arn
  role_arn       = aws_iam_role.events_to_firehose.arn
}
