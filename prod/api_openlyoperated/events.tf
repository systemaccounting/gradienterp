###############################################
# events.openlyoperated.biz — the stream.
#
#   shared bus ──rule (detail.customer_id, no detail.to)──▶ publisher lambda ──SigV4 POST /event──▶
#   Events API (AppSync), namespace `oob` ──WebSocket──▶ subscribers, and the api's GET /v1/events
#   relays a channel as server-sent events
#
# Channels: `/oob/counters` (every counter delta, no gerp id) and `/oob/<gerp_id>/<kind>` (a
# published gerp's events, projected through their contracts). Publish is IAM; connect and
# subscribe are the public key, which a page or an agent may hold — subscribe-only.
###############################################

resource "aws_appsync_api" "events" {
  name = "${local.stack_prefix}-oob-events"

  event_config {
    auth_provider {
      auth_type = "API_KEY"
    }
    auth_provider {
      auth_type = "AWS_IAM"
    }
    connection_auth_mode {
      auth_type = "API_KEY"
    }
    default_publish_auth_mode {
      auth_type = "AWS_IAM"
    }
    default_subscribe_auth_mode {
      auth_type = "API_KEY"
    }
  }
}

resource "aws_appsync_channel_namespace" "oob" {
  api_id = aws_appsync_api.events.api_id
  name   = "oob"
}

# the subscribe key: public, subscribe-only. AppSync caps a key at a year; the date moves forward
# with an apply before it passes, and the relay and the page read the new one from the outputs.
resource "aws_appsync_api_key" "subscribe" {
  api_id      = aws_appsync_api.events.api_id
  description = "subscribe to the oob channels — public"
  expires     = "2027-09-01T00:00:00Z"
}

locals {
  events_http_host     = aws_appsync_api.events.dns["HTTP"]
  events_realtime_host = aws_appsync_api.events.dns["REALTIME"]
}

# ─── the host: events.openlyoperated.biz, on the api's certificate ───

resource "aws_appsync_domain_name" "events" {
  domain_name     = "events.openlyoperated.biz"
  certificate_arn = aws_acm_certificate_validation.api.certificate_arn
  description     = "the stream: https://events.openlyoperated.biz/event to publish, wss://events.openlyoperated.biz/event/realtime to subscribe"
}

resource "aws_appsync_domain_name_api_association" "events" {
  api_id      = aws_appsync_api.events.api_id
  domain_name = aws_appsync_domain_name.events.domain_name
}

resource "aws_route53_record" "events" {
  zone_id = data.aws_route53_zone.biz.zone_id
  name    = "events.openlyoperated.biz"
  type    = "A"
  alias {
    name                   = aws_appsync_domain_name.events.appsync_domain_name
    zone_id                = aws_appsync_domain_name.events.hosted_zone_id
    evaluate_target_health = false
  }
}

# ─── the publisher: bus → Events API ───

data "archive_file" "publisher" {
  type        = "zip"
  output_path = "${path.module}/.build/publisher.zip"

  source {
    content  = file("${path.module}/lambdas/publisher/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/../../modules/aws/aws.py")
    filename = "aws.py"
  }
  # the event contracts: what a kind carries, and which of it is a person or a secret
  dynamic "source" {
    for_each = fileset("${path.module}/../../modules/events", "*/*.v1.json")
    content {
      content  = file("${path.module}/../../modules/events/${source.value}")
      filename = "contracts/${basename(source.value)}"
    }
  }
}

resource "aws_iam_role" "publisher" {
  name = "${local.stack_prefix}-publisher"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "publisher" {
  name = "${local.stack_prefix}-publisher"
  role = aws_iam_role.publisher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["appsync:EventPublish"]
        Resource = "${aws_appsync_api.events.api_arn}/channelNamespace/${aws_appsync_channel_namespace.oob.name}"
      },
      {
        # the gerp row's `published` bit
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${local.operator_account_id}:table/${local.stack_prefix}-customers"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:*"
      },
    ]
  })
}

module "publisher" {
  source = "../../modules/terraform/lambda"

  name             = "${local.stack_prefix}-publisher"
  role             = aws_iam_role.publisher.arn
  filename         = data.archive_file.publisher.output_path
  source_code_hash = data.archive_file.publisher.output_base64sha256
  src_dir          = "prod/api_openlyoperated/lambdas/publisher"
  timeout          = 20
  env_vars = {
    EVENTS_HTTP     = "https://${local.events_http_host}/event"
    CUSTOMERS_TABLE = "${local.stack_prefix}-customers"
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.publisher
  to   = module.publisher.aws_lambda_function.this
}


resource "aws_cloudwatch_event_rule" "publisher" {
  name           = "${local.stack_prefix}-publisher"
  description    = "Every gerp's own events (customer_id set, not addressed) to the publisher"
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  event_pattern = jsonencode({
    detail = {
      customer_id = [{ exists = true }]
      to          = [{ exists = false }]
    }
  })
}

resource "aws_cloudwatch_event_target" "publisher" {
  rule           = aws_cloudwatch_event_rule.publisher.name
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  target_id      = "publisher"
  arn            = module.publisher.arn
}

resource "aws_lambda_permission" "publisher_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.publisher.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.publisher.arn
}

# ─── outputs ───

output "events_url" {
  description = "The stream's host: /event to publish (IAM), /event/realtime to subscribe (the key)."
  value       = "https://events.openlyoperated.biz"
}

output "events_hosts" {
  description = "The Events API's own hosts, before the custom domain."
  value       = { http = local.events_http_host, realtime = local.events_realtime_host }
}

output "events_subscribe_key" {
  description = "The public subscribe key: connect and subscribe only."
  value       = aws_appsync_api_key.subscribe.key
  sensitive   = true
}
