###############################################
# prod/optimizer — the hub-and-spoke economic optimizer (operator account).
#
# Thesis in ../README.md; AWS build plan in ../TODO.md. Externally a matchmaker;
# internally the operator-account hub agent that coordinates openly-operated firms.
#
# Applies INTO the operator account (185369506315), like prod/api_openlyoperated/.
# Run under AWS_PROFILE=default — see prod/AGENTS.md §local apply.
#
# SCAFFOLD: the wiring below (config, operator remote state, region) is real; no
# resources yet. `terraform apply` is a clean no-op until the first slice lands.
###############################################

locals {
  config              = jsondecode(file("${path.module}/../../../config.json"))
  stack_prefix        = local.config.STACK_PREFIX         # "gerp"
  operator_account_id = local.config.OPERATOR_ACCOUNT_ID  # "185369506315"
  prefix              = "${local.stack_prefix}-optimizer" # gerp-optimizer-*
  # every org-scoped trust condition (aws:PrincipalOrgID) reads this list: the organization this
  # stack runs in plus the ones admitted in config.json ORG_IDS
  org_ids = concat([data.aws_organizations_organization.this.id], local.config.ORG_IDS)
}

data "aws_region" "current" {}
data "aws_organizations_organization" "this" {}

# ── operator singletons ──
# The hub reaches spokes over A2A and rides the shared bus for async coordination;
# customers_table resolves gerp_id -> aws_account_id. The public profile data the hub
# reads is materialized by prod/api_openlyoperated/ — pull that remote state too when
# the read path lands.
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

locals {
  customers_table         = data.terraform_remote_state.operator.outputs.customers_table
  profiles_table          = data.terraform_remote_state.operator.outputs.profiles_table
  profiles_stream_arn     = data.terraform_remote_state.operator.outputs.profiles_table_stream_arn
  profile_index_table     = data.terraform_remote_state.operator.outputs.profile_index_table
  profile_index_table_arn = data.terraform_remote_state.operator.outputs.profile_index_table_arn
  # profiles table ARN — constructed (avoids an operator re-apply just to add an output).
  profiles_table_arn  = "arn:aws:dynamodb:${data.aws_region.current.region}:${local.operator_account_id}:table/${local.profiles_table}"
  customers_table_arn = "arn:aws:dynamodb:${data.aws_region.current.region}:${local.operator_account_id}:table/${local.customers_table}"
}

###############################################
# reindex — the profile index writer.
#
# gerp-profiles stream ─▶ reindex lambda ─▶ gerp-profile-index (in sync)
#
# Every profile write (BFF person form, provisioning business rows) flows through the stream, so the
# inverted index self-heals without any writer implementing indexing. INSERT/MODIFY recompute the
# profile's match-key rows; REMOVE clears them. Which fields are match-keys is read from the bundled
# profile_fields.json (role == "match-key"), so a new dimension is a schema edit, not a code change.
# The reader (find_profiles) is wired with the hub agent — see ../TODO.md.
###############################################

# Bundle: the handler + shared _helpers + the profile schema (_helpers reads it beside itself).
data "archive_file" "reindex" {
  type        = "zip"
  output_path = "${path.module}/.build/reindex.zip"

  source {
    content  = file("${path.module}/../lambdas/reindex/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/../lambdas/_helpers.py")
    filename = "_helpers.py"
  }
  # _helpers.py imports `aws` (modules/aws/aws.py) — the local/AWS client factory. This archive is a
  # hand-listed manifest, unlike scripts/deploy.py which walks the import graph, so a shared lib
  # added to an import here has to be added here too or the function ImportErrors at cold start.
  source {
    content  = file("${path.module}/../../../modules/aws/aws.py")
    filename = "aws.py"
  }
  source {
    content  = file("${path.module}/../../../modules/schemas/data/profile_fields.json")
    filename = "profile_fields.json"
  }
}

