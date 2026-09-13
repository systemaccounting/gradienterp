# The export tool's registration on the agent gateway — separated from the lambda it points at
# because the two have different lifetimes.
#
# `prod/init_customer` owns the lambda: it must survive closure, since the download credential is
# one hour and the window is fifteen days, so something has to be able to issue a fresh one after
# the instance is gone. The gateway is `prod/per_customer`'s and dies with it. So the tool exists
# exactly as long as the agent does, and the lambda outlives both.

variable "gerp_id" {
  description = "Logical instance identifier — the SSM path the gateway ids are published under."
  type        = string
}

variable "lambda_arn" {
  description = "ARN of the export lambda in prod/init_customer. Derived by the caller rather than passed through state; the name is a function of (stack_prefix, gerp_id, account)."
  type        = string
}

variable "gateway_id" {
  description = "The agent's gateway the export tool registers on — module.agent.gateway_id, through the graph."
  type        = string
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes the tool as — module.agent.gateway_role_arn."
  type        = string
}

variable "lambda_function_name" {
  description = "Name of the same function — the gateway's invoke grant is written against the name."
  type        = string
}

# ─── agent gateway registration ───

locals {
  schema = jsondecode(file("${path.module}/../lambdas/export_gerp/schema.json"))
}

resource "aws_bedrockagentcore_gateway_target" "tool" {

  gateway_identifier = var.gateway_id
  lifecycle {
    ignore_changes = [gateway_identifier] # immutable per customer; pin so an agent-image bump doesn't force-replace
  }
  name        = "export-gerp" # AgentCore target name: hyphens, no underscores
  description = local.schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.lambda_arn

        tool_schema {
          inline_payload {
            name        = "export_gerp" # agent-facing tool name stays snake_case
            description = local.schema.description
            input_schema {
              type        = local.schema.type
              description = local.schema.description

              dynamic "property" {
                iterator = prop
                for_each = try(local.schema.properties, {})
                content {
                  name        = prop.key
                  type        = try(prop.value.type, "object")
                  description = try(prop.value.description, "")
                  required    = contains(try(local.schema.required, []), prop.key)
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

resource "aws_lambda_permission" "gateway" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = var.lambda_function_name
  principal           = var.gateway_role_arn
}

