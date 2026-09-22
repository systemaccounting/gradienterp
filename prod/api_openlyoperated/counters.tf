###############################################
# Economic counters.
#
#   bus event (detail.counters) ──rule──▶ counter lambda ──ADD──▶ counters DDB ──▶ GET /v1/economy/counters
#
# The counter lambda is dumb: the emitter stamps {op,key,magnitude} on the event (translate at the
# boundary), the lambda does the arithmetic. Aggregate = terms-of-use baseline, so the rule matches on
# `detail.counters` existing — every gerp, no gate. The live deltas go out on the stream (events.tf).
###############################################

data "aws_region" "current" {}

locals {
  econ_prefix = "${local.stack_prefix}-econ"
  econ_fns    = toset(["counter"])
}

# ─── tables ───

# One partition per gerp, the platform's own signals under `platform`. The range key is the public
# metric key (modules/metrics/metric_key.py public_key: `<event>#<kind>[#<property>=<value>]#<grain>#<period>`)
# for a firm's rows and `<signal>#<YYYY-MM>` for the platform's, each row carrying `signal` and `period`
# as attributes too, so no reader splits a key. A firm's whole public suite is one Query on its partition.
resource "aws_dynamodb_table" "counters" {
  name         = "${local.stack_prefix}-counters"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gerp_id"
  range_key    = "key"

  attribute {
    name = "gerp_id"
    type = "S"
  }
  attribute {
    name = "key"
    type = "S"
  }
}

# an event id, once: a bus delivers at least once, and a firm's count must not double
resource "aws_dynamodb_table" "counters_seen" {
  name         = "${local.stack_prefix}-counters-seen"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
  ttl {
    attribute_name = "expires"
    enabled        = true
  }
}

# ─── lambda packaging (one main.py each) ───

data "archive_file" "econ" {
  for_each    = local.econ_fns
  type        = "zip"
  output_path = "${path.module}/.build/${each.key}.zip"

  source {
    content  = file("${path.module}/lambdas/${each.key}/main.py")
    filename = "main.py"
  }
  # these handlers import `aws` (modules/aws/aws.py), the local/AWS client factory. This archive is
  # a hand-listed manifest, unlike scripts/deploy.py which walks the import graph — a shared lib
  # added to an import here has to be added here too or the function ImportErrors at cold start.
  # the public metric key, read here and built nowhere else (modules/metrics)
  source {
    content  = file("${path.module}/../../modules/metrics/metric_key.py")
    filename = "metric_key.py"
  }
  source {
    content  = file("${path.module}/../../modules/aws/aws.py")
    filename = "aws.py"
  }
}

# ─── execution role ───

resource "aws_iam_role" "econ" {
  name = "${local.econ_prefix}-lambda"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "econ" {
  name = "${local.econ_prefix}-lambda"
  role = aws_iam_role.econ.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.counters.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.counters_seen.arn
      },
      {
        # the gerp's account, to take a count only from the gerp the event names
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

# ─── functions ───

module "counter" {
  source = "../../modules/terraform/lambda"

  name               = "${local.econ_prefix}-counter"
  role               = aws_iam_role.econ.arn
  filename           = data.archive_file.econ["counter"].output_path
  source_code_hash   = data.archive_file.econ["counter"].output_base64sha256
  src_dir            = "prod/api_openlyoperated/lambdas/counter"
  timeout            = 15
  env_vars           = { COUNTERS_TABLE = aws_dynamodb_table.counters.name, SEEN_TABLE = aws_dynamodb_table.counters_seen.name, CUSTOMERS_TABLE = "${local.stack_prefix}-customers" }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.counter
  to   = module.counter.aws_lambda_function.this
}


# ─── bus rule: any event stamped with detail.counters → the counter lambda ───

resource "aws_cloudwatch_event_rule" "counters" {
  name           = "${local.stack_prefix}-counters"
  description    = "Route any event carrying detail.counters to the economic counter lambda (no openly_operated gate — aggregate is the ToU baseline)"
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name

  # match events whose detail.counters array has an element with a `key` — EventBridge `exists`
  # only matches leaf nodes, so we can't test the array itself; we test a leaf inside its elements.
  event_pattern = jsonencode({
    detail = { counters = { key = [{ exists = true }] } }
  })
}

resource "aws_cloudwatch_event_target" "counters" {
  rule           = aws_cloudwatch_event_rule.counters.name
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  arn            = module.counter.arn
}

resource "aws_lambda_permission" "counters_events" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowEventBridgeInvokeCounter"
  action              = "lambda:InvokeFunction"
  function_name       = module.counter.name
  principal           = "events.amazonaws.com"
  source_arn          = aws_cloudwatch_event_rule.counters.arn
}

# ─── outputs ───

output "counters_table" {
  description = "Economic counters table (pk = <signal>#<period>, value under `value`)."
  value       = aws_dynamodb_table.counters.name
}

# ─── a firm's product events: the second rule on the firm's bus sends every one here as recorded ───
resource "aws_cloudwatch_event_rule" "metrics" {
  name           = "${local.stack_prefix}-metrics"
  description    = "every product event a firm records, counted under its partition when its row reads published"
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  event_pattern  = jsonencode({ source = ["metrics"] })
}

resource "aws_cloudwatch_event_target" "metrics" {
  rule           = aws_cloudwatch_event_rule.metrics.name
  event_bus_name = data.terraform_remote_state.operator.outputs.events_bus_name
  target_id      = "counter"
  arn            = module.counter.arn
}

resource "aws_lambda_permission" "metrics_events" {
  statement_id  = "AllowEventBridgeInvokeCounterMetrics"
  action        = "lambda:InvokeFunction"
  function_name = module.counter.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.metrics.arn
}
