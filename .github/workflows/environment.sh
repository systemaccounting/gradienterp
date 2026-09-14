#!/usr/bin/env bash
# environment.sh — the `prod` environment the deploy and apply jobs run in.
#
#   bash .github/workflows/environment.sh
#
# The AWS role those jobs take (gerp-github-deploy, prod/platform/management/github_deploy.tf) trusts a
# job in `prod` and nothing else, and `prod` takes deployments from `main` alone: a workflow edited on
# another branch, a pull request or a fork never gets a job into it. No reviewers, so a dispatch starts
# at once. The role's arn is the environment's secret AWS_DEPLOY_ROLE_ARN (set with `gh secret set
# AWS_DEPLOY_ROLE_ARN --env prod`). Running it again changes nothing.
set -euo pipefail

REPO="${REPO:-systemaccounting/gradienterp}"

gh api -X PUT "repos/$REPO/environments/prod" --input - >/dev/null <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON

if ! gh api "repos/$REPO/environments/prod/deployment-branch-policies" --jq '.branch_policies[].name' | grep -qx main; then
  gh api -X POST "repos/$REPO/environments/prod/deployment-branch-policies" -f name=main -f type=branch >/dev/null
fi

gh api "repos/$REPO/environments/prod" --jq '{name, protection_rules: [.protection_rules[].type], deployment_branch_policy}'
gh api "repos/$REPO/environments/prod/deployment-branch-policies" --jq '[.branch_policies[] | "\(.type):\(.name)"]'
