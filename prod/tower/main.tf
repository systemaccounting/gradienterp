###############################################
# Tower — operator control plane.
#
# CodeBuild project that runs prod/per_customer/ terraform on trigger.
# provision_customer triggers this project after a new sub-account is vended.
#
# Source: S3. release/source.zip (a committed working tree, `zip.sh source` then
# `upload.sh source --release`) is built and uploaded by those scripts, not by
# an apply — the zip is CODE, and the lambda-code split applies to it for the same
# reason. This stack owns the BUCKET only. The lambda just StartBuilds.
###############################################

locals {
  config              = jsondecode(file("${path.module}/../../config.json"))
  stack_prefix        = local.config.STACK_PREFIX
  operator_account_id = local.config.OPERATOR_ACCOUNT_ID
  # the operator's artifact bucket (prod/platform/operator artifacts.tf), by the convention every
  # reader derives; tower's functions source their code from it like every module's
  artifact_bucket = "${local.stack_prefix}-artifacts-${local.operator_account_id}"
  # every org-scoped trust condition (aws:PrincipalOrgID / aws:ResourceOrgID) reads this list:
  # the organization this stack runs in plus the ones admitted in config.json ORG_IDS
  org_ids = concat([data.aws_organizations_organization.this.id], local.config.ORG_IDS)
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_organizations_organization" "this" {}

# Read management's outputs (TowerProvisioning role arn, customers OU id, root id).
data "terraform_remote_state" "management" {
  backend = "s3"
  config = {
    bucket = var.tfstate_bucket
    key    = "platform/management/terraform.tfstate"
    region = "us-east-1"
    assume_role = {
      role_arn = "arn:aws:iam::${local.operator_account_id}:role/OrganizationAccountAccessRole"
    }
  }
}

###############################################
# S3 source bucket — codebuild downloads its source from here. Two keys: `release/source.zip`, a
# committed tree (`upload.sh source --release`) and both projects' own location, so what a signup,
# a hub vend and a closure build from; and `source.zip`, every upload, which the operator's builds
# and the workflows name through `sourceLocationOverride`.
###############################################

resource "aws_s3_bucket" "codebuild_source" {
  bucket        = "${local.stack_prefix}-codebuild-source-${local.operator_account_id}"
  force_destroy = true # the source zips are rebuilt + re-uploaded; safe to drop on replace/teardown
}

resource "aws_s3_bucket_versioning" "codebuild_source" {
  bucket = aws_s3_bucket.codebuild_source.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "codebuild_source" {
  bucket = aws_s3_bucket.codebuild_source.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "codebuild_source" {
  bucket                  = aws_s3_bucket.codebuild_source.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "codebuild_source" {
  bucket = aws_s3_bucket.codebuild_source.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

###############################################
# CodeBuild service role
#
# Permissions:
#   - read source zips from the S3 source bucket
#   - read/write tfstate bucket (operator) + DDB lock table
#   - assume OperatorOrchestration role into ANY org member account
#     (scoped via aws:ResourceOrgID condition; role deployed by the
#     customers-OU stackset, trusts operator)
#   - CloudWatch logs write
#   - SSM read on /gradienterp/customers/* (per_customer terraform reads tenant blob)
###############################################

resource "aws_iam_role" "codebuild" {
  name = "tower-per-customer-codebuild"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  name = "tower-per-customer-codebuild"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SourceBucketRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.codebuild_source.arn,
          "${aws_s3_bucket.codebuild_source.arn}/*",
        ]
      },
      {
        Sid    = "TfstateBucketReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:GetObjectTagging",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.tfstate_bucket}",
          "arn:aws:s3:::${var.tfstate_bucket}/*",
        ]
      },
      {
        Sid    = "TfstateLockTable"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
        ]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.tfstate_lock_table}"
      },
      {
        # per_customer writes the gerp's agent-email records into the operator's gradienterp.cloud
        # zone (the `aws.operator` provider runs as this role): the zone lookup and its records
        Sid    = "AgentEmailZone"
        Effect = "Allow"
        Action = ["route53:ListHostedZones", "route53:GetHostedZone", "route53:ListResourceRecordSets",
        "route53:ChangeResourceRecordSets", "route53:GetChange", "route53:ListTagsForResource"]
        Resource = "*"
      },
      {
        # per_customer makes the gerp's firm client on the operator's Cognito pool (modules/mcp,
        # the `aws.operator` provider): one app client per gerp, read back on every apply
        Sid    = "FirmCognitoClient"
        Effect = "Allow"
        Action = ["cognito-idp:CreateUserPoolClient", "cognito-idp:DescribeUserPoolClient", "cognito-idp:UpdateUserPoolClient",
        "cognito-idp:DeleteUserPoolClient", "cognito-idp:DescribeUserPool", "cognito-idp:DescribeResourceServer"]
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:userpool/${local.config.COGNITO_USER_POOL_ID}"
      },
      {
        # Stash per_customer outputs (chat_url) onto the gerp's customers row after
        # apply, so the dashboard can deep-link to the gerp's web chat. The row is
        # in this (operator) account; the buildspec's post_build writes it.
        Sid      = "GerpCustomersRowWrite"
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        # Cross-account assume into customer sub-accounts. Scoped to org-only
        # via aws:ResourceOrgID — codebuild can only assume into accounts in
        # this AWS Organization.
        # closure runs the customer's own export before destroying their instance
        # (.codebuild/per-customer.yml, TF_ACTION=destroy). Cross-account by RESOURCE POLICY — the
        # callee names this role (modules/export, closure_invoker_role_arn) — so there is no
        # session to create in the middle of a bash loop. Wildcard account because the target is
        # whichever gerp is closing; the function name is what narrows it.
        Sid      = "InvokeCustomerExportForClosure"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:*:*:function:${local.stack_prefix}-export-*-export_gerp"
      },
      {
        Sid      = "AssumeIntoCustomerSubAccounts"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/OperatorOrchestration"
        Condition = {
          StringEquals = {
            "aws:ResourceOrgID" = local.org_ids
          }
        }
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/codebuild/*"
      },
    ]
  })
}

###############################################
# CodeBuild project
#
# Source: S3 (zip uploaded by lambda before each build).
# Buildspec: .codebuild/per-customer.yml in the source zip.
# Per-build env vars (CUSTOMER_ID, CUSTOMER_ACCOUNT_ID) overridden via
# StartBuild --environment-variables-override.
###############################################

resource "aws_codebuild_project" "per_customer" {
  name         = "tower-per-customer"
  description  = "Runs prod/per_customer/ terraform against a customer sub-account. Triggered by tower's provision_customer lambda."
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type            = "LINUX_CONTAINER"
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    privileged_mode = false

    environment_variable {
      name  = "SENDER_EMAIL"
      value = "ops+sender@gradienterp.cloud"
    }

    environment_variable {
      name  = "CHAT_BASE_URL"
      value = "https://gradienterp.cloud/chat"
    }

    environment_variable {
      name  = "CUSTOMERS_TABLE"
      value = "${local.stack_prefix}-customers"
    }

    # where an apply that needed a second pass says so (.codebuild/per-customer.yml, tf_apply)
    environment_variable {
      name  = "OPS_ALERTS_TOPIC_ARN"
      value = aws_sns_topic.ops_alerts.arn
    }

  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.codebuild_source.bucket}/release/source.zip"
    buildspec = ".codebuild/per-customer.yml"
  }

  logs_config {
    cloudwatch_logs {
      status     = "ENABLED"
      group_name = aws_cloudwatch_log_group.per_customer.name
    }
  }

  build_timeout = 30 # minutes
}

# every build's log (the apply, destroy or stop of one gerp), at the fleet's retention
resource "aws_cloudwatch_log_group" "per_customer" {
  name              = "/aws/codebuild/tower-per-customer"
  retention_in_days = local.config.LOG_RETENTION_DAYS
}

# The hub build: prod/hub against a hub account (.codebuild/hub.yml). Same source zip, same
# role — the role assumes OperatorOrchestration into any account of the org.
resource "aws_codebuild_project" "hub" {
  name         = "tower-hub"
  description  = "Runs prod/hub/ terraform against a hub account. Started by tower's provision_customer lambda on a hub vend."
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type            = "LINUX_CONTAINER"
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    privileged_mode = false
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.codebuild_source.bucket}/release/source.zip"
    buildspec = ".codebuild/hub.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.hub.name
    }
  }
}

resource "aws_cloudwatch_log_group" "hub" {
  name              = "/aws/codebuild/tower-hub"
  retention_in_days = local.config.LOG_RETENTION_DAYS
}

###############################################
# Source zip — the working tree, built by `bash scripts/zip.sh source` and uploaded by
# `bash scripts/upload.sh source` (`--release` for what a signup builds from). Upload it before a
# build that should run a changed per_customer/, modules/ or .codebuild/.
###############################################

###############################################
# provision_customer lambda
#
# Creates a new sub-account, seeds SSM tenant metadata, triggers codebuild.
# Bash equivalent: .github/workflows/per-customer-apply.sh.
###############################################

resource "aws_iam_role" "provision_customer" {
  name = "tower-provision-customer"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "provision_customer" {
  name = "tower-provision-customer"
  role = aws_iam_role.provision_customer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AssumeTowerProvisioning"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
      },
      {
        # Direct assume into the OperatorOrchestration role on any customer
        # account (deployed by the customers-OU stackset). Replaces the old
        # OperatorOrchestration replaces the OrganizationAccountAccessRole +
        # trust-widening dance the original DIY flow used.
        Sid      = "AssumeOperatorOrchestration"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/OperatorOrchestration"
        Condition = {
          StringEquals = {
            "aws:ResourceOrgID" = local.org_ids
          }
        }
      },
      {
        Sid      = "StartCodeBuild"
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = [aws_codebuild_project.per_customer.arn, aws_codebuild_project.hub.arn]
      },
      {
        # a vended gerp's spoke edge, through its region's hub door (prod/hub, another account)
        Sid      = "HubDoor"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:*:*:function:${local.stack_prefix}-hub-manage-edges"
        Condition = {
          StringEquals = { "aws:ResourceOrgID" = local.org_ids }
        }
      },
      {
        # The vended account id goes onto the gerp's row the moment the account exists — before the
        # CodeBuild that follows can fail. An account with no row pointing at it is one nothing can
        # find, bill or tear down. Read back on a re-run, so a row that names its account is
        # resumed rather than vended twice.
        Sid      = "RecordVendedAccount"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        # the directory row every gerp reads to address this one (modules/events)
        Sid      = "WriteDirectory"
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-directory"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "provision_customer" {
  source = "../../modules/terraform/lambda"

  name            = "tower-provision-customer"
  role            = aws_iam_role.provision_customer.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/provision_customer.zip"
  src_dir         = "prod/tower/lambdas/provision_customer"
  timeout         = 900
  env_vars = {
    STACK_PREFIX          = local.stack_prefix
    CUSTOMERS_TABLE       = "${local.stack_prefix}-customers"
    DIRECTORY_TABLE       = "${local.stack_prefix}-directory" # the row every gerp reads to address this one
    CODEBUILD_PROJECT     = aws_codebuild_project.per_customer.name
    HUB_CODEBUILD_PROJECT = aws_codebuild_project.hub.name
    # the organizations a vend may go into, keyed by org id — one entry; a second organization
    # is a second entry with its own management outputs
    ORGS = jsonencode({
      (data.aws_organizations_organization.this.id) = {
        tower_provisioning_role = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
        af_product_id           = data.terraform_remote_state.management.outputs.ct_af_product_id
        af_path_id              = data.terraform_remote_state.management.outputs.ct_af_path_id
        customers_ou            = data.terraform_remote_state.management.outputs.customers_ou_managed_name
        customers_ous           = try(data.terraform_remote_state.management.outputs.customers_ous, {}) # by region (management regions.tf)
        hubs_ou                 = data.terraform_remote_state.management.outputs.hubs_ou_managed_name
      }
    })
    # the regions a gerp can be built in and the model per profile family (config.json): the
    # vend's region picks the OU, the hub and the model whose agreement the account gets
    # compact: a lambda's environment caps at 4KB. REGIONS as region → model; HUBS as
    # region → account (the bus and door arns follow from the account and the region)
    REGIONS = jsonencode({ for k, v in local.config.REGIONS : k => v.model })
    # the hubs by region (config.json): a gerp's row names its hub and its spoke edge is added there
    HUBS = jsonencode({ for k, v in local.config.HUBS : k => v.account })
    # model access is per account: the form and one agreement per model, in the vended account
    BEDROCK_MODEL_IDS     = join(",", var.bedrock_model_ids)
    BEDROCK_USE_CASE_FORM = jsonencode(var.bedrock_use_case_form)
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.provision_customer
  to   = module.provision_customer.aws_lambda_function.this
}

###############################################
# cognito_post_confirmation lambda
#
# Cognito post-confirmation trigger. Signup creates an account (the Cognito
# identity) ONLY — this hook does NOT provision a sub-account. Provisioning is
# decoupled to an explicit gerp-instance capability action (a post-signup toggle;
# prod/tower/TODO.md). The hook just completes signup + logs the new account;
# it's the seam for future post-signup account bootstrapping.
#
# The user pool's lambda_config (in prod/platform/operator/cognito.tf) points
# at this function by constructed ARN — no terraform_remote_state coupling
# either way. lambda_permission below grants Cognito the right to invoke.
###############################################

resource "aws_iam_role" "cognito_post_confirmation" {
  name = "tower-cognito-post-confirmation"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "cognito_post_confirmation" {
  name = "tower-cognito-post-confirmation"
  role = aws_iam_role.cognito_post_confirmation.id

  # Logs + seed the new account's private profile row. Signup no longer provisions, so
  # this role does NOT carry lambda:InvokeFunction on provision_customer — provisioning is
  # decoupled to an explicit gerp-instance capability action (see the lambda + TODO.md).
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Sid      = "SeedAccountProfile"
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-accounts"
      },
    ]
  })
}

module "cognito_post_confirmation" {
  source = "../../modules/terraform/lambda"

  name            = "tower-cognito-post-confirmation"
  role            = aws_iam_role.cognito_post_confirmation.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/cognito_post_confirmation.zip"
  src_dir         = "prod/tower/lambdas/cognito_post_confirmation"
  timeout         = 10
  env_vars = {
    ACCOUNTS_TABLE = "${local.stack_prefix}-accounts"
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.cognito_post_confirmation
  to   = module.cognito_post_confirmation.aws_lambda_function.this
}

# Look up the gradienterp user pool by name so we don't need to read operator's
# tfstate — direct data source instead of terraform_remote_state.
data "aws_cognito_user_pools" "gradienterp" {
  name = "gradienterp"
}

data "aws_cognito_user_pool" "main" {
  user_pool_id = tolist(data.aws_cognito_user_pools.gradienterp.ids)[0]
}

# Allow Cognito to invoke this lambda. SourceArn pinned to the operator-account
# user pool to prevent confused-deputy attacks via crafted external pool IDs.
resource "aws_lambda_permission" "cognito_invoke_post_confirmation" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowCognitoInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.cognito_post_confirmation.name
  principal           = "cognito-idp.amazonaws.com"
  source_arn          = data.aws_cognito_user_pool.main.arn
}

###############################################
# bill_customer lambda
#
# Daily from early in the month, because nothing announces an AWS invoice:
# Billing's only EventBridge events are CloudTrail API calls and the Invoicing
# feature's are invoice-unit CRUD, so nothing fires when one is produced. Every
# day the invoice isn't there yet is a no-op.
#
# Design: tmp/billing.md.
###############################################

resource "aws_iam_role" "bill_customer" {
  name = "tower-bill-customer"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "bill_customer" {
  name = "tower-bill-customer"
  role = aws_iam_role.bill_customer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Invoicing is a management-account API. This role reads the invoices;
        # provision_customer used the same role to create the units.
        Sid      = "AssumeTowerProvisioning"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
      },
      {
        # Into the SELLER's account only — the fee is invoiced from gradienterp's
        # own gerp and the cost is booked there. This lambda never writes to a
        # customer's books.
        Sid      = "AssumeSellerOrchestration"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/OperatorOrchestration"
        Condition = {
          StringEquals = {
            "aws:ResourceOrgID" = local.org_ids
          }
        }
      },
      {
        Sid      = "ReadCustomers"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        # a settled balance clears on every priors row whose endings name the gerp
        Sid      = "SettlePriors"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-priors"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        # the org's account count against its quota and the customers OU's
        # against Control Tower's cap, as percentages; alerts.tf alarms at 80%
        Sid      = "PlatformCapacityMetrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "gerp/platform"
          }
        }
      },
    ]
  })
}

module "bill_customer" {
  source = "../../modules/terraform/lambda"

  name            = "tower-bill-customer"
  role            = aws_iam_role.bill_customer.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/bill_customer.zip"
  src_dir         = "prod/tower/lambdas/bill_customer"
  timeout         = 300
  env_vars = {
    CUSTOMERS_TABLE         = "${local.stack_prefix}-customers"
    PRIORS_TABLE            = "${local.stack_prefix}-priors"
    TOWER_PROVISIONING_ROLE = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
    CUSTOMERS_OU_ID         = data.terraform_remote_state.management.outputs.customers_ou_id # counted against Control Tower's 1,000 per OU
    SELLER_GERP             = var.seller_gerp
    SELLER_STORAGE_BUCKET   = var.seller_storage_bucket
    POST_JOURNAL_ENTRY_FN   = "gerp-accounting-${var.seller_gerp}-post_journal_entry"
    CREATE_INVOICE_FN       = "gerp-invoicing-${var.seller_gerp}-manage_invoice"
    ISSUE_INVOICE_FN        = "gerp-invoicing-${var.seller_gerp}-issue_invoice"
    GET_INVOICES_FN         = "gerp-invoicing-${var.seller_gerp}-manage_invoice"
    # the shared serving layer — not gerps, but their cost is the platform's cost
    PLATFORM_ACCOUNTS = jsonencode({
      operator = data.aws_caller_identity.current.account_id
      # management has no account-id output; the role arn it does export carries it
      management = split(":", data.terraform_remote_state.management.outputs.tower_provisioning_role_arn)[4]
    })
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.bill_customer
  to   = module.bill_customer.aws_lambda_function.this
}

###############################################
# close_account lambda
#
# The end of a closure: fifteen days after the build exported and destroyed a gerp, close its AWS
# account. `organizations:CloseAccount` is management-only, so this assumes TowerProvisioning for
# that one call, the way provision_customer does for Service Catalog. Invoked by the operator gerp's
# `closure/close.py` through gerp-closure-requester (prod/platform/operator).
###############################################

resource "aws_iam_role" "close_account" {
  name = "tower-close-account"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "close_account" {
  name = "tower-close-account"
  role = aws_iam_role.close_account.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AssumeTowerProvisioning"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
      },
      {
        # the gerp's spoke edge goes before its account, through its hub's door
        Sid      = "HubDoor"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:*:*:function:${local.stack_prefix}-hub-manage-edges"
        Condition = {
          StringEquals = { "aws:ResourceOrgID" = local.org_ids }
        }
      },
      {
        Sid      = "ReadAndMarkCustomers"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Sid      = "DeleteDirectoryRow"
        Effect   = "Allow"
        Action   = "dynamodb:DeleteItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-directory"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "close_account" {
  source = "../../modules/terraform/lambda"

  name            = "tower-close-account"
  role            = aws_iam_role.close_account.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/close_account.zip"
  src_dir         = "prod/tower/lambdas/close_account"
  timeout         = 60
  env_vars = {
    STACK_PREFIX            = local.stack_prefix
    CUSTOMERS_TABLE         = "${local.stack_prefix}-customers"
    DIRECTORY_TABLE         = "${local.stack_prefix}-directory"
    TOWER_PROVISIONING_ROLE = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
    HUBS                    = jsonencode({ for k, v in local.config.HUBS : k => v.account }) # region → account; the arns follow
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.close_account
  to   = module.close_account.aws_lambda_function.this
}

###############################################
# update_owner_email lambda
#
# An owner's changed login reaches the Identity Center user Account Factory made from it (one
# UpdateUser, management-only, so this assumes TowerProvisioning like close_account), each owned
# gerp's tenant blob in its own account (OperatorOrchestration — incidents and the agent-mailbox
# verify read owner_email off it), and the gerp-customers row. Invoked by the gerp-cloud BFF's
# email sync, same account.
###############################################

resource "aws_iam_role" "update_owner_email" {
  name = "tower-update-owner-email"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "update_owner_email" {
  name = "tower-update-owner-email"
  role = aws_iam_role.update_owner_email.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AssumeTowerProvisioning"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
      },
      {
        Sid      = "AssumeIntoCustomerSubAccounts"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/OperatorOrchestration"
        Condition = {
          StringEquals = {
            "aws:ResourceOrgID" = local.org_ids
          }
        }
      },
      {
        Sid      = "ReadAndMarkCustomers"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Sid      = "DeleteDirectoryRow"
        Effect   = "Allow"
        Action   = "dynamodb:DeleteItem"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-directory"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "update_owner_email" {
  source = "../../modules/terraform/lambda"

  name            = "tower-update-owner-email"
  role            = aws_iam_role.update_owner_email.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/update_owner_email.zip"
  src_dir         = "prod/tower/lambdas/update_owner_email"
  timeout         = 60
  env_vars = {
    CUSTOMERS_TABLE         = "${local.stack_prefix}-customers"
    TOWER_PROVISIONING_ROLE = data.terraform_remote_state.management.outputs.tower_provisioning_role_arn
    IDENTITY_STORE_ID       = data.terraform_remote_state.management.outputs.identity_store_id
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.update_owner_email
  to   = module.update_owner_email.aws_lambda_function.this
}

resource "aws_cloudwatch_event_rule" "bill_customer_daily" {
  name        = "tower-bill-customer-daily"
  description = "Poll AWS for last month's per-gerp invoices and bill at 1.2x. No-op until issued."
  # 09:00 UTC from the 2nd. AWS issues in the first days of the following month;
  # which day it actually lands is worth watching before this gets narrowed.
  schedule_expression = "cron(0 9 2-14 * ? *)"
}

resource "aws_cloudwatch_event_target" "bill_customer_daily" {
  rule = aws_cloudwatch_event_rule.bill_customer_daily.name
  arn  = module.bill_customer.arn
}

# the edges run: every day, not the billing days — every hub's edges against the directory, and
# the matcher the peer edges rely on

resource "aws_lambda_permission" "bill_customer_events" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowDailyBillingRun"
  action              = "lambda:InvokeFunction"
  function_name       = module.bill_customer.name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.bill_customer_daily.arn
}

###############################################
# update_business_info lambda
#
# A gerp's edited business profile — label, legal, public — reaches its tenant blob in its own
# account (OperatorOrchestration; the agent, the mailbox and the documents it issues read the
# firm's identity off it). The gerp-cloud BFF writes the row and invokes this, same account.
###############################################

resource "aws_iam_role" "update_business_info" {
  name = "tower-update-business-info"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "update_business_info" {
  name = "tower-update-business-info"
  role = aws_iam_role.update_business_info.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AssumeIntoCustomerSubAccounts"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/OperatorOrchestration"
        Condition = {
          StringEquals = {
            "aws:ResourceOrgID" = local.org_ids
          }
        }
      },
      {
        Sid      = "ReadCustomers"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "update_business_info" {
  source = "../../modules/terraform/lambda"

  name            = "tower-update-business-info"
  role            = aws_iam_role.update_business_info.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = "prod/tower/lambdas/update_business_info.zip"
  src_dir         = "prod/tower/lambdas/update_business_info"
  timeout         = 60
  env_vars = {
    CUSTOMERS_TABLE = "${local.stack_prefix}-customers"
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.update_business_info
  to   = module.update_business_info.aws_lambda_function.this
}

