###############################################
# gradienterp.cloud — owner web app + BFF (operator account, singleton front door).
#
# One front door for all owners. Serves the SPA (static, public) and an /api/* BFF
# (behind a JWT authorizer trusting the operator Cognito pool). The BFF enforces
# gerp ownership and forwards to the selected gerp's per-customer gateway. See
# AGENTS.md + secrets.md (the addsecret route is the first slice).
#
# SCAFFOLD: lambda serves the static SPA for now (single deployable, local-first).
# Production hosting (S3 + CloudFront + gradienterp.cloud domain/ACM) is a follow-on
# (TODO.md). Needs the standard backend + provider blocks (mirror prod/tower) to
# apply; not yet applied.
###############################################

data "aws_organizations_organization" "this" {}

# the operator's verified sending domain: the /support page's messages leave from it
data "aws_sesv2_email_identity" "sender" {
  email_identity = "gradienterp.cloud"
}

locals {
  config         = jsondecode(file("${path.module}/../../config.json"))
  stack_prefix   = local.config.STACK_PREFIX
  cognito_pool   = local.config.COGNITO_USER_POOL_ID
  cognito_client = local.config.COGNITO_CLIENT_ID
  prefix         = "${local.stack_prefix}-cloud"
  # where the operator stored the seller gerp's published customers/upsert answer ({url, token}),
  # and the customers/erase one beside it (the same caller, so the same token)
  customer_hook_param = "/gradienterp/cloud/hooks/customers_upsert"
  # the read-only role in management the BFF counts the org's accounts through, for the create
  # screen's "accounts currently available" line (prod/platform/management capacity_read_role.tf)
  capacity_read_role        = "arn:aws:iam::${data.aws_organizations_organization.this.master_account_id}:role/GerpCapacityRead"
  customer_erase_hook_param = "/gradienterp/cloud/hooks/customers_erase"
  # the metrics door gradienterp published for its own app (modules/metrics op=publish_source,
  # caller operator), stored the same way: SecureString {url, token}
  metrics_hook_param = "/gradienterp/cloud/hooks/metrics"
  # operator artifact bucket the web bundle deploys from (same account as this stack) — matches
  # the module-lambda convention (data.aws_s3_object below); literal like the module var-default.
  artifact_bucket = "${local.stack_prefix}-artifacts-185369506315"
  bff_key         = "prod/gradienterp_cloud/bff.zip"
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── BFF lambda (serves SPA + /api/*) ───
#
# The web bundle (main.py + web/*) is pushed to the operator artifact bucket by
# `bash scripts/deploy.sh push --dirs prod/gradienterp_cloud/bff`; the lambda sources it from there —
# same convention as every module lambda — so a `terraform apply` is a pointer-sync no-op, never a
# re-bundle. First deploy is push-then-apply (this data source fails the plan until the object exists).

# ── the vending switch ──────────────────────────────────────────────────────
# Empty by DEFAULT, so vending stays off unless someone turns it on deliberately:
#
#   terraform apply                                  # gerps stop at awaiting_payment
#   config.json "PROVISION_QUEUE": "tower-vends"   # real sub-accounts
#
# Production has vended since 2026-09-04; config.json is tracked, so every tree applies the same
# switch, and the line there is it.
#
# Off, `POST /api/gerps` records the row and `save-card` leaves it at awaiting_payment — the
# whole create → pay → return path runs with no Control Tower and nothing to clean up after.
# Defaulting to ON would mean a routine apply silently enables 15-minute account vending, which is
# not a thing anyone should discover by accident.
locals {
  # Tower's vends queue (the provisioner consumes it four at a time). Empty disables vending.
  provision_queue = local.config.PROVISION_QUEUE
}

# ── the closure switch ──────────────────────────────────────────────────────
# Off by default. Off, `POST /api/gerps/close` records the request on the row and hands nothing on,
# so the dialog, the typed confirmation and the whole owner-facing path run with nothing destroyed.
# On, the request goes to the seller gerp's closure scripts, which sit behind the operator's own
# CLOSE_BUILD_PROJECT switch in turn. Closure destroys a customer's instance; it does not get
# enabled by a routine apply. config.json "CLOSURE_ENABLED", on since 2026-09-05.
locals {
  closure_enabled = local.config.CLOSURE_ENABLED
}

variable "seller_gerp" {
  description = "The gerp that sells hosting. Its payments lambdas create the card-setup link and store the result."
  type        = string
  default     = "gradienterp"
}

variable "seller_account_id" {
  description = "AWS account of the seller gerp. The BFF invokes two of its lambdas cross-account during gerp creation."
  type        = string
  default     = "867637277314"
}

resource "aws_iam_role" "bff" {
  name = "${local.prefix}-bff"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "bff" {
  name = "${local.prefix}-bff"
  role = aws_iam_role.bff.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # gerp-customers = the gerp's instance record. GetItem joins it from gerp-members for
        # /api/gerps and the gerp-scoped forwards; PutItem on create-gerp; UpdateItem stamps a
        # closure request on the row; Scan counts the `queued` rows ahead of one in line.
        Effect = "Allow"
        # DeleteItem: an `awaiting_payment` row goes with its account — nothing was vended
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-customers"
      },
      {
        # gerp-members = the account↔gerp membership spine, the one read of ownership. Query by
        # account_id for /api/gerps and the forwards; PutItem the owner row on create-gerp.
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-members"
      },
      {
        # a card landing sends the provisioning payload to tower's vends queue (same account)
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = "arn:aws:sqs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:tower-vends"
      },
      {
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          # a changed login → the Identity Center user and each owned gerp's tenant blob
          "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:tower-update-owner-email",
          # an edited business profile → the gerp's tenant blob
          "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:tower-update-business-info",
        ]
      },
      {
        # the card-saving pair, in the SELLER's account. Gerp creation runs here, in operator,
        # and the payer's card is saved before the buyer's own gerp exists — so the setup link
        # and the write both have to land in gradienterp's gerp. The callee side admits this
        # role by name (modules/payments, billing_invoker_role_arn).
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-payment_links",
          "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-save_payment_method",
          "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-manage_saved_cards",
          "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-charge_saved_method",
        ]
      },
      {
        # every customer's export lambda, by wildcard rather than by name. Unlike the pair above
        # there is no fixed account: the target is whichever gerp the caller owns, and the set
        # grows with every gerp vended, so enumerating it here would mean an apply of THIS stack
        # each time one is created. The gerp side still admits this role by name
        # (modules/export, export_invoker_role_arn), and the handler checks ownership before it
        # calls — the wildcard is what makes the call POSSIBLE, not what decides who may.
        #
        # `:*:` is the account: org-scoped is not expressible in a Resource arn, so the narrowing
        # that matters is the function name, which only per-customer export stacks ever carry.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:*:function:${local.stack_prefix}-export-*-export_gerp"
      },
      {
        # the vendor-consent landing (modules/mcp): the owner returns from a vendor with a
        # session id, and the gerp holding that pending session completes it. Same shape as
        # export's: the callee admits this role by name, the handler checks ownership first.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:*:function:${local.stack_prefix}-mcp-*-complete_mcp_auth"
      },
      {
        # closing a gerp hands the request to the seller gerp's closure scripts through its
        # `automate` runner. The callee admits this role by name (modules/automation,
        # closure_invoker_role_arn). Whether anything is destroyed is decided there, behind the
        # operator's own switch; this stack starts no build.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-automation-${var.seller_gerp}-automate"
      },
      {
        # the seller gerp's customer-contact hook: url + bearer the operator stored after publishing the hook.
        # The BFF posts the account record there as an outside caller — no cross-account grant.
        # the org's account count against its quota: two reads through a role in management
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = local.capacity_read_role
      },
      {
        # the /support page's message, from the operator's sender to the support address
        Effect   = "Allow"
        Action   = "ses:SendEmail"
        Resource = data.aws_sesv2_email_identity.sender.arn
      },
      {
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.customer_hook_param}",
          "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.customer_erase_hook_param}",
          "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.metrics_hook_param}",
        ]
      },
      {
        # public profile → operator-account profile registry (read + upsert the caller's own)
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-profiles"
      },
      {
        # address autocomplete/geocode for the public-profile form (Places V2 is resource-less)
        Effect   = "Allow"
        Action   = ["geo-places:Autocomplete", "geo-places:GetPlace"]
        Resource = "*"
      },
      {
        # account info (Info & Billing) — read/write the caller's own private profile row
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-accounts"
      },
      {
        # what a closed account left behind — written by the deletion, read at create-gerp and
        # at provisioning
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.stack_prefix}-priors"
      },
      {
        # the account's Cognito user goes last in the deletion
        Effect   = "Allow"
        Action   = "cognito-idp:AdminDeleteUser"
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:userpool/${local.cognito_pool}"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# Code ships with `deploy.sh push --dirs prod/gradienterp_cloud/bff` (build → artifact bucket → update-function-code, the
# fleet's shape), so the bucket's latest version is the running code and an apply's re-pin is a
# no-op. 29s timeout: just inside API Gateway's 30s integration cap — `save-card` invokes the
# seller's lambda SYNCHRONOUSLY, three Stripe round trips deep, and a shorter timeout here fails
# a request the callee then completes (the card is saved and the buyer told it was not).
module "bff" {
  source = "../../modules/terraform/lambda"

  name            = "${local.prefix}-bff"
  role            = aws_iam_role.bff.arn
  artifact_bucket = local.artifact_bucket
  artifact_key    = local.bff_key
  src_dir         = "prod/gradienterp_cloud/bff"
  timeout         = 29
  memory          = 512
  env_vars = {
    CUSTOMERS_TABLE  = "${local.stack_prefix}-customers"
    MEMBERS_TABLE    = "${local.stack_prefix}-members"
    PROVISION_QUEUE  = local.provision_queue == "" ? "" : "https://sqs.${data.aws_region.current.region}.amazonaws.com/${data.aws_caller_identity.current.account_id}/${local.provision_queue}"
    OWNER_EMAIL_FN   = "tower-update-owner-email"
    BUSINESS_INFO_FN = "tower-update-business-info"
    # a requested closure goes to the seller gerp's closure scripts; empty records and stops
    CLOSURE_BEGIN_FN = local.closure_enabled ? "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-automation-${var.seller_gerp}-automate" : ""
    # the card-saving pair, in the seller's account (cross-account invoke)
    # Full ARNs, not names: boto3 resolves an unqualified function name against the CALLER's
    # account, so a bare name looks for these in operator and fails on an arn that never existed.
    SETUP_LINK_FN       = "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-payment_links"
    SAVE_CARD_FN        = "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-save_payment_method"
    CARD_METHODS_FN     = "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-manage_saved_cards"
    CHARGE_FN           = "arn:aws:lambda:${data.aws_region.current.region}:${var.seller_account_id}:function:${local.stack_prefix}-payments-${var.seller_gerp}-charge_saved_method"
    PROFILES_TABLE      = "${local.stack_prefix}-profiles"
    REGIONS             = jsonencode(local.config.REGIONS) # the create screen's regions (config.json)
    CUSTOMER_HOOK_PARAM = local.customer_hook_param
    METRICS_HOOK_PARAM  = local.metrics_hook_param
    CAPACITY_READ_ROLE  = local.capacity_read_role
    # the card page's Stripe.js key — the account's publishable key, public by design
    STRIPE_PUBLISHABLE_KEY = "pk_live_51Ta6U5RBOqTW9S9WmFw1hMXSPxX5YflH7dimUqgvThzHJXlzQ9WPEQzSVhEWIIFPRP8vOxqv5ZJeUt8geVHRvKqg004OHK5Q7E"
    # the /support page: from the operator's sender to the support mailbox (the SES catch-all
    # forwards it); the address lives here and in no page
    SENDER_EMAIL              = "ops+sender@gradienterp.cloud"
    SUPPORT_EMAIL             = "hello@gradienterp.cloud"
    CUSTOMER_ERASE_HOOK_PARAM = local.customer_erase_hook_param
    ACCOUNTS_TABLE            = "${local.stack_prefix}-accounts"
    PRIORS_TABLE              = "${local.stack_prefix}-priors"
    USER_POOL_ID              = local.cognito_pool
    AGENT_EMAIL_PARENT_DOMAIN = local.config.AGENT_EMAIL_PARENT_DOMAIN # gerp-config derives the agent address
    WEB_DIR                   = "/var/task/web"
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.bff
  to   = module.bff.aws_lambda_function.this
}


# ─── HTTP API: static (public) + /api/* (JWT-authed) ───

resource "aws_apigatewayv2_api" "this" {
  name          = local.prefix
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_authorizer" "owner" {
  api_id           = aws_apigatewayv2_api.this.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${local.prefix}-owner"
  jwt_configuration {
    audience = [local.cognito_client]
    issuer   = "https://cognito-idp.${data.aws_region.current.region}.amazonaws.com/${local.cognito_pool}"
  }
}

resource "aws_apigatewayv2_integration" "bff" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.bff.invoke_arn
  payload_format_version = "2.0"
}

# static SPA + auth callback — public (must load before login)
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.bff.id}"
}

# the private support form: public (it has no account behind it), and its own route so the stage can
# throttle it. Each post is an SES send from the identity Cognito's verification mail also sends from.
resource "aws_apigatewayv2_route" "support" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /api/support"
  target    = "integrations/${aws_apigatewayv2_integration.bff.id}"
}

# /api/* — owner JWT required
resource "aws_apigatewayv2_route" "api" {
  for_each           = toset(["GET /api/gerps", "POST /api/gerps", "GET /api/public-user", "POST /api/public-user", "GET /api/account", "POST /api/account", "DELETE /api/account", "GET /api/gerp-config", "GET /api/regions", "GET /api/capacity", "POST /api/gerp-settings", "GET /api/gerp-info", "POST /api/gerp-info", "GET /api/places/autocomplete", "GET /api/places/place", "POST /api/billing/setup-link", "POST /api/billing/save-card", "POST /api/billing/methods", "POST /api/billing/pay", "POST /api/billing/pay-link", "POST /api/export", "POST /api/mcp/complete", "POST /api/gerps/close", "POST /api/automate/{proxy+}"])
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.bff.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.owner.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  # a flood of support posts is refused here (429), before it spends the SES quota signups need
  route_settings {
    route_key              = aws_apigatewayv2_route.support.route_key
    throttling_rate_limit  = 1
    throttling_burst_limit = 5
  }

  # the same access log every gerp's server stage writes (modules/server)
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId       = "$context.requestId"
      requestTime     = "$context.requestTime"
      httpMethod      = "$context.httpMethod"
      routeKey        = "$context.routeKey"
      status          = "$context.status"
      responseLatency = "$context.responseLatency"
      sourceIp        = "$context.identity.sourceIp"
      # why: the fields that explain a 502 or a 401 the function never saw — the 30 s
      # integration wall, a payload too large, a permission gap, a bad JWT
      integrationStatus = "$context.integration.status"
      integrationError  = "$context.integrationErrorMessage"
      error             = "$context.error.message"
      authorizerError   = "$context.authorizer.error"
    })
  }
}

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${local.prefix}-access"
  retention_in_days = local.config.LOG_RETENTION_DAYS
}

resource "aws_cloudwatch_metric_alarm" "gateway_5xx" {
  count               = try(local.config.OPS_ALERTS_TOPIC_ARN, "") != "" ? 1 : 0
  alarm_name          = "${local.prefix}-gateway-5xx"
  alarm_description   = "the owner app's API stage answered 5xx; the access log /aws/apigateway/${local.prefix}-access carries integrationStatus, integrationError and error for the request"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.this.id, Stage = aws_apigatewayv2_stage.default.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.config.OPS_ALERTS_TOPIC_ARN]
  ok_actions          = [local.config.OPS_ALERTS_TOPIC_ARN]
}

resource "aws_lambda_permission" "apigw" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.bff.name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

output "api_endpoint" {
  value       = aws_apigatewayv2_api.this.api_endpoint
  description = "BFF/SPA URL. Add this + its /auth/callback to the gerp-cloud Cognito app client's callback URLs to enable Hosted UI login (or wire gradienterp.cloud)."
}
