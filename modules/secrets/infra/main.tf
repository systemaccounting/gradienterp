# secrets — the gerp's vault. One `manage_secret` lambda: put (a SecureString under
# /gradienterp/customers/<id>/secrets/<name>), list (names, never values), delete. The agent's
# in-chat `collect_secret` form is the way a value comes in: the chat lambda invokes this lambda
# directly as the form's sink (tagged `agent_frame_sink`) with op put, so no value passes
# through the agent; the same lambda is the agent's `manage_secret` tool on the gateway for
# list and delete, and refuses put from there. Consumers read by name. See AGENTS.md.


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
  description = "Logical tenant identifier; per-tenant resource-name suffix + SSM secret path key."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

locals {
  prefix = "${var.stack_prefix}-secrets-${replace(var.gerp_id, "_", "-")}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── iam ───

resource "aws_iam_role" "lambda" {
  name = "${local.prefix}-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.prefix}-lambda"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # this customer's two secret subtrees: the vault (read by name by the one tool that
        # consumes each) and the cmd tool's env path (exported into agent-written scripts —
        # modules/cmd). Put, delete, and DescribeParameters for the names; never GetParameter —
        # the vault lambda reads no value back (consumers do that).
        Effect = "Allow"
        Action = ["ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/secrets/*",
          "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/automation/env/*",
        ]
      },
      {
        # DescribeParameters takes no resource; the path filter in the call keeps it to this
        # customer's subtrees
        Effect   = "Allow"
        Action   = "ssm:DescribeParameters"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambda ───

module "manage_secret" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-manage_secret"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/secrets/lambdas/manage_secret.zip"
  src_dir         = "modules/secrets/lambdas/manage_secret"
  gerp_id         = var.gerp_id
  timeout         = 10
  env_vars = {
    CUSTOMER_ID = var.gerp_id
  }
  log_retention_days = var.log_retention_days
  tags               = { agent_frame_sink = "true" } # the chat lambda's discovery tag for the collect_secret form's sink
}

# ─── agent gateway registration: list and delete for the agent; put is refused from there ───

variable "register_with_agent" {
  description = "Register manage_secret as a tool on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "gateway_id" {
  description = "The agent's gateway this module registers its tool on — module.agent.gateway_id."
  type        = string
  default     = ""
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes tools as — module.agent.gateway_role_arn; the principal on the invoke permission."
  type        = string
  default     = ""
}

locals {
  # keyed on the schema file, never on the gateway id: on a fresh account the id is known only
  # after apply, and a for_each cannot take an unknown key
  tool_schemas = {
    for k in ["manage_secret"] :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent
  }
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_schemas

  gateway_identifier = var.gateway_id
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = replace(each.key, "_", "-")
  description = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.manage_secret.arn

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
                  type        = prop.value.type
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

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.manage_secret.name
  principal           = var.gateway_role_arn

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── outputs ───

output "manage_secret_fn_name" {
  value = module.manage_secret.name
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}
