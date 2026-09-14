###############################################
# GitHub Actions reaches AWS through this role: the deploy and apply workflows
# (.github/workflows/deploy.yaml, apply.yaml) run the same scripts as the laptop,
# and every profile those scripts name chains from `default`. The job's shared
# step (.github/actions/aws) writes this role's credentials as `[default]`.
#
# Trust: a job in the repo's `prod` environment and nothing else. The repo's tokens carry GitHub's
# immutable subject, owner and repo each with its id (`gh api
# repos/systemaccounting/gradienterp/actions/oidc/customization/sub`), so a repo renamed or recreated
# under the same name is not this one. The environment
# takes deployment branches from `main` only (.github/workflows/environment.sh),
# so a pull request, a fork or a workflow edited on another branch gets no token
# this trust accepts.
#
# Permission: assume the operator account's OrganizationAccountAccessRole, which
# every stack a workflow applies starts from. Nothing in this account: the
# management stack applies from the laptop.
###############################################

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "github_deploy" {
  name = "gerp-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      # configure-aws-credentials tags its session
      Action = ["sts:AssumeRoleWithWebIdentity", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:systemaccounting@12200511/gradienterp@1368357735:environment:prod"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "assume-operator"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = "arn:aws:iam::${aws_organizations_account.operator.id}:role/OrganizationAccountAccessRole"
    }]
  })
}

output "github_deploy_role_arn" {
  description = "The role the deploy and apply workflows take; the prod environment's AWS_DEPLOY_ROLE_ARN secret."
  value       = aws_iam_role.github_deploy.arn
}
