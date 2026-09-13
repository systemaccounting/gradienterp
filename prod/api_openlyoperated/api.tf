###############################################
# api.openlyoperated.biz — the read api.
#
# One REST API on API Gateway: stages for versions (`v1` is a stage; a breaking change is `v2`
# beside it), usage plans and api keys for the meter, response streaming for the reads that need
# it. The contract is `api/v1/openapi.json`; each path names its backend folder (`x-backend`) and
# whether it streams (`x-stream`). Terraform loops the folders and builds what each says it is —
# `handler.py` / `handler.mjs` a lambda; a `Dockerfile` a Fargate service behind a VPC Link (the
# folder shape is reserved; the case renders nothing until a folder exists) — and the resources,
# methods and integrations off the spec. Adding a resource is a folder and a path, no `.tf` edit.
###############################################

locals {
  api_dir  = "${path.module}/api/v1"
  api_spec = jsondecode(file("${local.api_dir}/openapi.json"))

  # backend folders: whatever under api/v1/ carries a handler or a Dockerfile
  backend_dirs = distinct([for f in fileset(local.api_dir, "*/{handler.py,handler.mjs,Dockerfile}") : dirname(f)])
  backends = {
    for d in local.backend_dirs : d => {
      kind    = fileexists("${local.api_dir}/${d}/Dockerfile") ? "service" : "lambda"
      node    = fileexists("${local.api_dir}/${d}/handler.mjs")
      runtime = fileexists("${local.api_dir}/${d}/handler.mjs") ? "nodejs22.x" : "python3.12"
    }
  }
  lambda_backends = { for d, b in local.backends : d => b if b.kind == "lambda" }

  # operations off the spec: "GET /gerps" => {path, method, backend, stream}
  ops = merge([
    for p, methods in local.api_spec.paths : {
      for m, op in methods : "${upper(m)} ${p}" => {
        path    = p
        method  = upper(m)
        backend = op["x-backend"]
        stream  = try(op["x-stream"], false)
        keyed   = try(op["x-keyed"], false)
      }
    }
  ]...)

  # every path prefix, by depth — API Gateway resources are a tree and a resource cannot reference
  # its own for_each, so one block per depth (four covers /gerps/{gerp_id}/sources/{source})
  segments = { for p in keys(local.api_spec.paths) : p => compact(split("/", p)) }
  prefixes = distinct(flatten([for p, s in local.segments : [for i in range(length(s)) : join("/", slice(s, 0, i + 1))]]))
  depth1   = [for pre in local.prefixes : pre if length(split("/", pre)) == 1]
  depth2   = [for pre in local.prefixes : pre if length(split("/", pre)) == 2]
  depth3   = [for pre in local.prefixes : pre if length(split("/", pre)) == 3]
  depth4   = [for pre in local.prefixes : pre if length(split("/", pre)) == 4]
  parent   = { for pre in local.prefixes : pre => join("/", slice(split("/", pre), 0, length(split("/", pre)) - 1)) }
  leaf     = { for pre in local.prefixes : pre => element(split("/", pre), length(split("/", pre)) - 1) }
  resource_ids = merge(
    { for k, r in aws_api_gateway_resource.d1 : k => r.id },
    { for k, r in aws_api_gateway_resource.d2 : k => r.id },
    { for k, r in aws_api_gateway_resource.d3 : k => r.id },
    { for k, r in aws_api_gateway_resource.d4 : k => r.id },
  )
  op_resource = { for k, op in local.ops : k => local.resource_ids[join("/", local.segments[op.path])] }
  # a backend with a streaming path holds its lambda for as long as the response runs
  stream_backends = distinct([for op in values(local.ops) : op.backend if op.stream])
}

# ─── the lambdas, one per backend folder ───

data "archive_file" "api" {
  for_each    = local.lambda_backends
  type        = "zip"
  output_path = "${path.module}/.build/api-${each.key}.zip"

  dynamic "source" {
    # the folder's files, minus what a local test run leaves behind
    for_each = [for f in fileset("${local.api_dir}/${each.key}", "**") : f if !strcontains(f, "__pycache__") && !endswith(f, ".pyc")]
    content {
      content  = file("${local.api_dir}/${each.key}/${source.value}")
      filename = source.value
    }
  }
  # python handlers use the shared local/AWS client factory
  dynamic "source" {
    for_each = each.value.node ? [] : [1]
    content {
      content  = file("${path.module}/../../modules/aws/aws.py")
      filename = "aws.py"
    }
  }
}

