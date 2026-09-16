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
  description = "logical identifier for the tenant this inventory stack belongs to. used as the per-tenant resource-name suffix. inventory has no tenant-metadata needs of its own, so no SSM lookup here."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json by the composition root; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "post_journal_entry_fn_arn" {
  description = "arn of the accounting module's post_journal_entry lambda"
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "name of the accounting module's post_journal_entry lambda"
  type        = string
}

variable "schema_table_name" {
  description = "Per-customer registry DDB table name (created by modules/schemas/infra). manage_stock queries it at cold start for field-name validation against the item_fields registry."
  type        = string
}

variable "rule_instances_table_name" {
  description = "Rule-instances DDB table (modules/rules/infra). manage_stock runs the instances keyed `ITEM_CREATED#*` — the catalog rules that decide an item's defaults (e.g. which account its revenue credits). A firm re-points them by writing a row; with no row the canonical instance runs and the rule's own defaults apply."
  type        = string
}

variable "register_with_agent" {
  description = "Register inventory lambdas as MCP tools on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "settings_table_name" {
  description = "Settings config table name (modules/settings/infra). The oob_inventory reader reads GERP#openly_operated from it to gate the public read."
  type        = string
}

variable "settings_table_arn" {
  description = "Settings config table ARN — the dynamodb:GetItem grant for the oob_inventory flag read + the oob_catalog descriptor row."
  type        = string
}

variable "server_api_id" {
  description = "Per-customer HTTP API id from modules/server/infra. Inventory attaches its GET /oob/inventory read route here."
  type        = string
}

variable "server_api_execution_arn" {
  description = "Per-customer HTTP API execution ARN root. Source ARN for the API-gateway invoke permission on oob_inventory."
  type        = string
}

locals {
  prefix = "${var.stack_prefix}-inventory-${replace(var.gerp_id, "_", "-")}"
}

# ─── dynamodb ───

resource "aws_dynamodb_table" "items" {
  name         = "${local.prefix}-items"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "item_id"

  attribute {
    name = "item_id"
    type = "S"
  }
}

# The movement log — append-only physical-i/o rows (`Δ · item · source · when`); on-hand /
# availability are folds over it, never a stored scalar (the item row's `quantity` is a
# materialized cache). pk=item_id groups an item's movements; sk=mv_sk = "{effective-time}#{id}"
# orders them and makes a re-put idempotent. See modules/inventory/movements.py.
resource "aws_dynamodb_table" "movements" {
  name         = "${local.prefix}-movements"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "item_id"
  range_key    = "mv_sk"

  attribute {
    name = "item_id"
    type = "S"
  }
  attribute {
    name = "mv_sk"
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
        Effect   = "Allow",
        Action   = ["dynamodb:GetItem"],
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
        # `clock` reads GERP#timezone at cold start — an availability RRULE is expanded on the
        # business's calendar, so the zone has to be the one the owner can actually edit
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Scan",
        ]
        Resource = aws_dynamodb_table.items.arn
      },
      {
        # manage_stock's move op appends to the movement log; a fold read Query's it back.
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.movements.arn
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.schema_table_name}"
      },
      {
        # manage_stock runs the rule instances keyed `ITEM_CREATED#*` on create_item — the item's catalog defaults
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = "arn:aws:dynamodb:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.rule_instances_table_name}"
      },
      {
        # oob_inventory reads GERP#openly_operated from the settings config table to gate the public read.
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = var.settings_table_arn
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.post_journal_entry_fn_arn
      },
      {
        # the reorder loop proposing a PO with no turn (auto_order)
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${local.agreements_request_fn}"
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
  functions = {
    manage_stock = "manage_stock"
    reserve      = "reserve"
  }

  # module-root shared libs each lambda bundles at its zip root (path relative to ../).
  # manage_stock's move op appends to the log — @point only, so no availability.py and no vendored
  # portion (the capacity fold's lazy import). reserve does the interval math, so it takes
  # availability.py + capacity.py + the vendor tree below.
  extra_root_sources = {
    manage_stock = ["movements.py", "stock_rules.py", "catalog_rules.py"]
    reserve      = ["movements.py", "availability.py", "capacity.py"]
  }

  # lambdas that need vendored `portion` (availability.py puts ./vendor on sys.path at import).
  needs_vendor = ["reserve"]

  # lambdas that run rules. manage_stock runs the instances keyed `ITEM_CREATED#*` on create_item
  # (an item's catalog defaults) and the STOCK_SOLD#-keyed ones on a SOLD move (a `produce_on_sale`
  # row backflushes a made-to-order composite) — named rows the firm re-points, not logic buried in
  # the lambda. The engine and the instance store live in modules/rules, bundled from outside.
  needs_rules = ["manage_stock"]

  env_vars = {
    ITEMS_TABLE = aws_dynamodb_table.items.name
    # The business's clock (modules/clock). BOTH are needed and the table is the one that matters:
    # `clock` resolves the owner-editable `GERP#timezone` row FIRST and only falls back to the env,
    # so without SETTINGS_TABLE the zone silently reverts to this var's default of UTC — and an
    # availability rule written as `BYHOUR=7` then opens at 7am UTC, which is midnight where the
    # business actually is. The windows still look well-formed, which is why it survived a demo.
    GERP_TIMEZONE         = var.timezone
    SETTINGS_TABLE        = "${var.stack_prefix}-settings-${replace(var.gerp_id, "_", "-")}"
    MOVEMENTS_TABLE       = aws_dynamodb_table.movements.name
    MOVEMENTS_TABLE       = aws_dynamodb_table.movements.name
    SCHEMA_TABLE          = var.schema_table_name
    RULE_INSTANCES_TABLE  = var.rule_instances_table_name # the catalog rules keyed `ITEM_CREATED#*`
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    CUSTOMER_ID           = var.gerp_id
    INTERNAL_BUS_NAME     = var.internal_bus_name # a record_metric row announces a product event here (modules/metrics)
    # an `auto_order` row on REORDER#<item> turns a gap into a PO through the shared agreements
    # request service, by CONSTRUCTED name (the inbox-router convention): agreements reads this
    # module's items table, so a module ref here would cycle
    AGREEMENTS_REQUEST_FN = local.agreements_request_fn
  }
  agreements_request_fn = "${var.stack_prefix}-agreements-${replace(var.gerp_id, "_", "-")}-request"
}

