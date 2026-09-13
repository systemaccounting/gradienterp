#!/usr/bin/env bash
# build-codebuild-source.sh — produce the zip CodeBuild uses as source.
#
# Bundles the repo subset that prod/init_customer/ and prod/per_customer/ terraform need:
#   - config.json          (STACK_PREFIX, the operator account, the switches — both templates read it)
#   - scripts/sync_playbooks.sh  (post_build ingests every modules/**/kb.md into the gerp's KB)
#   - prod/init_customer/  (the export bucket and its lambda — applied first, survives closure)
#   - prod/per_customer/   (the template)
#   - every modules/<m>/ the template instantiates — ALL of them, or `terraform init`
#     inside CodeBuild fails with "Unreadable module directory" and provisioning cannot
#     run at all. tests/tower/local/test_codebuild_source.py holds this list to the
#     template's, because nothing else made adding a module here follow adding one there.
#   - .codebuild/          (the buildspec)
#
# Builds the zip only. `deploy.sh source` is what UPLOADS it to
# s3://gerp-codebuild-source-<account>/per-customer-source.zip — the zip is CODE, so it ships on
# the code path, not on an apply. A tower apply owns the bucket and will not refresh its contents.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/prod/tower/.build"
ZIP="$BUILD_DIR/per-customer-source.zip"

mkdir -p "$BUILD_DIR"
rm -f "$ZIP"

cd "$REPO_ROOT"
zip -rq "$ZIP" \
  config.json \
  scripts/sync_playbooks.sh \
  prod/init_customer \
  prod/per_customer \
  prod/hub \
  modules/accounting \
  modules/agent \
  modules/agreements \
  modules/aws \
  modules/assets \
  modules/automation \
  modules/calendar \
  modules/cmd \
  modules/contacts \
  modules/events \
  modules/export \
  modules/inbox \
  modules/inventory \
  modules/invoicing \
  modules/labor \
  modules/mcp \
  modules/notes \
  modules/payments \
  modules/playbooks \
  modules/purchasing \
  modules/rules \
  modules/schemas \
  modules/secrets \
  modules/server \
  modules/settings \
  modules/shipping \
  modules/storage \
  modules/tasks \
  modules/terraform \
  modules/treasury \
  .codebuild \
  -x \
    '*/.terraform/*' \
    '*/__pycache__/*' \
    '*/node_modules/*' \
    '*/terraform.tfstate*' \
    '*/.terraform.lock.hcl' \
    '*/terraform.tfvars' \
    '*/.build/*' \
    '*.git/*'

echo "==> built: $(du -h "$ZIP" | cut -f1) at $ZIP"
echo "==> contents (first 30):"
# `|| true`: head closes early, unzip takes SIGPIPE, and pipefail would make a
# SUCCESSFUL build exit 141 — invisible interactively, fatal to any caller.
unzip -l "$ZIP" | head -30 || true
