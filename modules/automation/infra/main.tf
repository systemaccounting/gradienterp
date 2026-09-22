# ─────────────────────────────────────────────────────────────────────────────
# automation — custom code the firm's agent writes, running against the firm's own
# module tools. `modules/cmd` is the other kind (internet, no tools); this is the
# kind that reaches the ERP surface, and the two never share a role.
#
# Approval is a PATH: `automate` can read `automations/approved/modules/` and nothing
# else, so an unapproved script is not where GetObject looks. `approve_automation` is
# the only principal that can write under approved/ — manage_storage carries an
# explicit Deny there (modules/storage), so the turn that authors a script cannot
# bless it.
#
# The tool allowlist is one local generating BOTH the name→ARN map handed to the runner
# and the lambda:InvokeFunction resources in its identity policy, so the two cannot drift.
#
# Three lambdas, three roles, split on reach rather than on verb count: `automate` holds
# the tool allowlist, `approve_automation` is the only writer of approved/, and
# `review_automation` is the only writer of the review record. So an agent can carry a
# passing review's ticket and can never write one.
#
# TWO KINDS, one pipeline. `modules` runs through this module's own `automate` (tool
# allowlist, no internet); `external` runs through modules/cmd (internet, env secrets, no
# tools). Both read only from their kind's approved prefix, so a scheduled run of either can
# only execute bytes that passed review — the gate is the prefix, not a rule about who may
# schedule what.
# ─────────────────────────────────────────────────────────────────────────────


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
  type = string
}

variable "stack_prefix" {
  type = string
}

variable "cmd_enabled" {
  description = "Whether modules/cmd is provisioned for this gerp. Off ⇒ the external kind has no runner and schedule_automation says so instead of creating a schedule that cannot fire."
  type        = bool
  default     = false
}

variable "register_with_agent" {
  type    = bool
  default = true
}

variable "serve_web" {
  description = "Publish the automation routes on the server api (server_api_id). A bool the root sets, so the plan knows the count before the api exists."
  type        = bool
  default     = false
}

variable "server_api_id" {
  description = "modules/server's HTTP API. Empty leaves the web door unbuilt; a script is then reachable only through the gateway tool."
  type        = string
  default     = ""
}

variable "server_api_execution_arn" {
  type    = string
  default = ""
}

variable "server_api_endpoint" {
  description = "The gerp's HTTP API base url (modules/server api_endpoint). manage_hooks composes the hook's url from it; empty means the tool answers with the path alone."
  type        = string
  default     = ""
}

variable "owner_authorizer_id" {
  description = "Owner JWT authorizer on that API. Empty falls back to AWS_IAM, matching modules/settings."
  type        = string
  default     = ""
}

