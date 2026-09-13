# labor pay_run — a worker's pay run.
#
# An agent tool (gateway target) + direct/cron-invokable lambda. For a worker and
# pay period it sums that period's accrued WAGES_PAYABLE (the close-handler's
# credits, by dimensions.worker_id) from accounting's ledger partition, runs the
# rule INSTANCES attached to PAY_RUN#<contact_id> (effective as of the period), and
# posts the withholding entry that reclassifies the wage liability into the tax
# payables + books the employer taxes.
# Settling the remaining net to cash stays treasury — see modules/rules/AGENTS.md.
#
# Bundles the engine + the instance store + every rule library an instance can name
# (general_rules, payroll_rules); same gateway-registration pattern as the
# CRUD tools in main.tf (this one tool, drawn statically since its schema is two flat
# string props).

locals {
  pay_run_name   = "${local.prefix}-pay-run"
  pay_run_schema = jsondecode(file("${path.module}/../lambdas/pay_run/schema.json"))
}

# ─── package ───

# ─── iam ───

resource "aws_iam_role" "pay_run" {
  name = local.pay_run_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "pay_run" {
  name = local.pay_run_name
  role = aws_iam_role.pay_run.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # total the period's WAGES_PAYABLE credits from accounting's ledger
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.ledger_table_name}"
      },
      {
        # the worker row — its home location stamps the withholding entry's dims
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = aws_dynamodb_table.worker.arn
      },
      {
        # the rule instances attached to PAY_RUN#<contact_id> — the withholdings + employer
        # taxes this worker owes. The attachment IS the dispatch; there is no rule set.
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}"
      },
      {
        # the GENERAL platform rows — the bracket tables in force for the period. Read-only:
        # the gerp never authors a platform value, which is why it can't forge one.
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.rules_params_table_name}"
      },
      {
        # post the withholding entry
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
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

# ─── lambda ───

module "pay_run" {
  source = "../../terraform/lambda"

  name            = local.pay_run_name
  role            = aws_iam_role.pay_run.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/labor/lambdas/pay_run.zip"
  src_dir         = "modules/labor/lambdas/pay_run"
  gerp_id         = var.gerp_id
  timeout         = 30
  env_vars = {
    LEDGER_TABLE          = var.ledger_table_name
    WORKER_TABLE          = aws_dynamodb_table.worker.name
    RULE_INSTANCES_TABLE  = var.rule_instances_table_name
    RULES_PARAMS_TABLE    = var.rules_params_table_name
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    CUSTOMER_ID           = var.gerp_id
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.pay_run
  to   = module.pay_run.aws_lambda_function.this
}


# ─── agent gateway registration ───

resource "aws_bedrockagentcore_gateway_target" "pay_run" {
  count = var.register_with_agent ? 1 : 0

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = "pay-run"
  description = local.pay_run_schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.pay_run.arn

        tool_schema {
          inline_payload {
            name        = "pay_run"
            description = local.pay_run_schema.description
            input_schema {
              type        = "object"
              description = local.pay_run_schema.description

              property {
                name        = "worker_id"
                type        = "string"
                description = local.pay_run_schema.properties.worker_id.description
                required    = true
              }
              property {
                name        = "period"
                type        = "string"
                description = local.pay_run_schema.properties.period.description
                required    = false
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

resource "aws_lambda_permission" "pay_run_gateway" {
  count = var.register_with_agent ? 1 : 0

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.pay_run.name
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

output "pay_run_fn_name" {
  value = module.pay_run.name
}

output "pay_run_fn_arn" {
  value = module.pay_run.arn
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.
