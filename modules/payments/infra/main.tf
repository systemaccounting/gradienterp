# payments — webhook ingestion layer. Receives payment-processor webhooks on
# the customer's API gateway, validates + dedups, dispatches to accounting's
# ingest transforms, and cross-invokes post_journal_entry. No account-mapping
# data lives here (transforms are code in accounting; classification is a row in
# accounting's classifications table). See modules/payments/AGENTS.md.
#
# Stripe is the first provider; square + paypal reuse this shape (their
# transforms already exist in accounting/lambdas/ingest/transform.py).


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

variable "internal_bus_name" {
  description = "The firm's own event bus (modules/events/infra). A firm's invoice-transition rule announces on it; the collection rule here subscribes."
  type        = string
}

variable "contacts_get_fn_name" {
  description = "Name of contacts' manage_contacts lambda. charge_saved_method reads the payer's stripe_customer_id / stripe_payment_method_id from their contact — the fields save_payment_method wrote."
  type        = string
  default     = ""
}

variable "contacts_get_fn_arn" {
  description = "ARN of contacts' manage_contacts lambda — the invoke grant for the saved-card read."
  type        = string
  default     = ""
}

variable "contacts_put_fn_name" {
  description = "Name of contacts' manage_contacts lambda. save_payment_method creates the contact on first sight of a buyer, then merges."
  type        = string
  default     = ""
}

variable "contacts_put_fn_arn" {
  description = "ARN of contacts' manage_contacts lambda — the invoke grant for the create-on-first-sight path."
  type        = string
  default     = ""
}

variable "contacts_update_fn_arn" {
  description = "ARN of contacts' manage_contacts lambda — the invoke grant for save_payment_method's write."
  type        = string
  default     = ""
}

variable "contacts_update_fn_name" {
  description = "Name of contacts' manage_contacts lambda. save_payment_method writes the saved-card ids onto the payer's contact through it."
  type        = string
  default     = ""
}

variable "billing_invoker_role_arn" {
  description = "Role allowed to invoke the two card-saving lambdas from ANOTHER account — the gerp-cloud BFF, which runs in the operator account and has no other way to reach this one. Empty on every gerp but the one that sells hosting."
  type        = string
  default     = ""
}

variable "post_journal_entry_fn_arn" {
  description = "ARN of accounting's post_journal_entry lambda (cross-invoke target)."
  type        = string
}

variable "post_journal_entry_fn_name" {
  description = "Name of accounting's post_journal_entry lambda."
  type        = string
}

variable "server_api_id" {
  description = "Per-customer HTTP API id (from modules/server/infra). Webhook routes attach here."
  type        = string
}

variable "server_api_execution_arn" {
  description = "Per-customer HTTP API execution ARN root. Source ARN for the API-gateway invoke permission."
  type        = string
}

variable "settings_table_name" {
  description = "gerp-settings table — the ingest lambdas resolve a webhook's provider location id against the LOCATION# rows (cold-start cached); empty ⇒ everything attributes to the default location."
  type        = string
  default     = ""
}

variable "settings_table_arn" {
  description = "gerp-settings table ARN for the LOCATION# Query grant."
  type        = string
  default     = ""
}

variable "webhook_base_url" {
  description = "Per-customer HTTP API base URL (modules/server/infra api_endpoint). configure_webhook creates the Stripe endpoint at <webhook_base_url>/webhooks/stripe."
  type        = string
  default     = ""
}

variable "square_api_base" {
  description = "Square API base URL. Production (default) or https://connect.squareupsandbox.com for sandbox — a Square access token only works against its own environment's base. Used by configure_webhook + payment_links (kind test)."
  type        = string
  default     = "https://connect.squareup.com"
}

variable "paypal_api_base" {
  description = "PayPal REST API base URL. Production (default) or https://api-m.sandbox.paypal.com for sandbox — a PayPal client_id/secret pair only works against its own environment's base. Used by ingest_paypal (verify), configure_webhook + payment_links (kind test)."
  type        = string
  default     = "https://api-m.paypal.com"
}

variable "register_with_agent" {
  description = "Register the agent-facing payments tools (configure_webhook) as MCP gateway targets. Requires the agent module to have applied first (writes the gateway SSM params)."
  type        = bool
  default     = true
}