resource "aws_iam_role" "api" {
  name = "${local.stack_prefix}-api-lambda"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "api" {
  name = "${local.stack_prefix}-api-lambda"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the directory: the active gerps and their business profiles, operator tables
        Effect = "Allow"
        Action = ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = [
          "arn:aws:dynamodb:${data.aws_region.current.region}:${local.operator_account_id}:table/${local.stack_prefix}-customers",
          "arn:aws:dynamodb:${data.aws_region.current.region}:${local.operator_account_id}:table/${local.stack_prefix}-profiles",
          aws_dynamodb_table.counters.arn, # the economy's counters
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${local.operator_account_id}:*"
      },
    ]
  })
}

module "api" {
  for_each = local.lambda_backends
  source   = "../../modules/terraform/lambda"

  name             = "${local.stack_prefix}-api-${each.key}"
  role             = aws_iam_role.api.arn
  filename         = data.archive_file.api[each.key].output_path
  source_code_hash = data.archive_file.api[each.key].output_base64sha256
  src_dir          = "prod/api_openlyoperated/api/v1/${each.key}"
  handler          = "handler.handler"
  runtime          = each.value.runtime
  timeout          = contains(local.stream_backends, each.key) ? 900 : 30
  memory           = 256
  env_vars = {
    CUSTOMERS_TABLE      = "${local.stack_prefix}-customers"
    PROFILES_TABLE       = "${local.stack_prefix}-profiles"
    COUNTERS_TABLE       = aws_dynamodb_table.counters.name
    PUBLIC_BASE          = "https://api.openlyoperated.biz/v1"
    EVENTS_HTTP_HOST     = local.events_http_host
    EVENTS_REALTIME_HOST = local.events_realtime_host
    EVENTS_KEY           = aws_appsync_api_key.subscribe.key
  }
  log_retention_days = local.config.LOG_RETENTION_DAYS
}

moved {
  from = aws_lambda_function.api["economy_counters"]
  to   = module.api["economy_counters"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.api["events"]
  to   = module.api["events"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.api["gerps"]
  to   = module.api["gerps"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.api["gerps_sources"]
  to   = module.api["gerps_sources"].aws_lambda_function.this
}

resource "aws_lambda_permission" "api" {
  for_each      = local.lambda_backends
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.api[each.key].name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

# ─── the api: resources by depth, methods and integrations off the spec ───

resource "aws_api_gateway_rest_api" "api" {
  name        = "${local.stack_prefix}-api-openlyoperated"
  description = local.api_spec.info.description
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "d1" {
  for_each    = toset(local.depth1)
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = local.leaf[each.key]
}

resource "aws_api_gateway_resource" "d2" {
  for_each    = toset(local.depth2)
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.d1[local.parent[each.key]].id
  path_part   = local.leaf[each.key]
}

resource "aws_api_gateway_resource" "d3" {
  for_each    = toset(local.depth3)
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.d2[local.parent[each.key]].id
  path_part   = local.leaf[each.key]
}

resource "aws_api_gateway_resource" "d4" {
  for_each    = toset(local.depth4)
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.d3[local.parent[each.key]].id
  path_part   = local.leaf[each.key]
}

resource "aws_api_gateway_method" "op" {
  for_each         = local.ops
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = local.op_resource[each.key]
  http_method      = each.value.method
  authorization    = "NONE"
  api_key_required = each.value.keyed # a keyed path (x-keyed) needs x-api-key from the keyed plan; the rest share the stage throttle
}

resource "aws_api_gateway_integration" "op" {
  for_each                = local.ops
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = local.op_resource[each.key]
  http_method             = aws_api_gateway_method.op[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = each.value.stream ? module.api[each.value.backend].response_streaming_invoke_arn : module.api[each.value.backend].invoke_arn
  response_transfer_mode  = each.value.stream ? "STREAM" : "BUFFERED"
  timeout_milliseconds    = each.value.stream ? 900000 : 29000
}

resource "aws_api_gateway_deployment" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  triggers = {
    # any change to the contract or the wiring redeploys the stage
    redeploy = sha1(jsonencode([local.api_spec, { for k, i in aws_api_gateway_integration.op : k => i.uri }]))
  }
  lifecycle {
    create_before_destroy = true
  }
  depends_on = [aws_api_gateway_method.op, aws_api_gateway_integration.op]
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.api.id
  stage_name    = "v1"
  depends_on    = [aws_api_gateway_account.this]

  # the same access log the HTTP stages write; REST's context names differ in two places
  # (resourcePath for routeKey, no authorizer error field)
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId         = "$context.requestId"
      requestTime       = "$context.requestTime"
      httpMethod        = "$context.httpMethod"
      routeKey          = "$context.resourcePath"
      status            = "$context.status"
      responseLatency   = "$context.responseLatency"
      sourceIp          = "$context.identity.sourceIp"
      integrationStatus = "$context.integration.status"
      integrationError  = "$context.integrationErrorMessage"
      error             = "$context.error.message"
    })
  }
}

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${local.stack_prefix}-api-openlyoperated-access"
  retention_in_days = local.config.LOG_RETENTION_DAYS
}

# REST refuses a stage's access log until the account carries a CloudWatch role (an account-level
# setting, one per region; the HTTP stages need none). The read api is the operator's one REST api.
resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.apigw_logs.arn
}