variable "artifact_bucket" {
  description = "Versioned operator artifact bucket lambda code deploys from (scripts/deploy.sh pushes; org-read). Deliberate literal default — the op_event_bus_arn convention."
  type        = string
  default     = "gerp-artifacts-185369506315"
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  gerp   = replace(var.gerp_id, "_", "-")
  prefix = "${var.stack_prefix}-automation-${local.gerp}"

  # the filing cabinet — agent-owned bucket, constructed name (a module ref would cycle
  # through this module's depends_on = [module.agent]); same shape modules/cmd uses.
  cabinet_bucket = "${var.stack_prefix}-agent-${local.gerp}-uploads-${data.aws_caller_identity.current.account_id}"
  kms_alias      = "alias/agentcore_${replace(var.gerp_id, "-", "_")}_uploads"

  staged_prefix   = "automations/staged/"
  approved_prefix = "automations/approved/"
  modules_prefix  = "automations/approved/modules/"
  # the web door's route table — one object per url, written by `manage_storage op=put`. Not
  # "published": that word is the openly-operated public feed, and these are owner-JWT only.
  routes_prefix = "automations/routes/"

  # ── how a script reaches tools ──
  # Through this firm's own gateway, by the same route as the agent that wrote it. There is no
  # allowlist: `bedrock-agentcore:InvokeGateway` on one ARN is the whole grant, and the address
  # of a tool is composed from its name (`<tool-with-hyphens>___<tool>`) rather than looked up.
  #
  # It used to be an enumerated name→ARN map here. That map was an operator DEPLOY every time a
  # firm found something new to automate, which is the deployment cycle this module exists to
  # remove — and it was never the boundary it looked like, because the agent holds
  # `bedrock-agentcore:*` on the same gateway and can already call every registered tool. A
  # reviewed script calling one is the same reach without the turn.
  #
  # What bounds a script is the cold review. When a Cedar engine is attached to the gateway
  # (`modules/agent/TODO.md` § phase 5) it bounds it further, and for free — the caller is a
  # distinct principal (`AgentCore::IamEntity` carrying this role's ARN), so a policy can say
  # something about unattended scripts it does not say about a turn the owner is watching.

  # `create_inc_from_log`'s own reach — the tasks tool, invoked DIRECTLY. That lambda is platform
  # code opening an incident, not firm code, so it does not go through the gateway: there is no
  # untrusted script to bound and no policy question to answer about it.
  tasks_fn = "${var.stack_prefix}-tasks-${local.gerp}-manage_tasks"

  # the one resource `automate` is granted on, and the endpoint it posts to. Read from the SSM
  # modules/agent publishes rather than constructed — the gateway id carries a random suffix.
  gateway_arn = var.register_with_agent ? var.gateway_arn : ""
  gateway_url = var.register_with_agent ? var.gateway_url : ""

  schedule_group = "${local.prefix}-schedules"

  # the two kind runners. modules is this module's own; external is modules/cmd, reached by
  # CONSTRUCTED name rather than a module reference — cmd is count-gated on CMD_ENABLED, and a
  # ref would make this module fail to plan whenever that flag is off.
  automate_fn_arn = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${local.prefix}-automate"
  cmd_fn_arn      = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-cmd-${local.gerp}-cmd"

  # outbound mail (modules/agent, mail.tf) — constructed by name for the same reason as the
  # others: a module ref would drag this module's plan behind agent's.
  send_email_fn_arn = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.stack_prefix}-mail-${local.gerp}-send_email"

  # The firm's own credentials for systems this platform does not run — an API key for a supplier, a
  # token for a carrier. A script reads one at the moment it uses it; the name is what appears in the
  # source and the value never does.
  #
  # The SAME path `modules/cmd` reads. A credential is the firm's, not the runner's, and a firm that
  # writes a `.sh` today and a `.py` tomorrow should not collect it twice and rotate it once. What
  # each runner may DO is each one's role and stays separate.
  #
  # Not the `/secrets/` path: those are stack-derived — webhook signing secrets, the provider keys
  # the platform itself uses — and nothing a script reaches should be able to enumerate them.
  automation_env_path = "/gradienterp/customers/${var.gerp_id}/automation/env"

  functions = toset([
    "automate", "approve_automation", "review_automation", "manage_automation",
    "create_inc_from_log", "manage_machines", "machine_failed",
    "manage_hooks",
  ])

  # both are trigger-driven rather than agent-callable — no gateway target
  gateway_tools = setsubtract(local.functions, ["create_inc_from_log", "machine_failed"])

  # roles split on what each one may REACH — not on verb count. The runner holds the
  # tool allowlist; approve is the only writer of approved/; review is the only writer of
  # the review record; schedules is the only principal that can put anything in the group,
  # and it carries the listing reads too — one function serves schedule and read ops.
  function_roles = {
    automate            = aws_iam_role.automate.arn
    approve_automation  = aws_iam_role.approve.arn
    review_automation   = aws_iam_role.review.arn
    manage_automation   = aws_iam_role.schedules.arn
    create_inc_from_log = aws_iam_role.incidents.arn
    manage_machines     = aws_iam_role.sfn.arn
    # hooks: writes a route record and a secret, reads back neither script nor secret
    manage_hooks = aws_iam_role.hooks.arn
    # NOT the sfn role — reporting on machines must not carry the grant that deploys them
    machine_failed = aws_iam_role.machine_reporter.arn
  }
}