# every function goes through modules/terraform/lambda: the artifact pin, the fleet tag, the
# owned log group with its retention, the Errors alarm. References read module.fn["x"].arn / .name
module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/inventory/lambdas/${each.key}.zip"
  src_dir            = "modules/inventory/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["manage_stock"]
  to   = module.fn["manage_stock"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["reserve"]
  to   = module.fn["reserve"].aws_lambda_function.this
}

# ─── agent gateway registration ───
#
# Same pattern as modules/accounting/infra: each lambda with a sibling
# schema.json registers as an MCP tool against the agent's gateway, discovered
# via SSM. Permission is resource-based (aws_lambda_permission), keyed off the
# gateway's role ARN (also SSM).

locals {
  tool_schemas = {
    for k in keys(local.functions) :
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

                  dynamic "items" {
                    for_each = try(prop.value.type, "") == "array" ? [prop.value.items] : []
                    content {
                      type        = try(items.value.type, "object")
                      description = try(items.value.description, "")
                      dynamic "property" {
                        iterator = innerprop
                        for_each = try(items.value.properties, {})
                        content {
                          name        = innerprop.key
                          type        = try(innerprop.value.type, "object")
                          description = try(innerprop.value.description, "")
                          required    = contains(try(items.value.required, []), innerprop.key)
                        }
                      }
                    }
                  }

                  dynamic "property" {
                    iterator = innerprop
                    for_each = try(prop.value.type, "") == "object" ? try(prop.value.properties, {}) : {}
                    content {
                      name        = innerprop.key
                      type        = try(innerprop.value.type, "object")
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

output "items_table" {
  value = aws_dynamodb_table.items.name
}

output "movements_table" {
  value = aws_dynamodb_table.movements.name
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "lambda_arns" {
  description = "map of lambda logical name → ARN. agent/infra consumes this as `inventory_lambda_arns`."
  value       = { for k, fn in module.fn : k => fn.arn }
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.
variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "timezone" {
  description = "The gerp's IANA timezone, surfaced as GERP_TIMEZONE for `clock.py` — a seasonal par rule reads month membership locally. Default UTC = the behaviour before the clock module existed."
  type        = string
  default     = "UTC"
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

variable "internal_bus_name" {
  description = "The firm's own event bus (modules/events/infra). A record_metric row on this module's callsites announces a product event here."
  type        = string
}

variable "internal_bus_arn" {
  description = "The same bus, for the events:PutEvents grant."
  type        = string
}

# a firm's record_metric row on this module's callsites announces on the firm's OWN bus
# (modules/metrics)
resource "aws_iam_role_policy" "internal-bus" {
  name = "${local.prefix}-internal-bus"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = var.internal_bus_arn
    }]
  })
}
