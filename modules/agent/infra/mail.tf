# ─────────────────────────────────────────────────────────────────────────────
# outbound mail — the firm sends as ITSELF
#
# A gerp sends through the firm's own mail server, and only that. There is no SES sending in a
# customer account: the customers being written to have never heard of our subdomain, and reaching
# them through SES would need production access with the bounce-rate obligation, complaint
# thresholds, suppression list and reputation exposure that carries — per customer account, since
# each gerp is its own.
#
# Every firm that emails its customers already has a mailbox whose SPF and DKIM are right and whose
# deliverability is already somebody's job. So the sender is `SENDER#<address>` rows in the settings
# table: the FROM-ADDRESS is the key, because that is what varies and what a caller cares about. A
# script says who the mail is from and never names a machine.
#
# That is also the sending constraint. A gerp can only send as an address that has a row, and
# `configure_smtp` writes one only after a test message actually went out. No row, no send — which
# is a real state for a new gerp, and saying so beats sending as an address the firm does not own.
#
# `email.tf` is the other half: SES INBOUND on the per-gerp subdomain, which never sends.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  mail_functions = toset(["send_email", "configure_smtp", "get_send_history"])
  mail_prefix    = "${var.stack_prefix}-mail-${replace(var.gerp_id, "_", "-")}"
  mail_schemas = {
    for f in local.mail_functions :
    f => jsondecode(file("${path.module}/../lambdas/${f}/schema.json"))
  }
  send_log_group = "/aws/lambda/${local.mail_prefix}-send_email"
  settings_table = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
  secret_prefix  = "/gradienterp/customers/${var.gerp_id}/secrets"
}

# ─── one role: read the sender rows, read the credential, write the log ───
#
# No SES, no S3, no other module's tools. What this can do is open a socket to a host the firm
# named and authenticate with a secret the firm stored.

resource "aws_iam_role" "mail" {
  name = "${local.mail_prefix}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "mail" {
  name = "${local.mail_prefix}-lambda"
  role = aws_iam_role.mail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the SENDER# rows. Query for reading, write for configure_smtp — the same table every
        # other GERP#/USER# setting lives on, so this is a prefix rather than a new store.
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${local.settings_table}"
      },
      {
        # the mail password, by name. The agent holds the NAME and never the value; this is the
        # only thing that decrypts it, at send time, and never puts it in an env var.
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter${local.secret_prefix}/*"
      },
      {
        # get_send_history reads what send_email wrote — its own log group, nothing else
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:${local.send_log_group}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}


module "mail" {
  for_each = local.mail_functions
  source   = "../../terraform/lambda"

  name            = "${local.mail_prefix}-${each.key}"
  role            = aws_iam_role.mail.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/agent/lambdas/${each.key}.zip"
  src_dir         = "modules/agent/lambdas/${each.key}"
  gerp_id         = var.gerp_id
  timeout         = each.key == "send_email" ? 900 : 60
  memory          = 512
  env_vars = {
    GERP_ID             = var.gerp_id
    SETTINGS_TABLE      = local.settings_table
    SECRET_PARAM_PREFIX = local.secret_prefix
    SEND_LOG_GROUP      = local.send_log_group
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.mail["send_email"]
  to   = module.mail["send_email"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.mail["configure_smtp"]
  to   = module.mail["configure_smtp"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.mail["get_send_history"]
  to   = module.mail["get_send_history"].aws_lambda_function.this
}


# the group is the lambda module's now (retention is `log_retention_days`); get_send_history
# can only answer as far back as that keeps
moved {
  from = aws_cloudwatch_log_group.send_email
  to   = module.mail["send_email"].aws_cloudwatch_log_group.this
}

# ─── gateway registration ───

resource "aws_bedrockagentcore_gateway_target" "mail" {
  for_each = local.mail_schemas

  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = replace(each.key, "_", "-")
  description        = each.value.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.mail[each.key].arn

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

                  dynamic "items" {
                    for_each = try(prop.value.type, "") == "array" ? [prop.value.items] : []
                    content {
                      type        = items.value.type
                      description = try(items.value.description, "")
                    }
                  }
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

resource "aws_lambda_permission" "mail_gateway" {
  for_each = local.mail_functions

  statement_id_prefix = "AllowGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.mail[each.key].name
  principal           = aws_iam_role.agent_execution.arn # the gateway invokes under its own role

  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window where
  # the principal is unauthorised — a tool call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
}
