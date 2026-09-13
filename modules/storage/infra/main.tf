# storage — the agent's filing cabinet (see storage.md). The `manage_storage` lambda + the semantic-key /
# S3-annotation conventions over the encrypted uploads bucket. The bucket + CMK are OWNED by modules/agent
# (`uploads.tf`); agent stays upstream (every domain module
# registers on its gateway), so storage OPERATES on them (passed in), it doesn't own them. The caption
# lives ON each object as an S3 annotation (no metadata table); the object path is the index.


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
  description = "Logical tenant identifier; per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json; default keeps the module standalone-applyable."
  type        = string
  default     = "gerp"
}

variable "storage_bucket" {
  description = "Name of the encrypted uploads bucket (owned by modules/agent, `uploads.tf`). manage_storage reads/writes objects + their caption annotations here."
  type        = string
}

variable "storage_kms_key_arn" {
  description = "ARN of the uploads bucket CMK (modules/agent). manage_storage needs kms on it — annotations inherit the object's SSE-KMS, so put/get of an object or its annotation touches the key."
  type        = string
}

variable "register_with_agent" {
  description = "Register manage_storage as an MCP tool on the customer's agent gateway. Requires modules/agent applied first (writes /gradienterp/customers/<id>/agent/* SSM). false for an MVP that hasn't wired the agent module."
  type        = bool
  default     = true
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  prefix             = "${var.stack_prefix}-storage-${replace(var.gerp_id, "_", "-")}"
  storage_bucket_arn = "arn:aws:s3:::${var.storage_bucket}"
}

# ─── iam ───

