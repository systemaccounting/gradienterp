locals {
  prefix = "${var.stack_prefix}-rules-${replace(var.gerp_id, "_", "-")}"
}

# ─── rules-params table ───
#
# The shared config substrate the rule engine reads. One table for every domain's rule
# params:
#   pk = a contact_id (a worker/entity) | "GENERAL" (platform / employer-wide config)
#   sk = "_rules"            → the entity's rule set ({param: {names: [...]}})
#        "<rule>#<eff_from>" → a rule's params, append-only + effective-dated
# A worker's rows override GENERAL rows by key at resolve time; trigger lambdas (pay_run,
# …) read it instead of carrying baked defaults / per-domain param tables.

resource "aws_dynamodb_table" "rules_params" {
  name         = "${local.prefix}-params"
  tags         = { "gerp:layer" = "config" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

# rule instances — how a module USES a rule. A row here is the whole configuration: which general rule,
# what params, and what it fires on. The attachment IS the dispatch; nothing is taxed unless an
# instance points at it, so there is no "off" flag and no rate-of-zero.
#   pk = the subject an instance hangs off: INVOICE_LINE#<inventory key>, or INVOICE_LINE#* for every item
#   sk = <n>#<instance> — n orders the cascade when several match (a city tax on a state tax)
resource "aws_dynamodb_table" "rule_instances" {
  name         = "${local.prefix}-instances"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

# ─── iam ───

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

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
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.rules_params.arn
      },
      {
        # add_rule writes an instance; get_rules lists them (Scan — the table is config-sized);
        # delete_rule is the off switch
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.rule_instances.arn
      },
      {
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

# ─── lambdas (the agent's two rules tools: instances, params) ───

locals {
  functions = toset(["manage_rules", "rule_params"])
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name            = "${local.prefix}-${each.key}"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/rules/lambdas/${each.key}.zip"
  src_dir         = "modules/rules/lambdas/${each.key}"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    RULES_PARAMS_TABLE   = aws_dynamodb_table.rules_params.name
    RULE_INSTANCES_TABLE = aws_dynamodb_table.rule_instances.name
    CUSTOMER_ID          = var.gerp_id
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_rules"]
  to   = module.fn["manage_rules"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["rule_params"]
  to   = module.fn["rule_params"].aws_lambda_function.this
}


# ─── agent gateway registration (same pattern as modules/labor) ───

locals {
  tool_schemas = {
    for k in local.functions :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent && fileexists("${path.module}/../lambdas/${k}/schema.json")
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

  statement_id_prefix = "AllowAgentGatewayInvoke"
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

output "rules_params_table_name" {
  description = "Name of the rules-params DDB table; trigger lambdas (pay_run) read it to resolve rule sets + params."
  value       = aws_dynamodb_table.rules_params.name
}

output "rules_params_table_arn" {
  value = aws_dynamodb_table.rules_params.arn
}

output "rule_instances_table_name" {
  description = "Rule-instance DDB table. A firm uses a rule by writing a row here (add_rule); invoicing queries it by the inventory key of every item it is selling."
  value       = aws_dynamodb_table.rule_instances.name
}

output "rule_instances_table_arn" {
  value = aws_dynamodb_table.rule_instances.arn
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.
