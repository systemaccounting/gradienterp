# ─────────────────────────────────────────────────────────────────────────────
# cmd — the agent's shell with internet, deps it grows itself.
#
# Two lambdas: `cmd` runs agent-drafted scripts (python3.12 shim spawning `sh -e` — the
# deploy pipeline zips files 0644 so a provided.* bootstrap can't carry its exec bit, and
# the shim gets boto3 for the per-invoke SSM env fetch free), `build_layer` starts/polls
# codebuild runs that produce layers and attaches them. The cmd role is the security
# boundary: model-written scripts execute against ITS grants, never the agent's.
#
# tf owns SHAPE only, twice over: lambda code comes from the artifact bucket (deploy.sh
# push), and the cmd lambda's LAYERS belong to build_layer (ignore_changes) — an apply
# never strips what the agent built.
# ─────────────────────────────────────────────────────────────────────────────


variable "log_retention_days" {
  description = "How long each function's log group keeps its events; root-set (prod/per_customer)."
  type        = number
  default     = 90
}

variable "ops_alerts_topic_arn" {
  description = "The operator's ops topic each function's Errors alarm publishes to; empty = no alarm (local)."
  type        = string
  default     = ""
}

variable "gerp_id" {
  type = string
}

variable "stack_prefix" {
  type = string
}

variable "agent_email_address" {
  description = "The gerp's verified SES sender, exported to scripts as $AGENT_ADDRESS. Empty when the agent's email front door is off, in which case a script that tries to send gets an SES error rather than silence."
  type        = string
  default     = ""
}

variable "register_with_agent" {
  type    = bool
  default = true
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  gerp   = replace(var.gerp_id, "_", "-")
  prefix = "${var.stack_prefix}-cmd-${local.gerp}"

  # the filing cabinet — agent-owned bucket, constructed name (a module ref would cycle
  # through this module's depends_on = [module.agent])
  cabinet_bucket = "${var.stack_prefix}-agent-${local.gerp}-uploads-${data.aws_caller_identity.current.account_id}"
  kms_alias      = "alias/agentcore_${replace(var.gerp_id, "-", "_")}_uploads"

  # The firm's own env for its own scripts, shared with modules/automation rather than owned here.
  # A credential for a supplier's API is the firm's credential; which runner reads it — a `.sh`
  # through cmd or a `.py` through automate — is not a property of the credential, and two paths
  # would mean collecting it twice and rotating it once.
  #
  # What each runner may DO stays separate: that is each one's role, not this path.
  env_param_path = "/gradienterp/customers/${var.gerp_id}/automation/env"

  functions = toset(["cmd", "build_layer"])
}

data "aws_kms_alias" "cabinet" {
  name = local.kms_alias
}

# ─── IAM ───

resource "aws_iam_role" "lambda" {
  name = local.prefix
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = local.prefix
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the owner's env — the ONE param path; platform secrets live elsewhere
        Effect   = "Allow"
        Action   = ["ssm:GetParametersByPath", "ssm:GetParameter"]
        Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.env_param_path}*"
      },
      {
        # cabinet prefixes: scripts + buildspecs in, oversize outputs + layer zips out.
        # approved/external/ is the EXTERNAL LANE: written only by approve_automation, so a
        # scheduled cmd pointed there can only run bytes that passed review. scripts/ stays
        # agent-writable for the attended case, where the owner is watching the output.
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "arn:aws:s3:::${local.cabinet_bucket}/scripts/*",
          "arn:aws:s3:::${local.cabinet_bucket}/buildspecs/*",
          "arn:aws:s3:::${local.cabinet_bucket}/layers/*",
          "arn:aws:s3:::${local.cabinet_bucket}/automations/approved/external/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/outputs/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        # build_layer: run the pipeline + attach what it produces
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
        Resource = aws_codebuild_project.layers.arn
      },
      {
        # a failed build returns its log tail — the agent wrote the spec, so it fixes it
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/codebuild/${local.prefix}-layers:*"
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:PublishLayerVersion", "lambda:GetLayerVersion"]
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:layer:${local.prefix}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration"]
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-cmd"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambdas ───


module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name            = "${local.prefix}-${each.key}"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/cmd/lambdas/${each.key}.zip"
  src_dir         = "modules/cmd/lambdas/${each.key}"
  gerp_id         = var.gerp_id
  timeout         = each.key == "cmd" ? 900 : 60
  memory          = each.key == "cmd" ? 1024 : 256
  env_vars = {
    CUSTOMER_ID       = var.gerp_id
    AGENT_ADDRESS     = var.agent_email_address
    CABINET_BUCKET    = local.cabinet_bucket
    ENV_PARAM_PATH    = local.env_param_path
    CODEBUILD_PROJECT = aws_codebuild_project.layers.name
    CMD_FUNCTION      = "${local.prefix}-cmd"
    LAYER_PREFIX      = local.prefix
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["cmd"]
  to   = module.fn["cmd"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["build_layer"]
  to   = module.fn["build_layer"].aws_lambda_function.this
}


# ─── the layer pipeline shell ───

resource "aws_iam_role" "codebuild" {
  name = "${local.prefix}-build"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  name = "${local.prefix}-build"
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/layers/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

resource "aws_codebuild_project" "layers" {
  name         = "${local.prefix}-layers"
  description  = "Builds cmd-tool layers from agent-authored cabinet buildspecs (buildspecOverride at start)."
  service_role = aws_iam_role.codebuild.arn

  source {
    type      = "NO_SOURCE"
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - echo \"no spec passed - build_layer always overrides\"\n"
  }

  artifacts {
    type                = "S3"
    location            = local.cabinet_bucket
    path                = "layers"
    namespace_type      = "BUILD_ID"
    name                = "layer.zip"
    packaging           = "ZIP"
    encryption_disabled = false
  }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    type         = "LINUX_CONTAINER"
  }

  logs_config {
    cloudwatch_logs {
      status     = "ENABLED"
      group_name = aws_cloudwatch_log_group.layers.name
    }
  }
}

# every layer build's log, at the fleet's retention
resource "aws_cloudwatch_log_group" "layers" {
  name              = "/aws/codebuild/${local.prefix}-layers"
  retention_in_days = var.log_retention_days
}

# ─── agent gateway registration ───

locals {
  tool_schemas = {
    for k in local.functions :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent
  }
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_schemas

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = replace(each.key, "_", "-")
  description = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.fn[each.key].arn

        tool_schema {
          inline_payload {
            name        = each.key
            description = each.value.description
            input_schema {
              type        = each.value.type
              description = each.value.description

              dynamic "property" {
                iterator = prop
                for_each = try(each.value.properties, {})
                content {
                  name        = prop.key
                  type        = try(prop.value.type, "object")
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.required, []), prop.key)
                }
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}

resource "aws_lambda_permission" "gateway_invoke" {
  for_each = local.tool_schemas

  statement_id_prefix = "AllowGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn[each.key].name
  principal           = var.gateway_role_arn

  # gateway role arn is immutable per customer — pin it (same rationale as gateway_identifier on
  # the target): an agent-image bump defers this SSM read, and principal is force-new, so without
  # this every gateway-invoke permission would be needlessly delete+recreated.
  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── outputs ───

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

variable "gateway_id" {
  description = "The agent's gateway this module registers its tools on — module.agent.gateway_id. Read at plan as an input, never from SSM: a fresh account has no parameter to read yet."
  type        = string
  default     = ""
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes tools as — module.agent.gateway_role_arn; the principal on each tool lambda's invoke permission."
  type        = string
  default     = ""
}