resource "aws_iam_role" "lambda" {
  name = "${local.prefix}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.prefix}-lambda"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # blob CRUD + copy/list; the object-annotation ops carry the caption
        Effect = "Allow"
        # DeleteObjectVersion + ListBucketVersions are separate actions from their unversioned
        # counterparts; without them op=delete leaves a marker and the bytes stay billed.
        Action = [
          "s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket",
          "s3:ListBucketVersions", "s3:DeleteObjectVersion",
          "s3:PutObjectAnnotation", "s3:GetObjectAnnotation", "s3:ListObjectAnnotations", "s3:DeleteObjectAnnotation",
        ]
        Resource = [local.storage_bucket_arn, "${local.storage_bucket_arn}/*"]
      },
      {
        # The approval gate for modules/automation. The agent AUTHORS scripts with this tool
        # (into automations/staged/) and must not be able to bless one — only
        # approve_automation puts content under approved/. A Deny rather than a narrowed
        # Allow because the grant above is bucket-wide, and an explicit Deny beats it.
        #
        # PUT only. Deleting an approved script is how an automation is retired: it stops
        # running, which is a thing the owner may legitimately want. The risk being closed is
        # unreviewed content APPEARING here, not content leaving.
        Effect   = "Deny"
        Action   = "s3:PutObject"
        Resource = "${local.storage_bucket_arn}/automations/approved/*"
      },
      {
        # encrypt/decrypt the blobs + their annotations under the agent-owned per-tenant CMK
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.storage_kms_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── lambda ───

# Node: the annotation API is new, so we bundle the modular @aws-sdk/client-s3 (+ presigner) — small
# (~16MB) vs. boto3's monolith. source_dir zips index.mjs + node_modules (like modules/agent chat).
module "manage_storage" {
  source = "../../terraform/lambda"

  name            = "${local.prefix}-manage_storage"
  role            = aws_iam_role.lambda.arn
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/storage/lambdas/manage_storage.zip"
  src_dir         = "modules/storage/lambdas/manage_storage"
  gerp_id         = var.gerp_id
  handler         = "index.handler"
  runtime         = "nodejs22.x"
  timeout         = 30
  env_vars = {
    STORAGE_BUCKET = var.storage_bucket
    PORTAL_URL     = local.portal_url # op=put under pages/ returns the served link
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.manage_storage
  to   = module.manage_storage.aws_lambda_function.this
}


# ─── agent gateway registration (the non-file ops are agent-callable) ───

locals {
  schema = var.register_with_agent ? jsondecode(file("${path.module}/../lambdas/manage_storage/schema.json")) : null
}

resource "aws_bedrockagentcore_gateway_target" "manage_storage" {
  count = var.register_with_agent ? 1 : 0

  gateway_identifier = var.gateway_id
  lifecycle {
    ignore_changes = [gateway_identifier] # immutable per customer; pin so an agent-image bump doesn't force-replace
  }
  name        = "manage-storage" # AgentCore target name: hyphens, no underscores
  description = local.schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.manage_storage.arn
        tool_schema {
          inline_payload {
            name        = "manage_storage" # agent-facing tool name stays snake_case
            description = local.schema.description
            input_schema {
              type        = local.schema.type
              description = local.schema.description

              dynamic "property" {
                iterator = prop
                for_each = local.schema.properties
                content {
                  name        = prop.key
                  type        = prop.value.type
                  description = try(prop.value.description, "")
                  required    = contains(try(local.schema.required, []), prop.key)

                  # array property (e.g. tags) → describe its string items
                  dynamic "items" {
                    for_each = prop.value.type == "array" ? [prop.value.items] : []
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
    gateway_iam_role {} # gateway invokes under its own role; same-account dispatch
  }
}

resource "aws_lambda_permission" "gateway_invoke" {
  count = var.register_with_agent ? 1 : 0

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.manage_storage.name
  principal           = var.gateway_role_arn
  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal] # immutable per customer (same rationale as gateway_identifier)
  }
}

# ═══ inspect_document — the one tool that OPENS an object (Textract; see ../AGENTS.md) ═══
# Separate lambda + role from manage_storage: it reads bytes + calls Textract, so textract / kms-decrypt
# stay OFF the filing role (least privilege — file/move/delete can't invoke Textract). Same bucket + CMK
# (passed in). Dual-registered like manage_storage: gateway tool (read-it-back on a filed key) + an
# gateway-registered: inspect a document by `key` (a filed doc or a portal submission file).

resource "aws_iam_role" "inspect" {
  name = "${local.prefix}-inspect-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "inspect" {
  name = "${local.prefix}-inspect-lambda"
  role = aws_iam_role.inspect.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # read the object bytes + read/write the `inspection` annotation cache; NO put/delete of objects
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectAnnotation", "s3:PutObjectAnnotation"]
        Resource = ["${local.storage_bucket_arn}/*"]
      },
      {
        # decrypt the object; encrypt the annotation (inherits the object's SSE-KMS)
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
        Resource = var.storage_kms_key_arn
      },
      {
        # AnalyzeExpense has no resource-level ARN; the lambda only ever passes bytes from its own bucket
        Effect   = "Allow"
        Action   = ["textract:AnalyzeExpense"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

module "inspect_document" {
  source = "../../terraform/lambda"

  name               = "${local.prefix}-inspect_document"
  role               = aws_iam_role.inspect.arn
  artifact_bucket    = var.artifact_bucket
  artifact_key       = "modules/storage/lambdas/inspect_document.zip"
  src_dir            = "modules/storage/lambdas/inspect_document"
  gerp_id            = var.gerp_id
  handler            = "index.handler"
  runtime            = "nodejs22.x"
  timeout            = 60
  env_vars           = { STORAGE_BUCKET = var.storage_bucket }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.inspect_document
  to   = module.inspect_document.aws_lambda_function.this
}


locals {
  inspect_schema = var.register_with_agent ? jsondecode(file("${path.module}/../lambdas/inspect_document/schema.json")) : null
}

resource "aws_bedrockagentcore_gateway_target" "inspect_document" {
  count = var.register_with_agent ? 1 : 0

  gateway_identifier = var.gateway_id
  lifecycle {
    ignore_changes = [gateway_identifier] # immutable per customer; pin so an agent-image bump doesn't force-replace
  }
  name        = "inspect-document" # AgentCore target name: hyphens, no underscores
  description = local.inspect_schema.description

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.inspect_document.arn
        tool_schema {
          inline_payload {
            name        = "inspect_document" # agent-facing tool name stays snake_case
            description = local.inspect_schema.description
            input_schema {
              type        = local.inspect_schema.type
              description = local.inspect_schema.description

              dynamic "property" {
                iterator = prop
                for_each = local.inspect_schema.properties
                content {
                  name        = prop.key
                  type        = prop.value.type
                  description = try(prop.value.description, "")
                  required    = contains(try(local.inspect_schema.required, []), prop.key)
                }
              }
            }
          }
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {} # gateway invokes under its own role; same-account dispatch
  }
}

resource "aws_lambda_permission" "inspect_gateway_invoke" {
  count = var.register_with_agent ? 1 : 0

  statement_id_prefix = "AllowAgentGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.inspect_document.name
  principal           = var.gateway_role_arn
  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal] # immutable per customer (same rationale as gateway_identifier)
  }
}

# ─── outputs ───

output "manage_storage_fn_name" {
  value = module.manage_storage.name
}

output "inspect_document_fn_name" {
  value = module.inspect_document.name
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