# ─── IAM: hooks — manage_hooks ───
#
# A hook is a route record plus a bearer secret. This role writes both and reads neither back: it
# may put and delete under the routes prefix, put and delete parameters under the firm's automation
# env path, and confirm a script exists under approved/modules/ (a head, not a read). It cannot read
# a secret's value — the tool returns the one it minted, and nothing else can ask for it again.
resource "aws_iam_role" "hooks" {
  name = "${local.prefix}-hooks"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "hooks" {
  name = "${local.prefix}-hooks"
  role = aws_iam_role.hooks.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.routes_prefix}*"
      },
      {
        # HeadObject is GetObject's permission; a 403 on a missing key reads the same as a 404
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.modules_prefix}*"
      },
      {
        # the cabinet is SSE-KMS: a record written is encrypted, a script header is decrypted
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.automation_env_path}/HOOK_TOKEN_*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── the schedule group ───
#
# Terraform owns the GROUP and none of its entries — manage_automation (op: schedule) adds those, so a
# firm's automations are not terraform state. Three things fall out: ListSchedules on the
# group is exactly the automations, `scheduler:CreateSchedule` scoped to the group ARN means
# nothing lands in it except through the tool, and deleting the group deletes every schedule
# in it, so teardown leaves no orphans.

resource "aws_scheduler_schedule_group" "automations" {
  name = local.schedule_group
}

# the identity EventBridge Scheduler assumes to fire a schedule. Its only power is invoking
# the runner — a schedule cannot reach anything the runner cannot.
resource "aws_iam_role" "scheduler_target" {
  name = "${local.prefix}-scheduler-target"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_target" {
  name = "${local.prefix}-scheduler-target"
  role = aws_iam_role.scheduler_target.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # one entry per LANE runner. Scheduler can fire the modules runner and the external one
      # (cmd) and nothing else — an allowlist of two, the shape modules/calendar now uses too.
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [local.automate_fn_arn, local.cmd_fn_arn]
    }]
  })
}

# ─── the review record ───
#
# One row per review, pass or not. The findings are what the owner reads and what an
# escalation carries, so the record outlives the capability — no TTL. A PASSING review is
# additionally spendable once, within `spendable_until`, which approve_automation consumes.
#
# pk = script so one script's reviews are a query rather than a scan.

resource "aws_dynamodb_table" "reviews" {
  name         = "${local.prefix}-reviews"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "script"
  range_key    = "review_id"

  attribute {
    name = "script"
    type = "S"
  }

  attribute {
    name = "review_id"
    type = "S"
  }
}

data "aws_kms_alias" "cabinet" {
  name = local.kms_alias
}

# ─── IAM: automate — the runner. This role IS the blast radius of firm-written code ───


# ─── the closure credential ───
#
# A script that reaches the end of an unpaid sequence asks the OPERATOR account to close a gerp: mark
# the customer row and start the build that exports and destroys it. Neither is reachable from here,
# and neither should be — the authority belongs to the operator.
#
# So it is assumed, not stored. `prod/platform/operator/closure_requester.tf` holds a role granting
# exactly those two acts, trusting whichever accounts it is told to; this is the other half, saying
# which of THIS account's principals may use it. Both halves are empty by default, so the path does
# not exist until two applies deliberately open it.
#
# On `automate`'s role, because a script is what uses it. Some effects have no tool and are not going
# to get one — acting on a credential for a system this platform does not run — and for those the
# ROLE is the boundary: a script reaches exactly what this grant reaches, which is one operator role
# that can start one build and update one row. Scoped in terraform, reviewable there, rather than
# negotiated per script.
#
# It is the same shape a FIRM uses for its own credentialed effects, which is why it is worth having
# rather than routing around: a firm puts its credential at its own parameter path and its script
# reads it. Nothing here is a pattern only the operator can follow.