resource "aws_iam_role" "apigw_logs" {
  name = "${local.stack_prefix}-apigw-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_logs" {
  role       = aws_iam_role.apigw_logs.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_cloudwatch_metric_alarm" "gateway_5xx" {
  count               = try(local.config.OPS_ALERTS_TOPIC_ARN, "") != "" ? 1 : 0
  alarm_name          = "${local.stack_prefix}-api-openlyoperated-gateway-5xx"
  alarm_description   = "the read api's stage answered 5xx; the access log /aws/apigateway/${local.stack_prefix}-api-openlyoperated-access carries integrationStatus, integrationError and error for the request"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  dimensions          = { ApiName = aws_api_gateway_rest_api.api.name, Stage = aws_api_gateway_stage.v1.stage_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.config.OPS_ALERTS_TOPIC_ARN]
  ok_actions          = [local.config.OPS_ALERTS_TOPIC_ARN]
}

# the stage throttle: what an anonymous reader shares. A keyed reader's plan lifts it.
resource "aws_api_gateway_method_settings" "v1" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.v1.stage_name
  method_path = "*/*"
  settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }
}

# ─── the meter: a usage plan and one hand-made key (the screen that mints keys is later work) ───

resource "aws_api_gateway_usage_plan" "keyed" {
  name        = "${local.stack_prefix}-api-keyed"
  description = "A key lifts the anonymous throttle and is metered per call; GetUsage per key per day is what the monthly invoice multiplies."
  api_stages {
    api_id = aws_api_gateway_rest_api.api.id
    stage  = aws_api_gateway_stage.v1.stage_name
  }
  throttle_settings {
    rate_limit  = 200
    burst_limit = 400
  }
  quota_settings {
    limit  = 1000000
    period = "MONTH"
  }
}

resource "aws_api_gateway_api_key" "gradienterp" {
  name        = "${local.stack_prefix}-api-gradienterp"
  description = "gradienterp's own key — its reads and the tests"
}

resource "aws_api_gateway_usage_plan_key" "gradienterp" {
  key_id        = aws_api_gateway_api_key.gradienterp.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.keyed.id
}

# ─── the host: api.openlyoperated.biz ───

data "aws_route53_zone" "biz" {
  name = "openlyoperated.biz."
}

resource "aws_acm_certificate" "api" {
  domain_name               = "api.openlyoperated.biz"
  subject_alternative_names = ["events.openlyoperated.biz"]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "api_cert_validation" {
  for_each = {
    for o in aws_acm_certificate.api.domain_validation_options : o.domain_name => {
      name   = o.resource_record_name
      type   = o.resource_record_type
      record = o.resource_record_value
    }
  }
  zone_id         = data.aws_route53_zone.biz.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for r in aws_route53_record.api_cert_validation : r.fqdn]
}

resource "aws_api_gateway_domain_name" "api" {
  domain_name              = "api.openlyoperated.biz"
  regional_certificate_arn = aws_acm_certificate_validation.api.certificate_arn
  security_policy          = "TLS_1_2"
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# the stage is the version and the path carries it: api.openlyoperated.biz/v1/...
resource "aws_api_gateway_base_path_mapping" "v1" {
  api_id      = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.v1.stage_name
  domain_name = aws_api_gateway_domain_name.api.domain_name
  base_path   = "v1"
}

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.biz.zone_id
  name    = "api.openlyoperated.biz"
  type    = "A"
  alias {
    name                   = aws_api_gateway_domain_name.api.regional_domain_name
    zone_id                = aws_api_gateway_domain_name.api.regional_zone_id
    evaluate_target_health = false
  }
}

output "api_url" {
  value = "https://api.openlyoperated.biz/v1"
}

output "api_invoke_url" {
  value = aws_api_gateway_stage.v1.invoke_url
}
