# ─── what the operator holds in one hub region: the artifact bucket and the ops sink ───
#
# Lambda takes its package from a bucket in its own region and CloudWatch's cross-account
# observability links to a sink in the account's own region, so each region a gerp can be built
# in gets both, the shape of tower's us-east-1 ones (artifacts.tf, alerts.tf). Instantiated once
# per region in tower's regions.tf with that region's provider alias.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "stack_prefix" {
  type = string
}

variable "operator_account_id" {
  type = string
}

variable "org_ids" {
  type = list(string)
}

variable "region" {
  type = string
}

variable "issue_collector_arn" {
  description = "the issue collector in us-east-1 (prod/platform/operator), subscribed to this region's ops topic"
  type        = string
}

variable "ops_alerts_email" {
  type    = string
  default = ""
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.stack_prefix}-artifacts-${var.operator_account_id}-${var.region}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 60
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgRead"
      Effect    = "Allow"
      Principal = "*"
      Action = [
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:GetObjectAttributes",
        "s3:GetObjectVersionAttributes",
        "s3:GetObjectAnnotation",
        "s3:ListObjectAnnotations",
        "s3:GetObjectTagging",
        "s3:GetObjectVersionTagging",
        "s3:ListBucket",
        "s3:ListBucketVersions",
      ]
      Resource = [
        aws_s3_bucket.artifacts.arn,
        "${aws_s3_bucket.artifacts.arn}/*",
      ]
      Condition = {
        StringEquals = { "aws:PrincipalOrgID" = var.org_ids }
      }
    }]
  })
}

# an alarm notifies a topic in its own region only: this region's ops topic, which the alarms of
# the hub and the gerps here publish to, and which hands every state change to the issue
# collector in us-east-1 (a cross-region subscription) and to the ops email
resource "aws_sns_topic" "ops_alerts" {
  name = "${var.stack_prefix}-ops-alerts"
}

resource "aws_sns_topic_policy" "ops_alerts" {
  arn = aws_sns_topic.ops_alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "gerp-alarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.ops_alerts.arn
        Condition = { ArnLike = { "aws:SourceArn" = "arn:aws:cloudwatch:${var.region}:*:alarm:${var.stack_prefix}-*" } }
      },
      {
        Sid       = "access-findings"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.ops_alerts.arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.access_finding.arn } }
      },
    ]
  })
}

# this region's resources granting access outside the organization, as tower's alerts.tf does for
# us-east-1 and IAM
resource "aws_accessanalyzer_analyzer" "org" {
  analyzer_name = "${var.stack_prefix}-org-access"
  type          = "ORGANIZATION"
}

# IAM is global and tower's us-east-1 analyzer reports it; here a role's finding is a copy
resource "aws_accessanalyzer_archive_rule" "iam_in_us_east_1" {
  analyzer_name = aws_accessanalyzer_analyzer.org.analyzer_name
  rule_name     = "iam-reported-in-us-east-1"

  filter {
    criteria = "resourceType"
    eq       = ["AWS::IAM::Role"]
  }
}

# the public function urls that authenticate their own callers, as tower's alerts.tf archives them
resource "aws_accessanalyzer_archive_rule" "function_urls" {
  analyzer_name = aws_accessanalyzer_analyzer.org.analyzer_name
  rule_name     = "self-authenticating-function-urls"

  filter {
    criteria = "resourceType"
    eq       = ["AWS::Lambda::Function"]
  }
  filter {
    criteria = "resource"
    contains = ["-chat", "-ui"]
  }
}

resource "aws_cloudwatch_event_rule" "access_finding" {
  name        = "${var.stack_prefix}-ops-access-finding"
  description = "IAM Access Analyzer found access from outside the organization"
  event_pattern = jsonencode({
    source        = ["aws.access-analyzer"]
    "detail-type" = ["Access Analyzer Finding"]
    detail        = { status = ["ACTIVE"] }
  })
}

resource "aws_cloudwatch_event_target" "access_finding" {
  rule      = aws_cloudwatch_event_rule.access_finding.name
  target_id = "ops-alerts"
  arn       = aws_sns_topic.ops_alerts.arn

  input_transformer {
    input_paths = {
      type      = "$.detail.resourceType"
      resource  = "$.detail.resource"
      account   = "$.detail.accountId"
      principal = "$.detail.principal"
      public    = "$.detail.isPublic"
      finding   = "$.detail.id"
    }
    input_template = <<-EOT
      {
        "what": "access from outside the organization: <type> <resource>",
        "region": "${var.region}",
        "account": "<account>",
        "principal": <principal>,
        "public": <public>,
        "finding": "<finding>",
        "then": "remove the grant in terraform, or archive the finding on analyzer ${var.stack_prefix}-org-access when the access is intended"
      }
    EOT
  }
}

resource "aws_sns_topic_subscription" "ops_alerts_collector" {
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "lambda"
  endpoint  = var.issue_collector_arn
}

resource "aws_sns_topic_subscription" "ops_alerts_email" {
  count     = var.ops_alerts_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "email"
  endpoint  = var.ops_alerts_email
}

resource "aws_oam_sink" "gerps" {
  name = "${var.stack_prefix}-gerps"
}

resource "aws_oam_sink_policy" "gerps" {
  sink_identifier = aws_oam_sink.gerps.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = ["oam:CreateLink", "oam:UpdateLink"]
      Resource  = "*"
      Condition = {
        "ForAllValues:StringEquals" = { "oam:ResourceTypes" = ["AWS::CloudWatch::Metric", "AWS::Logs::LogGroup"] }
        StringEquals                = { "aws:PrincipalOrgID" = var.org_ids }
      }
    }]
  })
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "oam_sink_arn" {
  value = aws_oam_sink.gerps.arn
}

output "ops_alerts_topic_arn" {
  value = aws_sns_topic.ops_alerts.arn
}

# the agent image's repository in this region: replication (tower regions.tf) fills it from
# us-east-1's but carries no policy, and AgentCore in a gerp's account pulls from its own
# region, so the org-read policy of agent_image.tf is declared here on the replica too
resource "aws_ecr_repository" "agent" {
  name                 = "agentcore"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
}

resource "aws_ecr_repository_policy" "agent_org_read" {
  repository = aws_ecr_repository.agent.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgWideAgentImagePull"
      Effect    = "Allow"
      Principal = "*"
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
      ]
      Condition = { StringEquals = { "aws:PrincipalOrgID" = var.org_ids } }
    }]
  })
}
