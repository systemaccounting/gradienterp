variable "gerp_id" {
  description = "logical identifier for the tenant this contacts stack belongs to. used as the per-tenant resource-name suffix AND as the SSM lookup key for tenant metadata at /gradienterp/customers/<gerp_id>."
  type        = string
}


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

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "schema_table_name" {
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). manage_contacts queries it at cold start to load contact_fields validation."
  type        = string
}

variable "op_event_bus_arn" {
  description = "ARN of the operator's shared EventBridge bus. Reserved for future stream → EB wiring; not consumed by current lambdas."
  type        = string
  default     = "arn:aws:events:us-east-1:185369506315:event-bus/gerp-events"
}

variable "register_with_agent" {
  description = "Register contacts lambdas as MCP tools on the customer's agent gateway. Requires the agent module to have applied first (writes /gradienterp/customers/<id>/agent/* SSM params)."
  type        = bool
  default     = true
}

# ─── tenant metadata (SSM lookup, operator-seeded at onboard time) ───

data "aws_ssm_parameter" "customer" {
  name = "/gradienterp/customers/${var.gerp_id}"
}

locals {
  customer = jsondecode(data.aws_ssm_parameter.customer.value)
  prefix   = "${var.stack_prefix}-contacts-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb (contacts) ───
#
# Hash key contact_id. Schemaless attributes; the per-customer contact_fields
# registry is the contract (modules/schemas/data/contact_fields.json baseline,
# extensible per-customer at runtime). Streams enabled for future audit /
# downstream fan-out (a pipe to EventBridge can be added without redeploying
# the table).

resource "aws_dynamodb_table" "contacts" {
  name         = local.prefix
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "contact_id"

  attribute {
    name = "contact_id"
    type = "S"
  }

  attribute {
    name = "gerp_profile_id"
    type = "S"
  }

  # Sparse GSI: only contacts linked to a platform account (account_id present) are
  # indexed — the chat lambda resolves a caller's role by Query on their PUBLIC PROFILE id, not a
  # table Scan. ALL projection so the query returns the relationship flags.
  #
  # Keyed on `gerp_profile_id`, not `account_id`. They carry the same value for a person (a
  # profile is keyed by their account sub) but the profile id is strictly more general — an
  # organization contact has one too (its gerp_id) and has no Cognito subject at all. Registering
  # `account_id` would be a second name for one value, and `contact_fields.json` never carried it,
  # so `validate_contact` rejected it and no write path could populate it: the index had zero rows
  # and `contactRole()` returned null for every non-owner. Re-pointing was free.
  global_secondary_index {
    name            = "account-index"
    hash_key        = "gerp_profile_id"
    projection_type = "ALL"
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
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
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        # The index arn is NOT covered by the table arn — a policy granting Query on a table does
        # not grant it on that table's GSIs. `account-index` is how a public profile resolves to a
        # contact (an invoice's created_by is a Cognito subject, not a name).
        Resource = [
          aws_dynamodb_table.contacts.arn,
          "${aws_dynamodb_table.contacts.arn}/index/*",
        ]
      },
      {
        # cold-start validator reads the customer's contact_fields registry
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
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

# ─── lambdas ───

locals {
  functions = toset([
    "manage_contacts",
  ])

  env_vars = {
    CONTACTS_TABLE = aws_dynamodb_table.contacts.name
    SCHEMA_TABLE   = var.schema_table_name
    CUSTOMER_ID    = var.gerp_id
  }
}

# Each zip includes main.py + the shared _helpers.py (cold-start registry loader,
# applicable-bucket validator, local-mode jsonl shims).
module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/contacts/lambdas/${each.key}.zip"
  src_dir            = "modules/contacts/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_contacts"]
  to   = module.fn["manage_contacts"].aws_lambda_function.this
}


# ─── agent gateway registration ───

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
                  type        = prop.value.type
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.required, []), prop.key)

                  dynamic "items" {
                    for_each = prop.value.type == "array" ? [prop.value.items] : []
                    content {
                      type        = items.value.type
                      description = try(items.value.description, "")
                      dynamic "property" {
                        iterator = innerprop
                        for_each = try(items.value.properties, {})
                        content {
                          name        = innerprop.key
                          type        = innerprop.value.type
                          description = try(innerprop.value.description, "")
                          required    = contains(try(items.value.required, []), innerprop.key)
                        }
                      }
                    }
                  }

                  dynamic "property" {
                    iterator = innerprop
                    for_each = prop.value.type == "object" ? try(prop.value.properties, {}) : {}
                    content {
                      name        = innerprop.key
                      type        = innerprop.value.type
                      description = try(innerprop.value.description, "")
                      required    = contains(try(prop.value.required, []), innerprop.key)
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

output "contacts_table" {
  description = "DDB contacts table name. Hash key contact_id; streams enabled."
  value       = aws_dynamodb_table.contacts.name
}

output "contacts_stream_arn" {
  description = "DDB Stream ARN for the contacts table. Consume via an EventBridge Pipe when downstream fan-out is needed."
  value       = aws_dynamodb_table.contacts.stream_arn
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "lambda_arns" {
  description = "map of lambda logical name → ARN."
  value       = { for k, fn in module.fn : k => fn.arn }
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
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