locals {
  prefix = "${var.stack_prefix}-payments-${replace(var.gerp_id, "_", "-")}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── dynamodb ───
#
# webhook_log: idempotency — one row per `<provider>#<event_id>`, conditional
# put is the dedup. dlq: events whose provider/type has no transform yet.

resource "aws_dynamodb_table" "webhook_log" {
  name         = "${local.prefix}-webhook-log"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}

resource "aws_dynamodb_table" "dlq" {
  name         = "${local.prefix}-dlq"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}

# The dead letter splits in two, keyed the same, because the two halves answer to different rules.
# "how many provider events failed to map, and why" is real operational information and belongs in
# the open; the BODY that failed to map is a whole provider webhook — names, addresses, emails,
# card last4 — and it arrives on exactly the paths a human then goes and inspects. Same table, and
# a reader over the dlq is a reader over the bodies; separate tables and the safe half is safe by
# construction, no projection to get right.
resource "aws_dynamodb_table" "dlq_bodies" {
  name         = "${local.prefix}-dlq-bodies"
  tags         = { "gerp:layer" = "operational" } # reset lifecycle, same as its sibling — the tag classifies STATE, not sensitivity
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}

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
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.webhook_log.arn, aws_dynamodb_table.dlq.arn, aws_dynamodb_table.dlq_bodies.arn]
      },
      {
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = compact([
          var.post_journal_entry_fn_arn,
          # save_payment_method writes the saved-card ids onto the payer's contact
          var.contacts_update_fn_arn,
          var.contacts_put_fn_arn,
          # charge_saved_method reads the payer's contact for the card they saved
          var.contacts_get_fn_arn,
          # payment_links (kind payment) reads the invoice it is a link for
          "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}-manage_invoice",
          # ingest_stripe settles the receivable when a charge names its invoice — the transition
          # releases parked revenue per line, which a journal entry cannot do
          "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}-record_invoice_paid",
          # charge_saved_method says so when a charge does not land — `unpaid` is what a firm's
          # chase attaches to, and it posts nothing, so this grants no ledger reach
          "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}-mark_unpaid",
        ])
      },
      {
        # two prefixes on the settings table:
        #   LOCATION#  the webhook location_id → ordinal map, read once per container
        #   PROVIDER#  which processors this firm has set up, and which one a call means when
        #              `provider` is omitted. configure_webhook writes it when setup SUCCEEDS.
        #   MCP#       the vendor's row (modules/mcp): configure_webhook keeps a pending consent
        #              on it when Stripe's tools ask for one (firm_gateway.py) — GetItem for that
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = var.settings_table_arn != "" ? var.settings_table_arn : aws_dynamodb_table.webhook_log.arn
      },
      {
        # processor secrets at /gradienterp/customers/<id>/secrets/<provider>:
        # ingest_<provider> reads what verifies a delivery; configure_webhook writes it.
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/secrets/*"
      },
      {
        # modules/mcp: the vendor gateway's url and the firm's client — configure_webhook makes
        # Stripe's endpoint create as the firm (firm_gateway.py) so the pasted key can go
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParametersByPath"]
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/mcp",
          "arn:aws:ssm:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/mcp/*",
        ]
      },
      {
        Effect    = "Allow"
        Action    = "kms:Decrypt"
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "ssm.${data.aws_region.current.id}.amazonaws.com" } }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambdas ───
#
# Each ingest lambda bundles _helpers.py + accounting's transform.py (the
# canonical webhook→journal-entry mapping) alongside main.py.

locals {
  # A lambda is a unit of INVOCATION, not of code (AGENTS.md § providers).
  #
  # `ingest_*` stays split because there are three CALLERS — each vendor POSTs to its own URL with
  # its own signature scheme. `configure_webhook` is one because the agent calls one capability and
  # should not have to know which processor a firm uses in order to name a tool; the provider is a
  # parameter, and setup is the one moment the agent has just been told the answer.
  functions = {
    ingest_stripe       = "ingest_stripe"
    ingest_square       = "ingest_square"
    ingest_paypal       = "ingest_paypal"
    configure_webhook   = "configure_webhook"
    payment_links       = "payment_links"
    charge_saved_method = "charge_saved_method"
    save_payment_method = "save_payment_method"
    manage_saved_cards  = "manage_saved_cards"
    check_collection    = "check_collection"
  }

  env_vars = {
    WEBHOOK_LOG_TABLE     = aws_dynamodb_table.webhook_log.name
    SETTINGS_TABLE        = var.settings_table_name
    DLQ_TABLE             = aws_dynamodb_table.dlq.name
    DLQ_BODIES_TABLE      = aws_dynamodb_table.dlq_bodies.name
    POST_JOURNAL_ENTRY_FN = var.post_journal_entry_fn_name
    # payment_links (kind payment) reads the invoice FRESH each time — a sequence sends the same one for
    # weeks and the amount can change under it
    GET_INVOICES_FN = "${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}-manage_invoice"
    # ingest_stripe: a charge carrying metadata[invoice_id] settles that receivable
    RECORD_INVOICE_PAYMENT_FN = "${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}-record_invoice_paid"
    # a charge that failed: the invoice stops saying only "owed" and starts saying "we tried"
    MARK_UNPAID_FN = "${var.stack_prefix}-invoicing-${replace(var.gerp_id, "_", "-")}-mark_unpaid"
    # the collection watch — a charge enqueues it, check_collection consumes it (collection_watch.tf)
    COLLECTION_WATCH_QUEUE = aws_sqs_queue.collection_watch.url
    COLLECTION_WATCH_DELAY = "300"
    # charge_saved_method reads the payer's saved-card ids off their contact — the fields
    # save_payment_method wrote after they completed a hosted setup page
    CONTACTS_GET_FN    = var.contacts_get_fn_name
    CUSTOMER_ID        = var.gerp_id
    WEBHOOK_BASE_URL   = var.webhook_base_url
    SQUARE_API_BASE    = var.square_api_base
    PAYPAL_API_BASE    = var.paypal_api_base
    CONTACTS_UPDATE_FN = var.contacts_update_fn_name
    CONTACTS_PUT_FN    = var.contacts_put_fn_name
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = aws_iam_role.lambda.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/payments/lambdas/${each.key}.zip"
  src_dir            = "modules/payments/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = 30
  memory             = 512
  env_vars           = local.env_vars
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["ingest_stripe"]
  to   = module.fn["ingest_stripe"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["ingest_square"]
  to   = module.fn["ingest_square"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["ingest_paypal"]
  to   = module.fn["ingest_paypal"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["configure_webhook"]
  to   = module.fn["configure_webhook"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["payment_links"]
  to   = module.fn["payment_links"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["charge_saved_method"]
  to   = module.fn["charge_saved_method"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["save_payment_method"]
  to   = module.fn["save_payment_method"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["manage_saved_cards"]
  to   = module.fn["manage_saved_cards"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["check_collection"]
  to   = module.fn["check_collection"].aws_lambda_function.this
}


# ─── api gateway route (POST /webhooks/stripe) ───

resource "aws_apigatewayv2_integration" "ingest_stripe" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn["ingest_stripe"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "stripe" {
  api_id    = var.server_api_id
  route_key = "POST /webhooks/stripe"
  target    = "integrations/${aws_apigatewayv2_integration.ingest_stripe.id}"
}

resource "aws_lambda_permission" "apigw_ingest_stripe" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeIngestStripe"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["ingest_stripe"].name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# ─── api gateway route (POST /webhooks/square) ───

resource "aws_apigatewayv2_integration" "ingest_square" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn["ingest_square"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "square" {
  api_id    = var.server_api_id
  route_key = "POST /webhooks/square"
  target    = "integrations/${aws_apigatewayv2_integration.ingest_square.id}"
}

resource "aws_lambda_permission" "apigw_ingest_square" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeIngestSquare"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["ingest_square"].name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# ─── api gateway route (POST /webhooks/paypal) ───

resource "aws_apigatewayv2_integration" "ingest_paypal" {
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn["ingest_paypal"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "paypal" {
  api_id    = var.server_api_id
  route_key = "POST /webhooks/paypal"
  target    = "integrations/${aws_apigatewayv2_integration.ingest_paypal.id}"
}

resource "aws_lambda_permission" "apigw_ingest_paypal" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeIngestPaypal"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["ingest_paypal"].name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# ─── agent gateway registration ───
#
# configure_webhook is an agent tool (it has a schema.json). ingest_stripe
# is an HTTP webhook receiver (no schema.json) and is excluded by the filter below.
# Same pattern as the domain modules: register the lambda as an MCP target;
# permission is keyed off the gateway role ARN (both from SSM written by the agent).

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

output "webhook_log_table" {
  value = aws_dynamodb_table.webhook_log.name
}

output "dlq_bodies_table" {
  value = aws_dynamodb_table.dlq_bodies.name
}

output "dlq_table" {
  value = aws_dynamodb_table.dlq.name
}

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "lambda_arns" {
  value = { for k, fn in module.fn : k => fn.arn }
}

# latest artifact version per function — the apply-time read that makes terraform deploy
# BUCKET truth (always current via scripts/deploy.sh push) instead of the applier's tree.

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

# ─── cross-account invoke for the card-saving pair ───
#
# Gerp creation runs in the gerp-cloud BFF, in the OPERATOR account, and the payer's card is
# saved before the customer's own gerp exists — so the BFF has to reach into the SELLER's
# account. It has no path there: its one lambda invoke today (tower-provision-customer) is
# same-account.
#
# A resource policy rather than an assume-role hop: it names exactly two functions, is
# readable from the callee side, and is the shape AgentCore's cross-account invoke already
# uses. Set only on the gerp that sells hosting; empty everywhere else.

resource "aws_lambda_permission" "billing_invoker" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  for_each = var.billing_invoker_role_arn == "" ? toset([]) : toset([
    "payment_links", "save_payment_method", "manage_saved_cards",
    # Pay now on a missed hosting charge: the BFF asks the seller to charge the saved card
    "charge_saved_method",
  ])

  statement_id_prefix = "AllowBffCardSetup"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn[each.value].name
  principal           = var.billing_invoker_role_arn
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
