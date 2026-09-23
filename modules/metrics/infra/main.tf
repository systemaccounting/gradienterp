# metrics — the firm's product record.
#
#   an app ──POST /metrics, bearer──▶ record ──emit──▶ the firm's own bus ──rule──▶ Firehose ──Parquet──▶ the cabinet
#   a callsite row (record_metric) ──emit──▶ the same bus                                          metrics/dt=YYYY-MM-DD/
#   the agent ──manage_metrics op=record──▶ the same bus
#   the agent ──manage_metrics op=count|distinct|funnel|retention|query──▶ Athena, the gerp's workgroup, over one Glue table
#
# The store is the cabinet bucket (module.agent's, SSE-KMS), the catalog one Glue database with
# one table (one partition, the day, projected; no crawler), the query engine one Athena workgroup
# whose results land under the cabinet and expire by the bucket's lifecycle rule
# (prod/init_customer). Every read writes a usage row: who paid, which query, how many bytes.

variable "gerp_id" {
  description = "Logical tenant identifier; per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json."
  type        = string
  default     = "gerp"
}

variable "log_retention_days" {
  description = "How long each function's log group keeps its events; root-set (prod/per_customer)."
  type        = number
  default     = 90
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

variable "timezone" {
  description = "The gerp's IANA timezone, GERP_TIMEZONE for modules/clock: a read's window and bins are cut on the firm's own days."
  type        = string
  default     = "UTC"
}

variable "internal_bus_name" {
  description = "The firm's own event bus (modules/events/infra). Every product event is put here; the store rule takes it from here."
  type        = string
}

variable "operator_bus_arn" {
  description = "The operator's bus (prod/platform/operator gerp-operator): rule 2 puts every metrics event on it. A bus target is taken once per event, so the hub, whose forward edge is a second bus target, is not on this path."
  type        = string
}

variable "internal_bus_arn" {
  description = "The same bus, for the events:PutEvents grant and the rule."
  type        = string
}

variable "serve_web" {
  description = "Attach POST /metrics to the per-customer HTTP API. A bool, never the api id's emptiness: a module output is unknown at plan on a fresh account."
  type        = bool
  default     = true
}

variable "server_api_id" {
  description = "The per-customer HTTP API (modules/server). POST /metrics hangs off it when serve_web."
  type        = string
  default     = ""
}

variable "server_api_execution_arn" {
  description = "Execution arn of that API, for the lambda invoke permission."
  type        = string
  default     = ""
}

variable "server_api_endpoint" {
  description = "The API's base url; the url `publish_source` hands an app is this plus /metrics."
  type        = string
  default     = ""
}

variable "storage_bucket" {
  description = "The cabinet bucket (module.agent's uploads bucket): the store under metrics/, the results under metrics/results/."
  type        = string
}

variable "storage_kms_key_arn" {
  description = "The cabinet's CMK: Firehose writes and Athena reads and writes under it."
  type        = string
}

variable "canonical_bucket" {
  description = "The operator's canonical registry bucket (modules/schemas): manage_metrics reads metric_queries.json from it to copy a canonical query into the gerp's table on first use. Deliberate literal default, the schemas module's."
  type        = string
  default     = "gerp-canonical-185369506315"
}

variable "register_with_agent" {
  description = "Register manage_metrics as a tool on the customer's agent gateway."
  type        = bool
  default     = true
}

variable "gateway_id" {
  description = "The agent's gateway this module registers its tool on — module.agent.gateway_id. Read at plan as an input, never from SSM: a fresh account has no parameter to read yet."
  type        = string
  default     = ""
}

variable "gateway_role_arn" {
  description = "The role the gateway invokes tools as — module.agent.gateway_role_arn; the principal on the tool lambda's invoke permission."
  type        = string
  default     = ""
}

locals {
  gerp           = replace(var.gerp_id, "_", "-")
  prefix         = "${var.stack_prefix}-metrics-${local.gerp}"
  env_path       = "/gradienterp/customers/${var.gerp_id}/metrics/env"
  store_prefix   = "metrics/"
  results_prefix = "metrics/results/"
  database       = replace("${var.stack_prefix}_metrics_${var.gerp_id}", "-", "_")
  table          = "metrics"
  settings_table = "${var.stack_prefix}-settings-${local.gerp}"
  schema_table   = "${var.stack_prefix}-schema-${local.gerp}" # the registry table (modules/schemas), where metric_queries rows live
  bucket_arn     = "arn:aws:s3:::${var.storage_bucket}"
  account_id     = data.aws_caller_identity.current.account_id
  region         = data.aws_region.current.region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── the usage table: one row per query — who paid, what ran, how many bytes ───

resource "aws_dynamodb_table" "usage" {
  name         = "${local.prefix}-usage"
  tags         = { "gerp:layer" = "operational" }
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "payer"
  range_key    = "sk"

  attribute {
    name = "payer"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

# ─── the catalog: one database, one table, the date projected (no crawler) ───

resource "aws_glue_catalog_database" "metrics" {
  name        = local.database
  description = "The firm's product record (modules/metrics): one table over the cabinet's metrics/ prefix."
}

resource "aws_glue_catalog_table" "metrics" {
  name          = local.table
  database_name = aws_glue_catalog_database.metrics.name
  table_type    = "EXTERNAL_TABLE"

  # one partition, the ingestion day, projected as a date from the store's first day to today:
  # Athena enumerates the days that exist (tens now, hundreds a year on) and lists each once. Three
  # integer keys (year 2026..2100, month, day) enumerated ~28,000 partitions per query and took
  # 35 to 48 s of engine time to scan nothing (measured 2026-09-19)
  parameters = {
    "classification"              = "parquet"
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.range"         = "2026-09-01,NOW"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "storage.location.template"   = "s3://${var.storage_bucket}/${local.store_prefix}dt=$${dt}/"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.storage_bucket}/${local.store_prefix}"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    columns {
      name = "event"
      type = "string"
    }
    columns {
      name = "subject_id"
      type = "string"
    }
    columns {
      name = "ts"
      type = "string"
    }
    columns {
      name = "via"
      type = "string"
    }
    columns {
      name = "properties"
      type = "map<string,string>"
    }
  }
}

# ─── the store: the bus rule → Firehose → Parquet under the cabinet ───

resource "aws_iam_role" "firehose" {
  name = "${local.prefix}-firehose"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "firehose" {
  name = "${local.prefix}-firehose"
  role = aws_iam_role.firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource = local.bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:AbortMultipartUpload", "s3:GetObject", "s3:PutObject"]
        Resource = "${local.bucket_arn}/${local.store_prefix}*"
      },
      {
        # the cabinet is SSE-KMS
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = var.storage_kms_key_arn
      },
      {
        # the Parquet schema is the table's
        Effect = "Allow"
        Action = ["glue:GetTable", "glue:GetTableVersion", "glue:GetTableVersions"]
        Resource = [
          "arn:aws:glue:${local.region}:${local.account_id}:catalog",
          aws_glue_catalog_database.metrics.arn,
          aws_glue_catalog_table.metrics.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.firehose.arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "firehose" {
  name              = "/aws/kinesisfirehose/${local.prefix}-store"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_stream" "firehose" {
  name           = "delivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

resource "aws_kinesis_firehose_delivery_stream" "store" {
  name        = "${local.prefix}-store"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = local.bucket_arn
    # the date is Firehose's ingestion time. `ts` in the row is the event's own instant, and every
    # read filters on it; the partition bounds what a read lists
    prefix              = "${local.store_prefix}dt=!{timestamp:yyyy-MM-dd}/"
    error_output_prefix = "${local.store_prefix}errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"
    kms_key_arn         = var.storage_kms_key_arn
    # format conversion needs a buffer of at least 64 MiB; the interval is what delivers a quiet
    # firm's events, about a minute after they happen
    buffering_size     = 64
    buffering_interval = 60
    compression_format = "UNCOMPRESSED"

    data_format_conversion_configuration {
      enabled = true
      input_format_configuration {
        deserializer {
          open_x_json_ser_de {}
        }
      }
      output_format_configuration {
        serializer {
          parquet_ser_de {}
        }
      }
      schema_configuration {
        database_name = aws_glue_catalog_database.metrics.name
        table_name    = aws_glue_catalog_table.metrics.name
        role_arn      = aws_iam_role.firehose.arn
        region        = local.region
      }
    }

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.firehose.name
    }
  }
}

resource "aws_iam_role" "events_to_store" {
  name = "${local.prefix}-events-to-store"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "events_to_store" {
  name = "${local.prefix}-events-to-store"
  role = aws_iam_role.events_to_store.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
      Resource = aws_kinesis_firehose_delivery_stream.store.arn
    }]
  })
}

# ─── the second rule: the same event to the platform ───
#
# The emitter puts once; the bus delivers to every rule that matches. Rule 1 (store) is the firm's
# record; this rule sends the same `source = metrics` event to the hub's bus as recorded, through a
# role with PutEvents on it, the way the hub's own forward edge targets the operator bus. An event
# bus in another account takes no input transformer, and none is needed: the operator counts only
# a published gerp's events, off the flag's mirror on its row, and serves a set's size, never a
# member. Every firm's events cross; a firm that is not openly operated counts nothing there.
resource "aws_iam_role" "to_operator" {
  name = "${local.prefix}-to-operator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "to_operator" {
  name = "${local.prefix}-to-operator"
  role = aws_iam_role.to_operator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = var.operator_bus_arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "to_operator" {
  name           = "${local.prefix}-to-operator"
  description    = "every product event on the firm's own bus, to the hub as recorded; the platform counts a published firm's"
  event_bus_name = var.internal_bus_name
  event_pattern  = jsonencode({ source = ["metrics"] })
}

resource "aws_cloudwatch_event_target" "to_operator" {
  rule           = aws_cloudwatch_event_rule.to_operator.name
  event_bus_name = var.internal_bus_name
  target_id      = "hub"
  arn            = var.operator_bus_arn
  role_arn       = aws_iam_role.to_operator.arn
}

resource "aws_cloudwatch_event_rule" "store" {
  name           = "${local.prefix}-store"
  description    = "every product event on the firm's own bus, into the store"
  event_bus_name = var.internal_bus_name
  event_pattern  = jsonencode({ source = ["metrics"] })
}

resource "aws_cloudwatch_event_target" "store" {
  rule           = aws_cloudwatch_event_rule.store.name
  event_bus_name = var.internal_bus_name
  target_id      = "firehose"
  arn            = aws_kinesis_firehose_delivery_stream.store.arn
  role_arn       = aws_iam_role.events_to_store.arn

  # the row the table declares, flat: the detail-type is the event
  input_transformer {
    input_paths = {
      event      = "$.detail-type"
      subject_id = "$.detail.subject_id"
      ts         = "$.detail.ts"
      via        = "$.detail.via"
      properties = "$.detail.properties"
    }
    input_template = "{\"event\": <event>, \"subject_id\": <subject_id>, \"ts\": <ts>, \"via\": <via>, \"properties\": <properties>}"
  }
}

# ─── the query engine: the gerp's own workgroup, results under the cabinet ───

resource "aws_athena_workgroup" "metrics" {
  name          = local.prefix
  description   = "the firm's product record (modules/metrics); the agent's own reads"
  force_destroy = true
  # the payer: the gerp itself. A reader's workgroup (a metered key, #4) is a second one tagged
  # for its own payer, so the scans it causes are attributed by tag as well as by usage row
  tags = { payer = "gerp" }

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${var.storage_bucket}/${local.results_prefix}metrics/"
      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = var.storage_kms_key_arn
      }
    }
  }
}

