# modules/terraform — shared terraform blocks

The home for terraform a module calls rather than an ERP module: what every function, every
table or every queue owns, written once. `modules/aws`, `modules/events` and `modules/journal`
are the same idea for python.

## current features

- `lambda/` — one function and what every function owns: the function (the code from
  `artifact_key` pinned to the bucket's latest version (the bucket is the region's: the bare
  `artifact_bucket` in us-east-1, `<artifact_bucket>-<region>` elsewhere, filled by S3
  replication from tower's), or from `filename` + `source_code_hash`
  for an `archive_file` the applier builds — the operator stacks; the `gerp:src-dir` tag
  `deploy.sh` walks; `GERP_ID`/`CUSTOMER_ID` merged into the env when `gerp_id` is set; `memory`
  128 unless the caller says more; JSON log format, `log_level` INFO for the function's own
  lines and WARN for the runtime's), its log group at `log_retention_days` (the function depends
  on it, so the group is owned before the first invoke), and two metric filters on its `[ERROR]`
  lines: `ErrorLines` into `gerp/app/<gerp>` (`gerp/app/operator` when `gerp_id` is empty) and
  `ErrorLinesByKind` by function, `kind` and `category`. No alarm lives here: a raise is counted
  by the account's dimensionless `AWS/Lambda Errors` and a caught failure by `ErrorLines`, and
  the ONE alarm on each is the root's (`prod/per_customer`, `prod/tower`) — alarms bill per
  alarm, and the log group names the function. Outputs `arn`, `name`, `invoke_arn`,
  `qualified_arn`, `response_streaming_invoke_arn`, `log_group`.
  Every function in the repo goes through it
  (`tests/tower/local/test_codebuild_source.py`).

- `stream/` — one DynamoDB stream mapping and what every mapping owns: the record-level retry
  (`ReportBatchItemFailures`, bisect, `retries` = 3) and the queue a record lands on when it
  keeps failing (`<name>-failed`, 14 days, the function's role granted SendMessage). The handler
  goes through `aws.stream_batch(event, one)`: `one(record)` raises on a failure and returns on a
  skip, and the raise becomes an `[ERROR]` line and one `batchItemFailures` entry — a raise from
  the handler itself would block the shard for 24 hours, and a return after a failed record
  consumes it. `filter_patterns` takes the mapping's jsonencoded patterns. `<name>-parked` alarms
  on the queue's visible messages to `ops_alerts_topic_arn` and stays in ALARM until the record
  is redriven or removed; a person reads the record off the queue. Outputs
  `failed_queue_arn`, `failed_queue_url`, `mapping_uuid`. Proven live 2026-09-08: a record settle
  could not settle was invoked four times over 90 s and parked with its sequence number.

## how a module calls it

Once per function, `for_each` over the module's own map — the knobs beside the name, the role
and every reference staying with the caller:

    locals {
      functions = {
        manage_stock = { timeout = 60 }
        reserve      = {}
      }
    }

    module "fn" {
      for_each = local.functions
      source   = "../../terraform/lambda"
      name     = "${local.prefix}-${each.key}"
      role     = aws_iam_role.lambda.arn
      artifact_bucket      = var.artifact_bucket
      artifact_key         = "modules/inventory/lambdas/${each.key}.zip"
      src_dir              = "modules/inventory/lambdas/${each.key}"
      gerp_id              = var.gerp_id
      timeout              = try(each.value.timeout, 30)
      env_vars             = local.env_vars
      log_retention_days   = var.log_retention_days
      ops_alerts_topic_arn = var.ops_alerts_topic_arn
    }

    module "settle_stream" {
      source       = "../../terraform/stream"
      name         = "${local.prefix}-settle"
      stream_arn   = aws_dynamodb_table.agreements.stream_arn
      function_arn = module.settle.arn
      role_name    = aws_iam_role.settle.name
    }

References read `module.fn["manage_stock"].arn` / `.name`. The role's logs statement is
CreateLogStream + PutLogEvents on `/aws/lambda/<prefix>-*`; no CreateLogGroup.

Alarms that are about the app — a duration ceiling, a queue's age — live in the calling
module's `alarms.tf`, not here.

## what a raise does, per invoke shape

The exit after a failure's `[ERROR]` line is the invoke shape's call, because a raise means
something different under each:

| shape | a raise does | failure exit | refusal exit |
|---|---|---|---|
| tool (a gateway target the agent calls) | FunctionError to the agent, no reason | `err(reason, 502)` — the agent reads why | `err(reason, 4xx)` |
| route (API Gateway) | 502 to the caller | 502 — a webhook provider retries a 5xx | 4xx, no retry |
| direct invoke (another lambda reads the response) | FunctionError to the caller | `err(reason, 502)`; the caller treats FunctionError and non-200 as failure | 4xx |
| async (a bus rule, a schedule, an Event invoke) | two retries, then the `on_failure` destination or nothing | raise if a retry could succeed, return if it could not | return |
| DynamoDB stream (`stream/`) | the batch retries until the record expires and the shard waits | `aws.stream_batch`: the record in `batchItemFailures`, retried alone, then the queue | return from `one` |
| SQS | the message returns to the queue | raise — the redrive is the retry | return (the message is deleted) |

## adopting a live stack

A group Lambda created itself — a function declared before this module, or a function
hand-made — exists unowned, and a create on the same name fails the apply.
`bash scripts/adopt_log_groups.sh <stack-dir> <logs-profile> [<tf-profile> <gerp_id>
<aws_account_id>]` plans the stack, imports every `/aws/lambda/<name>` the plan would create
that already exists, and the apply then sets the retention. One gerp at a time in a template
dir (`prod/per_customer`, `prod/init_customer`): the script's init points the dir's backend at
that gerp's state. A fresh account needs no import. A function that moves from a bare
`aws_lambda_function` onto this module moves by a `moved` block in the calling module.

## the fleet deploy

Every gerp at once is `bash scripts/deploy.sh fleet [--report] [--gerp a,b] [--dirs …]`, a pipe
over `scripts/fleet.py`'s pieces, each one AWS call shape with tab-separated lines on stdout:
`listzipversions` (the bucket's latest version and sha256 per zip, read once: the run's target),
`listgerps` (every active row with an account), `listzipfnsconf --gerp X` (the gerp's functions
by the `gerp:src-dir` tag with their `CodeSha256`, through the `gerp-X` profile), `status
<snapshot>` (the join: `in-sync`, `behind` with the snapshot's version to move to, `left` for a
function with no artifact or a src_dir another gerp carries and this one does not), `update` (one
`UpdateFunctionCode` per `behind` line, a gerp outside us-east-1 from its region's replica),
`push <dir>` (build and put with provenance, the only piece that zips). Ten gerps run at once
through `xargs -P`; a failed move is a line and the exit code, and the others finish; the report
is `.build/fleet.tsv`. A loop is one artifact type paired with one resource type, zip × lambda
function here; another deployable is another loop after it, not a column in these lines.

The workflow yaml runs the same pipe on a runner. **Keep the two compositions identical in
logic**: the same pieces in the same order with the same flags. A runner thing (`parallel` in
the xargs slot, `::group::`, the step summary) is written in the yaml and never in a script, so
no script under scripts/ reads `GITHUB_ACTIONS`. Two copies of a pipe are a hazard for people
and a small one with agents reading both; the test that reads both holds them together.