resource "aws_iam_role" "reindex" {
  name = "${local.prefix}-reindex"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "reindex" {
  name = "${local.prefix}-reindex"
  role = aws_iam_role.reindex.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Write the index; Query the by-profile GSI to find + drop a profile's stale rows.
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"]
        Resource = [local.profile_index_table_arn, "${local.profile_index_table_arn}/index/*"]
      },
      {
        # Read the gerp-profiles stream (via the event source mapping).
        Effect   = "Allow"
        Action   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
        Resource = local.profiles_stream_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:*"
      },
    ]
  })
}

module "reindex" {
  source = "../../../modules/terraform/lambda"

  name             = "${local.prefix}-reindex"
  role             = aws_iam_role.reindex.arn
  filename         = data.archive_file.reindex.output_path
  source_code_hash = data.archive_file.reindex.output_base64sha256
  src_dir          = "prod/optimizer/lambdas/reindex"
  timeout          = 30
  env_vars = {
    # _helpers builds table handles for both at import; reindex only touches the index.
    PROFILES_TABLE = local.profiles_table
    INDEX_TABLE    = local.profile_index_table
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.reindex
  to   = module.reindex.aws_lambda_function.this
}


resource "aws_lambda_event_source_mapping" "reindex" {
  event_source_arn  = local.profiles_stream_arn
  function_name     = module.reindex.arn
  starting_position = "LATEST"
  batch_size        = 100
}

###############################################
# find_profiles — the registry reader (the hub's yellow-pages lookup).
#
# match=[<dim>#<value>, …] → intersect the inverted index → batch-get the profiles → radius-filter.
# The hub agent invokes this lambda as its `find_profiles` in-process tool. Same bundle pattern as
# reindex (main.py + _helpers); reads gerp-profiles (GetItem) + gerp-profile-index (Query).
###############################################

data "archive_file" "find_profiles" {
  type        = "zip"
  output_path = "${path.module}/.build/find_profiles.zip"

  source {
    content  = file("${path.module}/../lambdas/find_profiles/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/../lambdas/_helpers.py")
    filename = "_helpers.py"
  }
  # _helpers.py imports `aws` (modules/aws/aws.py) — the local/AWS client factory. This archive is a
  # hand-listed manifest, unlike scripts/deploy.py which walks the import graph, so a shared lib
  # added to an import here has to be added here too or the function ImportErrors at cold start.
  source {
    content  = file("${path.module}/../../../modules/aws/aws.py")
    filename = "aws.py"
  }
}

resource "aws_iam_role" "find_profiles" {
  name = "${local.prefix}-find-profiles"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "find_profiles" {
  name = "${local.prefix}-find-profiles"
  role = aws_iam_role.find_profiles.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Query the index for each match key; GetItem the matched profiles.
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem"]
        Resource = [local.profile_index_table_arn, local.profiles_table_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:*"
      },
    ]
  })
}

module "find_profiles" {
  source = "../../../modules/terraform/lambda"

  name             = "${local.prefix}-find-profiles"
  role             = aws_iam_role.find_profiles.arn
  filename         = data.archive_file.find_profiles.output_path
  source_code_hash = data.archive_file.find_profiles.output_base64sha256
  src_dir          = "prod/optimizer/lambdas/find_profiles"
  timeout          = 30
  env_vars = {
    PROFILES_TABLE = local.profiles_table
    INDEX_TABLE    = local.profile_index_table
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.find_profiles
  to   = module.find_profiles.aws_lambda_function.this
}


output "find_profiles_fn" {
  description = "The registry reader lambda — the hub agent's find_profiles tool invokes this."
  value       = module.find_profiles.name
}

###############################################
# NEXT — on-demand hub agent (the a2a matchmaker). See hub.tf.
#   the hub AgentCore runtime (operator account, broker mode) reuses the shared agent image,
#   invokes find_profiles (above) to pick spokes, resolves each spoke's runtime_endpoint_arn off
#   gerp-customers, and InvokeAgentRuntime's it ({"prompt"} today, A2A once the runtime switch lands —
#   modules/agent/TODO.md). Trust: the hub role gets org-scoped InvokeAgentRuntime; each spoke's
#   resource policy grants it (per_customer). TODO §the hub agent — talking to N spokes.
###############################################
