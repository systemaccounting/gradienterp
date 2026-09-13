# ─── the regions a gerp can be built in: the operator's side of each ───
#
# One provider alias per region (a provider cannot be for_each'd) and one instance of the region
# module (region/) per alias: the artifact bucket lambda deploys from and the OAM sink a gerp's
# account links to, in that region. The list is config.json REGIONS; a region added there is an
# alias and a block here, the one place a new region is a tf edit rather than an entry. ECR
# replication carries the agent image to every region in one resource.

provider "aws" {
  alias  = "eu_west_1"
  region = "eu-west-1"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_eu_west_1" {
  source    = "./region"
  providers = { aws = aws.eu_west_1 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "eu-west-1"
}

provider "aws" {
  alias  = "eu_central_1"
  region = "eu-central-1"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_eu_central_1" {
  source    = "./region"
  providers = { aws = aws.eu_central_1 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "eu-central-1"
}

provider "aws" {
  alias  = "eu_west_2"
  region = "eu-west-2"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_eu_west_2" {
  source    = "./region"
  providers = { aws = aws.eu_west_2 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "eu-west-2"
}

provider "aws" {
  alias  = "ap_southeast_1"
  region = "ap-southeast-1"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_ap_southeast_1" {
  source    = "./region"
  providers = { aws = aws.ap_southeast_1 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "ap-southeast-1"
}

provider "aws" {
  alias  = "ap_northeast_1"
  region = "ap-northeast-1"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_ap_northeast_1" {
  source    = "./region"
  providers = { aws = aws.ap_northeast_1 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "ap-northeast-1"
}

provider "aws" {
  alias  = "ap_southeast_2"
  region = "ap-southeast-2"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_ap_southeast_2" {
  source    = "./region"
  providers = { aws = aws.ap_southeast_2 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "ap-southeast-2"
}

provider "aws" {
  alias  = "ap_south_1"
  region = "ap-south-1"
  default_tags {
    tags = { "gerp:stack" = "tower" }
  }
  assume_role {
    role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
  }
}

module "region_ap_south_1" {
  source    = "./region"
  providers = { aws = aws.ap_south_1 }

  stack_prefix        = local.stack_prefix
  operator_account_id = local.operator_account_id
  org_ids             = local.org_ids
  issue_collector_arn = "arn:aws:lambda:${var.aws_region}:${local.operator_account_id}:function:${local.stack_prefix}-issue-collector"
  ops_alerts_email    = var.ops_alerts_email
  region              = "ap-south-1"
}

# the agent image in every region: AgentCore pulls from ECR in its own region
resource "aws_ecr_replication_configuration" "agent" {
  replication_configuration {
    rule {
      dynamic "destination" {
        for_each = [for r in keys(local.config.REGIONS) : r if r != var.aws_region]
        content {
          region      = destination.value
          registry_id = local.operator_account_id
        }
      }
      repository_filter {
        filter      = aws_ecr_repository.agent.name
        filter_type = "PREFIX_MATCH"
      }
    }
  }
}

output "regions" {
  description = "Per region: the artifact bucket and the OAM sink arn (us-east-1 is tower's own); config.json REGION_ARTIFACTS / OAM_SINKS ride from here"
  value = merge(
    { (var.aws_region) = { artifacts_bucket = aws_s3_bucket.artifacts.id, oam_sink_arn = aws_oam_sink.gerps.arn, ops_alerts_topic_arn = aws_sns_topic.ops_alerts.arn } },
    { "eu-west-1" = { artifacts_bucket = module.region_eu_west_1.artifacts_bucket, oam_sink_arn = module.region_eu_west_1.oam_sink_arn, ops_alerts_topic_arn = module.region_eu_west_1.ops_alerts_topic_arn } },
    { "eu-central-1" = { artifacts_bucket = module.region_eu_central_1.artifacts_bucket, oam_sink_arn = module.region_eu_central_1.oam_sink_arn, ops_alerts_topic_arn = module.region_eu_central_1.ops_alerts_topic_arn } },
    { "eu-west-2" = { artifacts_bucket = module.region_eu_west_2.artifacts_bucket, oam_sink_arn = module.region_eu_west_2.oam_sink_arn, ops_alerts_topic_arn = module.region_eu_west_2.ops_alerts_topic_arn } },
    { "ap-southeast-1" = { artifacts_bucket = module.region_ap_southeast_1.artifacts_bucket, oam_sink_arn = module.region_ap_southeast_1.oam_sink_arn, ops_alerts_topic_arn = module.region_ap_southeast_1.ops_alerts_topic_arn } },
    { "ap-northeast-1" = { artifacts_bucket = module.region_ap_northeast_1.artifacts_bucket, oam_sink_arn = module.region_ap_northeast_1.oam_sink_arn, ops_alerts_topic_arn = module.region_ap_northeast_1.ops_alerts_topic_arn } },
    { "ap-southeast-2" = { artifacts_bucket = module.region_ap_southeast_2.artifacts_bucket, oam_sink_arn = module.region_ap_southeast_2.oam_sink_arn, ops_alerts_topic_arn = module.region_ap_southeast_2.ops_alerts_topic_arn } },
    { "ap-south-1" = { artifacts_bucket = module.region_ap_south_1.artifacts_bucket, oam_sink_arn = module.region_ap_south_1.oam_sink_arn, ops_alerts_topic_arn = module.region_ap_south_1.ops_alerts_topic_arn } },
  )
}

# ─── the artifacts follow: S3 replication from the us-east-1 bucket to every region's ───
#
# `deploy.sh push` writes one bucket; each region's replica fills within seconds, and a gerp
# there deploys from it (scripts/deploy.py waits for the checksum it pushed). A new region's
# bucket takes the existing artifacts once: `aws s3 sync s3://<us-east-1 bucket> s3://<region
# bucket>` (replication carries writes from then on).

resource "aws_iam_role" "artifacts_replication" {
  name = "${local.stack_prefix}-artifacts-replication"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "s3.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "artifacts_replication" {
  name = "${local.stack_prefix}-artifacts-replication"
  role = aws_iam_role.artifacts_replication.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = aws_s3_bucket.artifacts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Effect = "Allow"
        Action = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
        Resource = [
          "arn:aws:s3:::${module.region_eu_west_1.artifacts_bucket}/*",
          "arn:aws:s3:::${module.region_eu_central_1.artifacts_bucket}/*",
          "arn:aws:s3:::${module.region_eu_west_2.artifacts_bucket}/*",
          "arn:aws:s3:::${module.region_ap_southeast_1.artifacts_bucket}/*",
          "arn:aws:s3:::${module.region_ap_northeast_1.artifacts_bucket}/*",
          "arn:aws:s3:::${module.region_ap_southeast_2.artifacts_bucket}/*",
          "arn:aws:s3:::${module.region_ap_south_1.artifacts_bucket}/*",
        ]
      },
    ]
  })
}

resource "aws_s3_bucket_replication_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  role   = aws_iam_role.artifacts_replication.arn

  rule {
    id       = "to-eu-west-1"
    status   = "Enabled"
    priority = 1
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_eu_west_1.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  rule {
    id       = "to-eu-central-1"
    status   = "Enabled"
    priority = 2
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_eu_central_1.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  rule {
    id       = "to-eu-west-2"
    status   = "Enabled"
    priority = 3
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_eu_west_2.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  rule {
    id       = "to-ap-southeast-1"
    status   = "Enabled"
    priority = 4
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_ap_southeast_1.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  rule {
    id       = "to-ap-northeast-1"
    status   = "Enabled"
    priority = 5
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_ap_northeast_1.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  rule {
    id       = "to-ap-southeast-2"
    status   = "Enabled"
    priority = 6
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_ap_southeast_2.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  rule {
    id       = "to-ap-south-1"
    status   = "Enabled"
    priority = 7
    filter {}
    delete_marker_replication { status = "Disabled" }
    destination {
      bucket        = "arn:aws:s3:::${module.region_ap_south_1.artifacts_bucket}"
      storage_class = "STANDARD"
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}