# ─── the two functions ───

resource "aws_iam_role" "record" {
  name = "${local.prefix}-record"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "record" {
  name = "${local.prefix}-record"
  role = aws_iam_role.record.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # the bearers, read to compare; nothing else on the path
        Effect   = "Allow"
        Action   = ["ssm:GetParametersByPath"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${local.env_path}"
      },
      {
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.internal_bus_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:*"
      },
    ]
  })
}

resource "aws_iam_role" "manage" {
  name = "${local.prefix}-manage"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "manage" {
  name = "${local.prefix}-manage"
  role = aws_iam_role.manage.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # a source's bearer is written and deleted here, never read back
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${local.env_path}/METRICS_TOKEN_*"
      },
      {
        # list_sources: names only. DescribeParameters takes no resource
        Effect   = "Allow"
        Action   = "ssm:DescribeParameters"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = var.internal_bus_arn
      },
      {
        # the reads: this workgroup and no other
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution"]
        Resource = aws_athena_workgroup.metrics.arn
      },
      {
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions", "glue:GetPartition"]
        Resource = [
          "arn:aws:glue:${local.region}:${local.account_id}:catalog",
          aws_glue_catalog_database.metrics.arn,
          aws_glue_catalog_table.metrics.arn,
        ]
      },
      {
        # Athena verifies the results bucket as the caller before it starts a query: a location
        # read and a list with no prefix, so neither takes a prefix condition. Key names of the
        # firm's own cabinet, read by the firm's own tool; the objects stay under the two prefixes
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource = local.bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${local.bucket_arn}/${local.store_prefix}*"
      },
      {
        # Athena writes the result set here as this role; the function reads it back
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.bucket_arn}/${local.results_prefix}*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = var.storage_kms_key_arn
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = aws_dynamodb_table.usage.arn
      },
      {
        # modules/clock reads GERP#timezone: a window is cut on the firm's own calendar
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${local.settings_table}"
      },
      {
        # a query is a metric_queries row in the registry table: read by name, and a canonical one
        # written on first use
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${local.schema_table}"
      },
      {
        # the canonical query file, for the copy on first use
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${var.canonical_bucket}/metric_queries.json"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:*"
      },
    ]
  })
}

