###############################################
# Operator-side ECR for the agent container image.
#
# Single repo, one image, all customers' AgentCore Runtimes pull from here
# cross-account via an org-scoped ECR resource policy. Replaces the old
# per-customer ECR pattern (modules/agent/infra/ecr.tf, retired).
#
# Image build + push happens operator-side (one of):
#   - manual: bash scripts/publish-agent-image.sh <tag>
#   - CI: github action on agent module changes
#   - operator-side codebuild: triggered when modules/agent/docker/ changes
# Per-customer terraform pins to a tag and pulls; never builds or pushes.
###############################################

resource "aws_ecr_repository" "agent" {
  name                 = "agentcore"
  image_tag_mutability = "IMMUTABLE" # version-tag everything; never overwrite

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Retention: keep the last 10 tagged versions (rollback headroom + recent history), and expire
# untagged manifests after 14 days. Two rules, not one `tagStatus=any` count: each historical
# buildx push landed 3 artifacts (tagged manifest + untagged attestation + untagged platform
# child), so a flat "keep 10 images" retained only ~3 versions. The count now applies to TAGGED
# images (= 10 real versions); the untagged rule sweeps the legacy attestation/child debris.
# (Going forward `scripts/docker.sh` builds with --provenance/--sbom off, so new pushes are a
# single tagged image with no untagged children — the untagged rule just cleans up the past.)
resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "expire untagged manifests (legacy attestation/platform children) after 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "keep the last 10 tagged versions"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
    ]
  })
}

# Amazon Inspector scans the image for OS and package vulnerabilities on push, and again whenever a
# new vulnerability is published against a package in it. us-east-1 alone: every other region holds
# a replica of the same digests. A HIGH or CRITICAL finding reaches gerp-ops-alerts (alerts.tf).
resource "aws_inspector2_enabler" "ecr" {
  account_ids    = [data.aws_caller_identity.current.account_id]
  resource_types = ["ECR"]
}

resource "aws_ecr_registry_scanning_configuration" "agent" {
  scan_type = "ENHANCED"

  rule {
    scan_frequency = "CONTINUOUS_SCAN"
    repository_filter {
      filter      = aws_ecr_repository.agent.name
      filter_type = "WILDCARD"
    }
  }

  depends_on = [aws_inspector2_enabler.ecr]
}

# Org-scoped read policy. Any principal in the org (every customer's AgentCore
# Runtime execution role) can pull. ECR's GetAuthorizationToken is account-
# level (not resource-policied) so consumers also need that on their own role.
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
        "ecr:DescribeImages",       # data.aws_ecr_image (most_recent) — the per-customer runtime pins the newest digest at plan
        "ecr:DescribeRepositories", # the same data source's provider pre-read
      ]
      Condition = {
        StringEquals = {
          "aws:PrincipalOrgID" = local.org_ids
        }
      }
    }]
  })
}

output "agent_ecr_repository_url" {
  value       = aws_ecr_repository.agent.repository_url
  description = "ECR repository URL for the agent container image. Per-customer terraform pins a tag (default 'latest', or a version like 'v1') and AgentCore Runtime pulls from here cross-account."
}

output "agent_ecr_repository_arn" {
  value       = aws_ecr_repository.agent.arn
  description = "ECR repository ARN. Per-customer agent execution role's IAM policy references this for ecr:BatchGetImage / ecr:GetDownloadUrlForLayer (cross-account allowed via the org-scoped resource policy)."
}
