# prod/platform/operator

Operator-account foundation. Runs in the **operator sub-account** via cross-account `OrganizationAccountAccessRole` assumed from management. Provisions only the resources every other operator-account terraform depends on.

## what it provisions

- **the firms' machine scope** — Cognito resource server `gerp-mcp` (scope `call`) on the pool;
  modules/mcp makes one client-credentials app client per gerp against it from the per_customer
  apply, the firm's `sub` on its vendor gateway.
| resource | purpose |
|---|---|
| `aws_s3_bucket.tfstate` | s3 state bucket — every other terraform dir in the org points its `backend "s3"` here |
| `aws_dynamodb_table.tfstate_lock` | DynamoDB lock table — terraform standard schema (`LockID` hash key) |
| `aws_dynamodb_table.customers` (`gerp-customers`) | the gerp-instance registry — one row per provisioned gerp, hash key `gerp_id`, owner-index GSI on `owner_sub` (account → its gerps). Streams enabled for downstream fan-out |
| `aws_cloudwatch_event_bus.operator` (`gerp-operator`) | the operator's own bus: the hub's forward edge (`prod/hub`) puts every event not addressed to a gerp here, and the operator's consumers — the publisher, the counters, the archive (`prod/api_openlyoperated`), the platform's reports — are its rules. The old `gerp-events` bus forwards the same events here until every gerp puts to the hub |
| `aws_cognito_user_pool.main` (`gradienterp`) + client + prefix domain (`gerp-auth`) | single auth identity for the platform. Hosted UI on cognito's prefix domain (no custom domain yet). Account is the cognito identity; capabilities (public_user, erp_instance) are dashboard tiles. |
| `aws_lambda_function.plaid_gateway` (`gerp-plaid-gateway`) + `aws_ssm_parameter.plaid_{client_id,secret}` | the shared-Plaid-credential boundary (`plaid_gateway.tf`, see `modules/accounting/AGENTS.md § bank-feed reconciliation`). Holds the one `client_id`/`secret` here so it never sprawls into customer accounts; makes the outbound Plaid calls — ops: `create_link`/`complete` (Hosted Link connect), `exchange`, `pull` (`/transactions/sync`), `verification_key` (JWK for the webhook shim), `webhook_route` (item_id → gerp → cross-account invoke reconcile). Per-gerp `reconcile`/`connect_bank`/`check_bank_connection` invoke it cross-account carrying only that gerp's `access_token` (one org-scoped `aws_lambda_permission`). |
| `aws_lambda_function.plaid_webhook` (`gerp-plaid-webhook`, Node) + `aws_lambda_function_url` | the public webhook endpoint (`plaid_webhook.tf`). Plaid signs webhooks with an ES256 JWT (Python can't verify ECDSA) — this thin Node shim verifies with built-in `crypto` (zero bundled deps), fetching the JWK via the gateway's `verification_key` op (never holds the Plaid secret), then calls `webhook_route`. Function URL is auth NONE — Plaid's JWT is the auth. |
| `aws_dynamodb_table.priors` (`gerp-priors`) | what a closed account left behind — one row per identifier, hash key `id` = `card#<fingerprint>` / `email#<sha256>` / `phone#<sha256>`, carrying the endings (gerp_id, how: requested \| unpaid, closed_at), the balance owed and the Stripe customer id. Written by the gerp-cloud BFF when an account is deleted; read by it at create-gerp (email, phone) and at provisioning (the card). Nothing in the clear. |
| `aws_dynamodb_table.plaid_items` (`gerp-plaid-items`) | item_id → gerp_id map, hash key `item_id`. Written by the gateway's `complete` on connect (the one place item_id and gerp meet); read by `webhook_route` to route a `SYNC_UPDATES_AVAILABLE` webhook to the right gerp's reconcile. |

State bucket has versioning + AES256 + public-access-block, with a lifecycle rule expiring noncurrent versions after 90 days.

## bootstrap order

This dir is the second apply, after `prod/platform/management/`:

1. **Run `prod/platform/management/`** with local state. Creates the operator sub-account; outputs `operator_account_id`.
2. **Run this dir** with local state, passing `operator_account_id`:
   ```bash
   cd prod/platform/operator/
   cat > terraform.tfvars <<EOF
   operator_account_id = "<operator-account-id-from-management-apply>"
   EOF
   terraform init
   terraform apply
   ```
   Provider assumes `OrganizationAccountAccessRole` into the operator account; resources land there.
3. **Migrate state for both dirs into the s3 backend** — add a `backend "s3"` block to each dir's `versions.tf`:
   ```hcl
   backend "s3" {
     bucket         = "gradienterp-tfstate-<operator-account-id>"
     key            = "platform/management/terraform.tfstate"   # or operator/, etc.
     region         = "us-east-1"
     dynamodb_table = "gradienterp-tfstate-lock"
     encrypt        = true
   }
   ```
   Then `terraform init -migrate-state` per dir. Subsequent applies are remote-backed and concurrent-safe.

## plaid gateway creds — set out of band, never through tf

The `client_id`/`secret` must NOT flow through terraform (a `data.aws_ssm_parameter` would pull the plaintext into state, and state lives in the s3 backend for a repo that goes public). So tf only creates the params as empty placeholders (`value = "SET_ME"`, `ignore_changes = [value]`) and declares the contract; the lambda reads them at runtime via boto3 (`ssm.get_parameter(WithDecryption=True)`, module-cached). After `terraform apply` creates the shells, set the real values once:

```bash
AWS_PROFILE=operator-org aws ssm put-parameter --no-cli-pager \
  --name /gradienterp/operator/plaid/client_id --type String --overwrite --value <plaid_client_id>
AWS_PROFILE=operator-org aws ssm put-parameter --no-cli-pager \
  --name /gradienterp/operator/plaid/secret --type SecureString --overwrite --value <plaid_secret>
```

Terraform's known value stays `"SET_ME"` forever (ignore_changes), so subsequent applies never clobber or read the real secret. `PLAID_ENV` is `sandbox` today (in `plaid_gateway.tf`); flip to `production` and re-set the params with prod creds at go-live. Local dev/tests bypass SSM via the `PLAID_CLIENT_ID`/`PLAID_SECRET` env branch in `_creds_pair()`.

## bus topology

The bus does no filtering itself. Every event carries `openly_operated` as a top-level `detail` attribute, set at emit time by the customer's modules from local SSM. Two consumer shapes attach downstream:

- **publication** — a rule with pattern `{"detail":{"openly_operated":[true]}}` routes broadcast events (journal entries, iot streams) to the public DDB ledger + S3 archive. Lands in `prod/api_openlyoperated/` alongside the sinks it targets. No publisher lambda — the rule is the gate.
- **peer routing** — per-customer rules forward peer-addressed events (purchasing quotes, scheduling visits) to the recipient agent's bus. Lands in `prod/per_customer/`.

Operator subscriptions beyond publication (telemetry, billing, support) are bound by ToS, not by infra segregation. Same posture as any conventional saas; the explicit ToS enumeration is the contract.

## the alarm as a task

`issue_collector` has two doors into the operator gerp's tasks. The first is an agent's
escalation (a rule on the shared bus). The second is `gerp-ops-alerts`: every alarm state
change is delivered here, and an alarm becomes a task (the us-east-1 topic also emails
`ops+alerts@`, `prod/tower/alerts.tf`).

An ALARM opens **one task per failure kind**, not one per alarm — the alarm names the account
and the signal (`gerp-<gerp>-errors` a raise anywhere in that account, `gerp-<gerp>-error-lines`
a caught failure anywhere, `<mapping>-parked` a stream record on its queue), and the LINES name
the function and the kind. The collector reads them itself: `ErrorLinesByKind` for the kinds in
the window, then the lines of each kind; for a raise, the errorTypes by Logs Insights; for a
parked record, the queue's depth. Each task carries the alarm, the function, the gerp, the
account, the window, the first lines with their `raised_at`, and a Logs Insights query already
written for the rest — the investigation starts from the task, never from a console.

An alarm whose name matches none of those is a threshold on a metric — `gerp-org-accounts-80pct`,
`gerp-customers-ou-80pct`, `tower-provision-customer-slow` — and has no lines to read. It files
one task, keyed on the alarm's name, with `category: threshold`, the datapoint the SNS message
reports in `NewStateReason` as the count, the metric and its threshold from `Trigger`, and the
metric's `get-metric-statistics` over the alarm's own period as the query. No role is assumed.

Dedupe is the open task for that `<function>#<kind>` (`subject_key`, `category: alarm`): a
second ALARM strikes the same task, an OK closes every task the alarm opened (found by the
`alarm:` line in the content — a tag would be a registry meaning, and an alarm name is not
one). The operator gerp's `tasks_poke` fires on the insert like any escalation.

The reads into a gerp's account go through `gerp-ops-read` (`prod/init_customer`), assumed from
here: Logs Insights, filter, metrics, alarms, the failed queues' messages, a table's
description. No data reads, no writes. The same role is what a person or a model uses to
investigate from a terminal. The reads open in the gerp's region — the alarm's arn names it
(every region's ops topic forwards to this collector) — and the task carries a `region:` line
so each investigator opens there too (`read_fleet_logs` takes `region`; `investigate.py` reads
the line).

The gerp-side collector (`modules/automation/lambdas/create_inc_from_log`) is untouched: the
owner's automations failing are the owner's incidents, on the owner's tasks.

### who works the task

The task row is the contract: an investigator is anything that can read it (`manage_tasks`
get), read the gerp's logs, and update it. Every investigator reads through one role,
`gerp-ops-read` in the gerp's account (`prod/init_customer`: logs, metrics, alarms, the failed
queues, the tables' descriptions; no data, no writes), and each is a principal on its trust.
What it writes back is four fields the task registry admits: `investigated_by` (who),
`finding` (what it read, quoted), `root_cause` (why), `proposed_fix` (what to change and where).
Two investigators exist:

- **the operator gerp's agent.** The task's insert pokes it (`tasks_poke` admits `alarm` beside
  `escalation`) with the investigator's prompt; its `read_fleet_logs` tool (the agent container,
  registered where `OPS_READ_ROLE` is set — the operator's own gerp only) assumes the role in the
  account the task names, runs the task's query as given and peeks a named queue. It proposes;
  a person approves any change.
- **a person with a Claude session.** `bash scripts/investigate.sh <task_id>` prints the task,
  the lines the query returns, the queue's head and the module's `AGENTS.md` as one prompt;
  the same with `--finding --root-cause --proposed-fix` writes back as `local`.

Two findings on one task sit side by side in its changelog. A managed investigator (AWS DevOps
Agent) is a third principal on the same role and the same four fields, after golive.

## logs

Every function in every account writes one JSON object per line (Lambda's JSON log format,
`modules/terraform/lambda`; `aws.log` for the fields) into its own log group at
`LOG_RETENTION_DAYS` (`config.json`, 90). Two log groups feed a collector today by
subscription filter (`automate` and `machine_failed` on `{ $.incident = "*" }` → the gerp's
`create_inc_from_log`); everything else is read by a person or an alarm's metric filter.

The operator account is also the monitoring account for CloudWatch cross-account observability:
`aws_oam_sink.gerps` (tower) with a link in each gerp account (`prod/init_customer`, admitted by
the org id). Metrics and log groups from every gerp read here without assuming a role — what
`gerp-ops` draws and what an investigator queries. Three facts past CloudWatch:

- **an export path exists without touching code** — a subscription filter on a log group →
  Firehose → any backend (Datadog, Grafana Loki, OpenSearch, Axiom), or the backend's own
  Lambda extension shipping lines direct. JSON lines make either parse-free; the backend gets
  `level`, `requestId`, `gerp_id`, `function` and the ids as fields on ingest.
- **OpenTelemetry is the road to traces** — the ADOT layer speaks OTLP to every backend; if a
  request is ever to be followed across the gateway → a lambda → its downstream invokes, that
  is the instrument, and `requestId` on every line is what it joins on.
- **EMF (CloudWatch embedded metrics) is CloudWatch-only** and is not used; a metric a function
  wants to publish goes through a metric filter on its lines, which any backend can also do.

## reading the fleet

Four reads a session starts from. The profiles are `bash scripts/awsacct.sh --all`'s.

- **open alarm tasks** — on the operator gerp's tasks table (its task door is `scripts/investigate.sh <task_id>`):

      aws dynamodb query --profile gerp-gradienterp --table-name gerp-tasks-gradienterp \
        --index-name open-tasks-index --key-condition-expression 'open_flag = :o' \
        --filter-expression 'category IN (:a, :t)' \
        --expression-attribute-values '{":o":{"S":"1"},":a":{"S":"alarm"},":t":{"S":"threshold"}}' \
        --projection-expression 'task_id, subject_key, category, created_at'

- **a day's errors, every gerp** — Logs Insights in the operator account, which reads each gerp's
  log groups through the observability link. A gerp links to its own region's sink
  (`config.json` `OAM_SINKS`), so the query runs once per region — `--region eu-west-1` for Dublin.
  A caught failure is a line with `level: "ERROR"`; a raise is Lambda's own line, with `errorType`
  and no `level`, so the filter takes both. Lines before 2026-09-13 are plain text, not JSON:

      aws logs start-query --profile operator-org --region <region> --start-time $(( $(date +%s) - 86400 )) --end-time $(date +%s) \
        --query-language CWLI --query-string 'SOURCE logGroups(namePrefix: ["/aws/lambda/gerp-"], class: "STANDARD") START=-1d END=0s
          | fields @timestamp, @log, level, errorType, message
          | filter level = "ERROR" or ispresent(errorType) | sort @timestamp desc | limit 100'
      aws logs get-query-results --profile operator-org --region <region> --query-id <queryId>

  `@log` names the account and the function; add `and @log like /-<gerp_id>-/` for one gerp.
- **the agent's tokens** — namespace `gerp/agent`, dimension `gerp_id`, in the gerp's account
  (`modules/agent/AGENTS.md`): `aws cloudwatch get-metric-statistics --profile gerp-<gerp_id>
  --namespace gerp/agent --metric-name InputTokens --dimensions Name=gerp_id,Value=<gerp_id>
  --statistics Sum --period 86400 --start-time $(date -u -v-7d +%Y-%m-%dT00:00:00Z) --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ)`
  (`date -d '7 days ago'` on linux)
- **this month's AWS cost per gerp** — Cost Explorer in the management account, by linked
  account, matched to gerps with `bash scripts/awsacct.sh --list`:

      aws ce get-cost-and-usage --profile default --granularity MONTHLY --metrics UnblendedCost \
        --time-period Start=$(date -u +%Y-%m-01),End=$(date -u +%Y-%m-%d) \
        --group-by Type=DIMENSION,Key=LINKED_ACCOUNT

## what's deferred

These accrue in separate apply passes as consumers come online:

- **Public DDB ledger + S3 archive + publication rule** — written by `prod/api_openlyoperated/` once that dir exists. Reads from this bus.
- **Tower lambdas, api lambdas, biz frontend** — separate dirs (`prod/tower/`, `prod/api_openlyoperated/`, `prod/openlyoperated_biz/`) that all target this account but have their own state keys under the same s3 bucket.

## destroying

The state bucket has versioning enabled — `terraform destroy` won't empty it; you'd need to `aws s3 rm s3://<bucket> --recursive --include '*' --version-id-marker` first, then destroy. The DDB tables and lock table destroy cleanly.

Don't destroy this dir while other operator-account terraform dirs have state in the bucket — every other dir's state goes with it.

## gotchas

- The s3 bucket name is `gradienterp-tfstate-<account-id>`. Globally unique, account-id-suffixed so the same convention works if a second org ever spawns.
- `OrganizationAccountAccessRole` is auto-created in every sub-account by AWS Organizations. The caller (management-account user / Identity Center session) needs `sts:AssumeRole` on the role ARN — admin perms cover this; tighter scopes need an explicit policy.
- `aws_s3_bucket_lifecycle_configuration` requires an explicit `filter {}` block on each rule even when there's no actual filter (provider 6.x quirk).
- Customer-table streams emit `NEW_AND_OLD_IMAGES` — old-image is needed for tower to detect transitions (e.g., `openly_operated` flag flips) without a separate read.

**`aws_cognito_managed_login_branding` takes `jsonencode(jsondecode(file(...)))`, not `file()`.** Fed
the raw file, the resource plans an update on every run: Cognito returns the settings normalized
(floats, its default `categories` merged in), the provider compares `settings` as text and
re-derives `settings_all` from it, and no form of the file matches both. The decode/encode makes
the value terraform's canonical JSON, byte-equal to what the provider stores, and the plan is
clean. Upstream: hashicorp/terraform-provider-aws#49785.