locals {
  env = {
    CUSTOMER_ID       = var.gerp_id
    INTERNAL_BUS_NAME = var.internal_bus_name
    METRICS_ENV_PATH  = local.env_path
    METRICS_BASE_URL  = var.server_api_endpoint
    STORE_BUCKET      = var.storage_bucket
    STORE_PREFIX      = local.store_prefix
    USAGE_TABLE       = aws_dynamodb_table.usage.name
    ATHENA_WORKGROUP  = aws_athena_workgroup.metrics.name
    GLUE_DATABASE     = aws_glue_catalog_database.metrics.name
    GLUE_TABLE        = aws_glue_catalog_table.metrics.name
    GERP_TIMEZONE     = var.timezone
    SETTINGS_TABLE    = local.settings_table
    SCHEMA_TABLE      = local.schema_table
    CANONICAL_BUCKET  = var.canonical_bucket
  }
  functions = {
    record         = { role = aws_iam_role.record.arn, timeout = 30 }
    manage_metrics = { role = aws_iam_role.manage.arn, timeout = 60 }
  }
}

module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name               = "${local.prefix}-${each.key}"
  role               = each.value.role
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/metrics/lambdas/${each.key}.zip"
  src_dir            = "modules/metrics/lambdas/${each.key}"
  gerp_id            = var.gerp_id
  timeout            = each.value.timeout
  env_vars           = local.env
  log_retention_days = var.log_retention_days
}