variable "customers_table_name" {
  description = "The OPERATOR account's customer table, written by a closure script through the assumed role. Only meaningful where closure_requester_role_arn is set."
  type        = string
  default     = ""
}

variable "close_build_project" {
  description = "CodeBuild project that exports a gerp then destroys it. Empty records the request and tears nothing down — the operator's own switch, independent of whether a person approved a particular closure."
  type        = string
  default     = ""
}

variable "close_account_fn_arn" {
  description = "tower's close_account lambda in the operator account, invoked through the closure role fifteen days after the build. Empty everywhere the role arn is."
  type        = string
  default     = ""
}

variable "closure_invoker_role_arn" {
  description = "The owner web app's role, admitted to invoke `automate` so POST /api/gerps/close can hand a requested closure to the closure scripts. Empty (the default) admits nobody — set only on the operator's own gerp."
  type        = string
  default     = ""
}

variable "closure_requester_role_arn" {
  description = "Operator-account role this gerp's automations may assume to request a gerp closure (prod/platform/operator, closure_requester.tf). Empty (the default) means no automation here can ask, which is what every gerp but the operator's own wants."
  type        = string
  default     = ""
}

resource "aws_iam_role_policy" "automation_env" {
  name = "${local.prefix}-automation-env"
  role = aws_iam_role.automate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # The one path, the same shape `modules/cmd` uses for its own. Scoped by prefix, so a script
      # reads what the firm put there for scripts and cannot walk to anything else — the boundary
      # is this resource line, not a rule about which library a script may import.
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:GetParametersByPath"]
      Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.automation_env_path}*"
      }, {
      # modules/mcp: the vendors' gateway url and the firm's client, so a rule that names a
      # vendor tool calls that gateway as the firm. Its own path, beside the vault, not in it.
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:GetParametersByPath"]
      Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/mcp/*"
      }, {
      # the client secret there is a SecureString under the default aws/ssm key
      Effect    = "Allow"
      Action    = "kms:Decrypt"
      Resource  = "*"
      Condition = { StringEquals = { "kms:ViaService" = "ssm.${data.aws_region.current.region}.amazonaws.com" } }
    }]
  })
}

resource "aws_iam_role_policy" "assume_closure_requester" {
  count = var.closure_requester_role_arn == "" ? 0 : 1
  name  = "${local.prefix}-assume-closure-requester"
  role  = aws_iam_role.automate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = var.closure_requester_role_arn
    }]
  })
}

resource "aws_iam_role" "automate" {
  name = "${local.prefix}-automate"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "automate" {
  name = "${local.prefix}-automate"
  role = aws_iam_role.automate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        # the approved prefix and nothing else under automations/ — this is the approval
        # gate, expressed as a read permission rather than as a flag anyone checks
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.modules_prefix}*"
      },
      {
        # the route records. A record only NAMES a script — the scripts themselves stay under
        # the approved prefix above, which `manage_storage` is denied PutObject on — so serving a
        # url cannot reach bytes review has not passed.
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.routes_prefix}*"
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"

      },
      ], [
      {
        # the whole tool grant. A script that skips ctx and reaches for boto3 gets exactly this:
        # it can ask this firm's gateway to run a tool, and it cannot invoke a lambda directly,
        # read a secret, or touch a table.
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeGateway"
        Resource = local.gateway_arn != "" ? local.gateway_arn : "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:gateway/none"
      },
    ])
    # deliberately absent: lambda:InvokeFunction (the gateway is the only route), ssm:GetParameter
    # (one grant on the secrets path and any script reads the whole vault), dynamodb:*, ses:*,
    # sts:AssumeRole, any iam:*.
  })
}

# ─── IAM: approve_automation — the only writer of approved/ ───

resource "aws_iam_role" "approve" {
  name = "${local.prefix}-approve"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "approve" {
  name = "${local.prefix}-approve"
  role = aws_iam_role.approve.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectAnnotation"]
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.staged_prefix}*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectAnnotation", "s3:GetObject"]
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.approved_prefix}*"
      },
      {
        # Encrypt as well as GenerateDataKey: approving is a CopyObject between two SSE-KMS
        # objects, which decrypts the source and re-encrypts at the destination
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        # spend a review — UpdateItem only. approve can never write a new one, so an
        # approval always traces back to a review it did not perform itself.
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = aws_dynamodb_table.reviews.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── IAM: review_automation — reads staged, wakes a cold turn, creates tickets ───

resource "aws_iam_role" "review" {
  name = "${local.prefix}-review"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "review" {
  name = "${local.prefix}-review"
  role = aws_iam_role.review.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectAnnotation"]
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.staged_prefix}*"
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        # record only. No UpdateItem, so a review cannot spend the ticket it just wrote,
        # and no write path exists for anything else — the agent can hold one, never make one.
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = aws_dynamodb_table.reviews.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      ], var.register_with_agent ? [
      {
        # the cold turn: a fresh session on the gerp's own runtime
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = ["${local.runtime_arn}", "${local.runtime_arn}/*"]
      },
    ] : [])
  })
}

# ─── IAM: schedules — the only principal that can put anything in the group ───

resource "aws_iam_role" "schedules" {
  name = "${local.prefix}-schedules"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "schedules" {
  name = "${local.prefix}-schedules"
  role = aws_iam_role.schedules.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # scoped to the group: a schedule ARN is schedule/<group>/<name>, so nothing can be
        # created, changed or removed outside it
        Effect = "Allow"
        Action = [
          "scheduler:CreateSchedule", "scheduler:UpdateSchedule",
          "scheduler:DeleteSchedule", "scheduler:GetSchedule",
        ]
        Resource = "arn:aws:scheduler:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:schedule/${local.schedule_group}/*"
      },
      {
        # CreateSchedule hands Scheduler a role to fire with; passing one needs PassRole,
        # and this is the only role it may pass
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.scheduler_target.arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" }
        }
      },
      {
        # refuse to schedule a script that is not approved — the read is the check
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}/${local.approved_prefix}*"
      },
      {
        # ListSchedules takes NO resource type and NO condition keys (service authorization
        # reference), so it can only be granted on *. The group is a filter the tool always
        # passes, not a boundary. GetSchedule, which returns the payload, IS group-scoped above.
        Effect   = "Allow"
        Action   = "scheduler:ListSchedules"
        Resource = "*"
      },
      {
        # a script with no schedule is still an automation, so listing joins the objects;
        # bucket-wide ListBucket matches manage_storage's existing posture
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:aws:s3:::${local.cabinet_bucket}"
      },
      {
        # "why is this running, and who let it" — the review record answers it (op: get)
        Effect   = "Allow"
        Action   = "dynamodb:Query"
        Resource = aws_dynamodb_table.reviews.arn
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_alias.cabinet.target_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# ─── IAM: incidents — the privileged half of the failure chain ───

resource "aws_iam_role" "incidents" {
  name = "${local.prefix}-incidents"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "incidents" {
  name = "${local.prefix}-incidents"
  role = aws_iam_role.incidents.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        # the tasks tool only — the incident is the state, so this is all it needs
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${local.tasks_fn}"
      },
      {
        # the tenant blob carries owner_email. NOT the secrets path — that stays out of
        # every role in this module.
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}"
      },
      {
        # the failure notice goes out through the firm's own mail server, like every other mail
        # a gerp sends. Nothing in this module holds a sending credential.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.send_email_fn_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      ], var.register_with_agent ? [
      {
        # the diagnose poke. It cannot approve anything — that needs a review ticket.
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = ["${local.runtime_arn}", "${local.runtime_arn}/*"]
      },
    ] : [])
  })
}

# ─── lambdas ───


module "fn" {
  for_each = local.functions
  source   = "../../terraform/lambda"

  name            = "${local.prefix}-${each.key}"
  role            = local.function_roles[each.key]
  artifact_bucket = var.artifact_bucket
  artifact_key    = "modules/automation/lambdas/${each.key}.zip"
  src_dir         = "modules/automation/lambdas/${each.key}"
  gerp_id         = var.gerp_id
  timeout         = each.key == "approve_automation" ? 30 : 300
  memory          = each.key == "approve_automation" ? 256 : 512
  env_vars = {
    CUSTOMER_ID     = var.gerp_id
    CABINET_BUCKET  = local.cabinet_bucket
    STAGED_PREFIX   = local.staged_prefix
    APPROVED_PREFIX = each.key == "automate" ? local.modules_prefix : local.approved_prefix
    ROUTES_PREFIX   = local.routes_prefix
    REVIEWS_TABLE   = aws_dynamodb_table.reviews.name
    OWNER_SUB_PARAM = "/gradienterp/customers/${var.gerp_id}/owner_sub" # /automate/ answers only this sub

    SCHEDULE_GROUP        = aws_scheduler_schedule_group.automations.name
    SCHEDULER_TARGET_ROLE = aws_iam_role.scheduler_target.arn
    # where a script reads the firm's own credentials from
    AUTOMATION_ENV_PATH = local.automation_env_path
    HOOKS_BASE_URL      = var.server_api_endpoint

    # what a script assumes to reach the operator account, plus what it acts on there. Empty
    # everywhere but the operator's own gerp, and an empty arn is what makes the script a no-op.
    CLOSURE_REQUESTER_ROLE_ARN = var.closure_requester_role_arn
    CUSTOMERS_TABLE            = var.customers_table_name
    CLOSE_BUILD_PROJECT        = var.close_build_project
    CLOSE_ACCOUNT_FN           = var.close_account_fn_arn
    GATEWAY_URL                = local.gateway_url

    AUTOMATE_FUNCTION_ARN = local.automate_fn_arn
    CMD_FUNCTION_ARN      = var.cmd_enabled ? local.cmd_fn_arn : ""

    TASKS_FN                = local.tasks_fn
    TENANT_PARAM            = "/gradienterp/customers/${var.gerp_id}"
    SEND_EMAIL_FUNCTION_ARN = local.send_email_fn_arn

    AGENT_RUNTIME_ENDPOINT_ARN = local.runtime_endpoint_arn

    # the machines kind (machines.tf) — the definition comes from the approved prefix, the
    # execution role is the fence, and every machine logs to one group because they are created
    # at runtime rather than by terraform
    MACHINES_PREFIX = "${local.approved_prefix}machines/"
    SFN_ROLE_ARN    = aws_iam_role.machine.arn
    # the ":*" qualifier is required — CreateStateMachine refuses a bare log-group ARN
    SFN_LOG_GROUP_ARN = "${aws_cloudwatch_log_group.machines.arn}:*"
    SFN_NAME_PREFIX   = "${local.prefix}-"
    # manage_automation joins deployed state onto approved machines (op: list) and builds a
    # machine target from it. Both need the account to construct an arn.
    AWS_ACCOUNT_ID = data.aws_caller_identity.current.account_id
  }
  log_retention_days = var.log_retention_days
}

moved {
  from = aws_lambda_function.fn["automate"]
  to   = module.fn["automate"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["approve_automation"]
  to   = module.fn["approve_automation"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["review_automation"]
  to   = module.fn["review_automation"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["manage_automation"]
  to   = module.fn["manage_automation"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["create_inc_from_log"]
  to   = module.fn["create_inc_from_log"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["manage_machines"]
  to   = module.fn["manage_machines"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["machine_failed"]
  to   = module.fn["machine_failed"].aws_lambda_function.this
}

moved {
  from = aws_lambda_function.fn["manage_hooks"]
  to   = module.fn["manage_hooks"].aws_lambda_function.this
}


# ─── the failure chain ───
#
# The runner LOGS its outcome and does nothing else about it. A subscription filter carries
# the line here, where the privileged half lives — writing a task, mailing the owner, waking
# an agent. Nothing is created at runtime: one static resource, and dedupe is a query against
# the open incident rather than an alarm's state machine.

# The runner's log group is created by its lambda module (`module.fn["automate"]`); the filter
# below attaches by name, and the module's group exists before the function does.
removed {
  from = aws_cloudwatch_log_group.automate
  lifecycle {
    destroy = false
  }
}

resource "aws_cloudwatch_log_subscription_filter" "outcomes" {
  name            = "${local.prefix}-outcomes"
  log_group_name  = module.fn["automate"].log_group
  filter_pattern  = "{ $.incident = \"*\" }"
  destination_arn = module.fn["create_inc_from_log"].arn

  depends_on = [aws_lambda_permission.logs_invoke]
}

resource "aws_lambda_permission" "logs_invoke" {
  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window
  # where the principal is unauthorised — a call landing in that gap would be a bare 403.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowLogSubscription"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["create_inc_from_log"].name
  principal           = "logs.amazonaws.com"
  source_arn          = "${module.fn["automate"].log_group_arn}:*"
}

# ─── gateway registration ───

# gated the same way the target registration is: a gerp with no agent module has no gateway, and
# `automate` answers "no gateway configured" rather than being unable to plan.
locals {
  # the SSM value is the ENDPOINT arn (…:runtime/<id>/runtime-endpoint/<name>); IAM wants
  # the runtime it hangs off, so review's grant covers the runtime and its endpoints
  runtime_endpoint_arn = var.register_with_agent ? var.agent_runtime_endpoint_arn : ""
  runtime_arn          = split("/runtime-endpoint/", local.runtime_endpoint_arn)[0]

  tool_schemas = {
    for k in local.gateway_tools :
    k => jsondecode(file("${path.module}/../lambdas/${k}/schema.json"))
    if var.register_with_agent
  }
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_schemas

  gateway_identifier = var.gateway_id
  # gateway id is immutable per customer — pin it so an agent-image bump (which defers this
  # SSM read via depends_on = [module.agent]) doesn't force-replace the target.
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

  statement_id_prefix = "AllowGatewayInvoke"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn[each.key].name
  principal           = var.gateway_role_arn

  lifecycle {
    # AddPermission has no update, so a change replaces this; created before
    # destroyed so no call lands in a window where the principal is unauthorised.
    create_before_destroy = true
    ignore_changes        = [principal]
  }
}

# ─── outputs ───

output "lambda_functions" {
  value = { for k, fn in module.fn : k => fn.name }
}

output "automate_role_arn" {
  value = aws_iam_role.automate.arn
}

output "approve_role_arn" {
  description = "The only principal that may write under automations/approved/. per_customer names it in the cabinet's bucket policy, so the prefix is closed to every OTHER principal — including roles written later that hold bucket-wide access."
  value       = aws_iam_role.approve.arn
}

# The owner web app hands a requested closure to `closure/begin.py` by invoking `automate` directly
# — cross-account by resource policy, the callee naming the caller, the same way modules/payments
# admits it for the card-setup pair. Only the operator's own gerp sets the arn.
resource "aws_lambda_permission" "closure_invoker" {
  count = var.closure_invoker_role_arn == "" ? 0 : 1
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowBffClosure"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["automate"].name
  principal           = var.closure_invoker_role_arn
}

variable "gateway_arn" {
  description = "module.agent.gateway_arn."
  type        = string
  default     = ""
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

variable "gateway_url" {
  description = "module.agent.gateway_url."
  type        = string
  default     = ""
}

variable "agent_runtime_endpoint_arn" {
  description = "module.agent.runtime_endpoint_arn — the endpoint a poke invokes."
  type        = string
  default     = ""
}

# The owner routes read who the owner is: the one parameter, nothing beside it.
resource "aws_iam_role_policy" "owner_sub" {
  name = "${local.prefix}-automate-owner-sub"
  role = aws_iam_role.automate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/gradienterp/customers/${var.gerp_id}/owner_sub"
    }]
  })
}