# ─── the door: POST /metrics, no authorizer; the function admits by bearer ───

resource "aws_apigatewayv2_integration" "record" {
  count                  = var.serve_web ? 1 : 0
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn["record"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "record" {
  count              = var.serve_web ? 1 : 0
  api_id             = var.server_api_id
  route_key          = "POST /metrics"
  target             = "integrations/${aws_apigatewayv2_integration.record[0].id}"
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "record" {
  count = var.serve_web ? 1 : 0
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeMetrics"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["record"].name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# ─── the tool on the agent's gateway ───

locals {
  tools = var.register_with_agent ? {
    manage_metrics = {
      arn    = module.fn["manage_metrics"].arn
      name   = module.fn["manage_metrics"].name
      schema = jsondecode(file("${path.module}/../lambdas/manage_metrics/schema.json"))
    }
  } : {}
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tools

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this SSM
  # read via the module's depends_on = [module.agent], making it "known after apply") doesn't
  # force-replace the target. name/description/schema/lambda_arn changes still apply in-place.
  lifecycle {
    ignore_changes = [gateway_identifier]
  }
  name        = replace(each.key, "_", "-")
  description = each.value.schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = each.value.arn

        tool_schema {
          inline_payload {
            name        = each.key
            description = each.value.schema.description
            input_schema {
              type        = each.value.schema.type
              description = each.value.schema.description

              dynamic "property" {
                iterator = prop
                for_each = try(each.value.schema.properties, {})
                content {
                  name        = prop.key
                  type        = try(prop.value.type, "object")
                  description = try(prop.value.description, "")
                  required    = contains(try(each.value.schema.required, []), prop.key)

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
  for_each = local.tools

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = each.value.name
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

output "usage_table_name" {
  description = "One row per query: payer, query id, bytes scanned."
  value       = aws_dynamodb_table.usage.name
}

output "workgroup" {
  description = "The gerp's Athena workgroup for its own reads."
  value       = aws_athena_workgroup.metrics.name
}

output "glue_database" {
  description = "The catalog the table `metrics` lives in."
  value       = aws_glue_catalog_database.metrics.name
}
