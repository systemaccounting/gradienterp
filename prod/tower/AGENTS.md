# tower — operator control plane

operator-singleton. Runs in the **operator account** (services OU sub-account). Orchestrates signup → CT-managed sub-account vending → per-customer terraform apply. every cross-account trust condition (`aws:PrincipalOrgID` / `aws:ResourceOrgID`) reads `local.org_ids` — the organization plus `ORG_IDS` from `config.json` — so a second organization is an entry there.

## what's provisioned today

| resource | purpose |
|---|---|
| `aws_s3_bucket.codebuild_source` (`gerp-codebuild-source-<account>`) | versioned + encrypted bucket. holds the source zips, the working tree `scripts/zip.sh source` builds, never uploaded by an apply: `release/source.zip` (a committed tree, `upload.sh source --release`), both projects' own location and so what a signup, a hub vend and a closure build from; and `source.zip` (every upload, `upload.sh source`), which `apply.sh` names through `sourceLocationOverride`. codebuild reads it as its source; the lambda only triggers the build |
| `aws_codebuild_project.per_customer` (`tower-per-customer`) | runs `prod/init_customer/` then `prod/per_customer/` terraform via the buildspec at `.codebuild/per-customer.yml` (`TF_ACTION=apply`). Two destroys, both of `per_customer` only and both after the export: a closure (`TF_ACTION=destroy`, from gradienterp's `closure/begin.py`) stamps the row `closing` then `closed`; a stop (`TF_ACTION=stop`, from `bash scripts/apply.sh --stack per_customer --gerp <id> --action stop`) sets it `stopped` with `gateway_url`, `chat_url` and `runtime_endpoint_arn` removed, schedules nothing, and `apply.sh --stack per_customer --gerp <id>` is the apply that brings it back. A `plan` build (`apply.sh … --plan`) plans both stacks and changes nothing. The account and the row stay through all three. post_build writes the row only when the build phase passed (`CODEBUILD_BUILD_SUCCEEDING`): a failed apply leaves it `provisioning`, a failed destroy `closing`, and the build_failed alert says why |
| `aws_iam_role.codebuild` (`tower-per-customer-codebuild`) | codebuild service role. Permissions: source bucket read, tfstate bucket r/w + DDB lock, `sts:AssumeRole` on `OperatorOrchestration` in any org member account (scoped via `aws:ResourceOrgID`), CloudWatch logs |
| `aws_lambda_function.provision_customer` (`tower-provision-customer`) | orchestrator. See `lambdas/provision_customer/AGENTS.md` |
| `aws_sqs_queue.vends` (`tower-vends`) + `aws_lambda_event_source_mapping.vends` | the card-to-vend queue, consumed four at a time (§ the vends queue); `tower-vends-failed` + `tower-vends-parked` |
| `aws_codebuild_project.hub` (`tower-hub`) | applies `prod/hub` against a hub account (`.codebuild/hub.yml`); started by the provisioner on a `kind: hub` vend |
| `aws_lambda_function.cognito_post_confirmation` (`tower-cognito-post-confirmation`) | post-confirmation trigger on the `gradienterp` user pool. **Signup creates an account (the Cognito identity) only — it does NOT provision a sub-account.** Provisioning is decoupled to an explicit gerp-instance capability action (`TODO.md`). This hook completes signup and seeds the `gerp-accounts` row: the email from the pool's attributes, the name from the confirm call's `ClientMetadata` (`{first, last}`, sent by the SPA on `ConfirmSignUp`) — the pool never holds a name. A conditional put, so a second confirm does not clobber an edited row; a failed put does not fail signup, and `GET /api/account` recreates a missing row on first read. See `lambdas/cognito_post_confirmation/main.py` |
| `aws_ecr_repository.agent` (`agentcore`) | operator-shared agent container image. immutable tags, scan-on-push, keep-last-10 lifecycle, org-scoped pull policy. every customer's AgentCore Runtime pulls cross-account; per-customer terraform reads the most recent image and never builds. (`agent_image.tf`) |
| `aws_s3_bucket.canonical_schemas` (`gerp-canonical-<account>`) | canonical registry JSON. versioned, org-scoped read, 90-day noncurrent expiry. three explicit `aws_s3_object` uploads on every tower apply — chart_of_accounts, contact_fields, calendar_fields. there's no `*.json` glob, so a new registry needs its own `aws_s3_object` added here (the note/task/item field registries in `modules/schemas/data/` are not mirrored yet). customer agents pull on their weekly cron. (`canonical_schemas.tf`) |

## what `provision_customer` lambda does

Runs when a card lands on a gerp: the owner app's BFF sends the provisioning payload to the `tower-vends` queue and the mapping hands it to this function one message at a time (§ the vends queue). A hand-run invokes it directly with the same payload. **Not** run on signup — signup only creates the Cognito identity:

1. Assume `TowerProvisioning` role in management
2. `servicecatalog:ProvisionProduct` against CT's Account Factory product. CT vends the account, auto-baselines, deploys `OperatorOrchestration` role via the customers-OU stackset
3. Poll `organizations:ListAccounts` for the new ACTIVE account by name (SC AF can return FAILED while CT internally completes — account visibility is source of truth)
4. Assume `OperatorOrchestration` directly on the new sub-account → seed SSM tenant metadata at `/gradienterp/customers/<customer_id>` (`business_name`, `business_category`, `owner_email`, `reporting_schedule`, `openly_operated`, and the `legal` / `public` business profiles the create screen collected when the BFF passed them)
5. On that same session, model access — per ACCOUNT, not shared from the org: `PutUseCaseForModelAccess` with the operator's form (`var.bedrock_use_case_form`), poll `authorizationStatus` to AUTHORIZED, then one `CreateFoundationModelAgreement` per `var.bedrock_model_ids` unless already AVAILABLE/PENDING. Does not wait for AVAILABLE — the build that follows outlasts it. `scripts/bedrock-subscribe-model.sh` is the by-hand version
6. `codebuild:StartBuild` on `tower-per-customer` with `CUSTOMER_ID` (the gerp_id: at most 28 characters, the BFF's `GERP_ID_MAX`, so `gerp-<module>-<gerp_id>-<function>` fits Lambda's 64 on every function; `tests/tower/local/test_resource_names.py`) + `CUSTOMER_ACCOUNT_ID` env overrides — the build applies `init_customer` (the export bucket, its CMK, the reader role, `export_gerp`; `<gerp>/init.tfstate`) and then `per_customer`
7. Return `{customer_id, account_id, build_id, build_arn, status: "provisioning"}` — does NOT wait for codebuild

monolithic for POC. Step Functions earns its place when the chain has more steps (rollback paths, fan-out, longer waits).

Before step 2 it looks for an ACTIVE account already named for the business and takes it as this gerp's (recorded on the row, nothing vended): a run that died between Account Factory and the row is resumed by the next attempt. A row that already names its account skips the vend the same way.

## the vends queue

Control Tower runs five account operations at once, and a vend holds one for ~15 minutes. `tower-vends` (`vends.tf`) sits between the card and the vend: the BFF sends `{customer_id, owner_sub, owner_email, business_name, openly_operated, legal, public}` and writes the row `queued`; the event source mapping runs `provision_customer` on at most four messages at a time (`scaling_config.maximum_concurrency = 4`, one Control Tower slot left for a hand-run), and the function writes `provisioning` when it takes one. The fifth signup in a window waits on the queue, and its card says how many are in line before it.

- visibility timeout 5,400 s, six times the function's 900 s wall: a consumer that dies mid-vend surfaces its message 90 minutes later, when the account it started is ACTIVE and the name check above resumes it
- three receives, then `tower-vends-failed`; `tower-vends-parked` (`ApproximateNumberOfMessagesVisible ≥ 1`) on the ops topic, so a parked vend is a task on the operator gerp. Redrive it or remove it; the alarm stays until the queue is empty
- a message is the same payload a hand-run invokes with; the handler reads `Records[0].body` when the mapping calls it
- a message with `kind: hub` vends a hub (`prod/hub`): an account in the organization's `hubs` OU named `gerp hub <region>`, then the `tower-hub` build (`.codebuild/hub.yml`) instead of the per-customer one; no row, no invoice unit, no tenant blob, no model access (`scripts/vend_hub.sh`)

## where a gerp lives: the organization, the hub, the region

The provisioner's `ORGS` env (from management's outputs, keyed by organization id, one entry) names the management role that may call Account Factory, the product and path, the customers OU and the hubs OU; a vend message may name its `org`, and the first is the default. `HUBS` (config.json) names each region's hub — its account, `bus_arn`, `manage_edges_arn`; `REGIONS` names where a gerp can be built (label, `model` — the region's inference profile, status, the countries it is the default for). A vend message carries `region` (the create screen's pick, the address's country's region by default); the provisioner vends into that region's customers OU (`customers_ous` from management), records `region` and the region's hub on the row and on the gerp's directory row, adds the spoke on that region's hub, agrees to the region's base model in that region, and starts the build with `CUSTOMER_REGION`, which the buildspec exports as `TF_VAR_aws_region` and `AWS_REGION`; the build's own region is kept as `OPERATOR_REGION` first, and every call the buildspec makes to the operator's `gerp-customers` table and ops topic names it. Measured on the Irish gerp: the vend to an active account 22 s, the per-customer build 10 min to `marked active`. A vended gerp's row carries `region`, `hub` (the bus its events go to) and `org`, and every arn built for the gerp reads them off the row (the BFF's forwards, the ssm writers here). With the account recorded the provisioner writes the gerp's row in `gerp-directory` (`hub`, `hub_bus_arn`, `region`, `aws_account_id`: what any gerp reads to address this one, modules/events) and adds its `spoke` edge on its region's hub through the hub's door (`manage_edges`: a rule on the hub bus, exact `detail.to`, target the gerp's own bus); no hub for the region means no edge, and a door that refuses is logged and the vend goes on. No other hub is touched. `close_account` deletes the directory row and removes the spoke before `CloseAccount`, by the row's region.

The provisioner resumes: it reads the row first and skips the vend when `aws_account_id` is
there, and everything after the vend is idempotent, so a failed run is re-invoked into the same
account. It logs one JSON line per step (`provision.step`, the gerp, `elapsed_s`) and a failure
names its step, so a run reads per gerp — `{ $.customer_id = "<gerp_id>" }` — not per request id.

## the regions: what the operator holds in each

`regions.tf`: one provider alias per region in `REGIONS` beyond the first and one instance of
the `region/` module per alias — the artifact bucket lambda deploys from
(`gerp-artifacts-<operator>-<region>`), the OAM sink a gerp's account links to, the ops topic
its alarms notify, the ECR replica of the agent image — plus S3 replication from the us-east-1
artifact bucket to every region's (`deploy.sh push` writes one bucket; a gerp elsewhere deploys
from its region's replica, `scripts/deploy.py` waiting for the checksum it pushed) and ECR
replication of `agentcore` to every region (AgentCore pulls from its own). A region added to
`REGIONS` is an alias and a module block here, the one place a new region is a tf edit. The
whole of what a region is, across management, the operator, the hub and the vend, and the steps
that add one: `region/AGENTS.md`.

## one account, many stacks

An AWS account and the stack inside it have two different clocks, and only the account is
scarce (the org's slot; a closed account holds its slot 90 days). What burns a slot:
`ProvisionProduct` and `CloseAccount`. What does not: the apply, the destroy, the export, the
seeds, the agent — all stack work on the same account, free to repeat; `force_destroy` on every
bucket means a destroy empties it and the next build runs into it again. Fighting tower or the
build never vends a second account.

The clocks, measured on westwood (2026-09-04 to 06):

| phase | measured |
|---|---|
| Account Factory, `START` to `vended_at` (the ACTIVE account) | 16 s; the CT baseline runs on after |
| the account under the payer (the invoice unit) | up to 5 min; the provisioner waits it out |
| the provisioner after the vend (invoice unit, assume, tenant blob, model access, StartBuild) | 8 s |
| the apply build, fresh account, one pass (512 resources, then the guides) | 8–10 min |
| the destroy build (the export, then 511 resources) | 12 min; the export 20 s |
| ready to the agent's first answer | at once |

`stop` and `start` (`bash scripts/deploy.sh …`) are the stack clock alone, and the routine the
staging pair runs between sessions.

bash equivalent at `.github/workflows/per-customer-apply.sh` — useful for ad-hoc provisioning + debugging.

## dependencies

- **cognito user pool** (operator account) — auth identity for signups; carries the signup record itself, no separate signups DDB needed for POC
- **TowerProvisioning role in management** — minimal cross-account role granting `servicecatalog:ProvisionProduct` + `controltower:CreateManagedAccount` + Org reads. Associated with CT's Account Factory portfolio
- **OperatorOrchestration role on every customer account** — auto-deployed by service-managed CFN stackset on customers OU (`prod/platform/management/operator_trust_stackset.tf`). Trust: operator account. Permissions: AdministratorAccess. Lets operator-resident workloads (this lambda, codebuild) reach customer accounts directly without IAM trust widening
- **codebuild project** that runs `prod/per_customer/` terraform on lambda trigger
- **`prod/per_customer/`** template — the per-customer apply
- **customers DDB** (in `prod/platform/operator/`) — provisioned for customer status tracking (id, status, sub_account_id, owner_email, …), but **not written by tower today**: provisioning status lives in the SSM tenant metadata at `/gradienterp/customers/<id>` plus the vended account's own state

## metering + billing

AWS meters per-sub-account natively (Cost & Usage Report, Cost Explorer API, cost allocation tags), so there is no custom meter lambda — billing reads AWS's own metering.

**Each gerp gets its own AWS invoice unit, created at PROVISIONING.** One `invoicing create-invoice-unit` holding that gerp's linked account, so AWS issues a real invoice per gerp each month with its own id and its own tax. It is free (`AWSInvoicing` has no chargeable products) and it must exist BEFORE the month it should invoice — which is why it belongs in `provision_customer` and not in the billing run.

`bill_customer` is a daily poll from early in the month, and it books BOTH legs:

- **cost** — `invoicing list-invoice-summaries` → the unit's `TotalAmount` → DR `COST_OF_GOODS_SOLD` / CR `ACCOUNTS_PAYABLE`, with `get-invoice-pdf` stored to S3 as evidence and never parsed.
- **revenue** — that amount × 1.2 → `manage_invoice (op: create)` in gradienterp's own gerp → `issue_invoice`.

Collection is not tower's concern and not a branch here: it is the payer's business, and it runs on the same rails every other invoice does.

**What the operator's own gerp attaches to those rails.** `collect_platform_fee` on
`INVOICE_STATUS#issued` runs `charge_saved_card`, and `charge_saved_method` calls `mark_unpaid` when
the card fails. Two rows on `INVOICE_STATUS#unpaid` schedule the chase (`collections/notice.py`,
every 3 days for 15) and the deadline (`closure/begin.py` at 15 days, named by the customer);
`stop_chasing` on `INVOICE_STATUS#paid` runs `collections/cancel.py`. The closure scripts are the
same ones `POST /api/gerps/close` hands a customer's own request to — one sequence from the backup
on, described in `modules/automation/AGENTS.md`. `collections/audit.py` sweeps daily. The scripts are
gradienterp's own, approved into its cabinet; the reusable half is `modules/automation` and
`modules/rules`.

## the receivable state on the gerp row

The seller and the operator are one party, so "this customer owes gradienterp $X, and their gerp
closes on D" is gradienterp's own collections state and lives on `gerp-customers`, kept by
`bill_customer`: at issue it stamps `billing` — a list of the hosting invoices still open,
`{invoice_id, total, period, issued_at, unpaid_at?}`; every daily run first re-reads each entry
from the seller (`manage_invoice {op: get}`) on every row carrying `billing`, whatever the gerp's
status — `unpaid` stamps `unpaid_at`, `paid` drops the entry, and a row with nothing left open has
`balance_owed` set to 0 on it and on every `gerp-priors` row whose endings name the gerp. A closed
gerp with a balance is read until it pays. `dry_run` reads and writes nothing. The gerp-cloud BFF
reads the row for the owner console; nothing pushes from the seller's rules.

## a failure reaches a person

`alerts.tf`: the topic `gerp-ops-alerts` with one email subscription (`ops_alerts_email`, an
`ops+` address the SES catch-all forwards; confirmed once). The sources:

- the build — an EventBridge rule on `CodeBuild Build State Change` for `tower-per-customer`,
  `FAILED`, `STOPPED`, `TIMED_OUT`; the message is a JSON object carrying the status, the build,
  the log link and the build's environment (`CUSTOMER_ID`, `TF_ACTION`), so it says which gerp
  and which action without a lookup
- the lambdas — ONE alarm per account on a raise anywhere: `AWS/Lambda Errors` with no
  dimension is the account's sum across every function, so `gerp-operator-errors` here covers
  tower, the read api, the BFF, the optimizer, platform/operator and email, and
  `gerp-<gerp>-errors` (`prod/per_customer`) covers a gerp's ninety in its own account,
  publishing to this topic across accounts (`OPS_ALERTS_TOPIC_ARN` in `config.json`; the topic
  policy admits any `gerp-*` alarm in the org). The log group names the function; the alarm's
  description carries the Logs Insights query that lists them. Alarms bill per alarm, and one
  per function said nothing the lines do not. Plus a `Duration ≥ 720 s` alarm on the
  provisioner as the early word that Account Factory is slow
- the async invokes — `on_failure` destinations on `provision_customer` (a hand-run) and
  `bill_customer`: the failure record carries the request and the error. **A hand-run vend is
  never retried by Lambda** (`maximum_retry_attempts = 0`); a queued vend's retry is the queue's,
  90 minutes on, and the provisioner's name check is what makes it safe (§ the vends queue).
- the stage in front of a function — three API stages (the gerp's server, the owner app's BFF,
  the read api) each write a JSON access log at `LOG_RETENTION_DAYS` with the fields that say
  why a request failed before any function ran (`integrationStatus`, `integrationError`,
  `error`, on the HTTP stages `authorizerError`), and each has a `-gateway-5xx` alarm on the
  topic (`AWS/ApiGateway 5xx` / `5XXError`, the stage's own failures, which no lambda log
  shows); the collector's threshold reader files it. A REST stage's access log needs the
  account's `aws_api_gateway_account` cloudwatch role (`prod/api_openlyoperated` holds it);
  HTTP stages need none
- the failures a function caught — every function's log group has a metric filter on its
  `[ERROR]` lines (`modules/terraform/lambda`): `ErrorLines` into `gerp/app/<gerp>` (or
  `gerp/app/operator`), and `ErrorLinesByKind` by function, `kind` and `category`. ONE alarm per
  stack on the sum — `gerp-<gerp>-error-lines` in `prod/per_customer`, `gerp-operator-error-lines`
  here — because the line already names the function and the kind, and a task made from the
  alarm queries the lines; an alarm per function would say nothing more and bill 89 times. The
  runtime's `Errors` alarm stays per function (its dimension is fixed).
- a stream record parked — `<mapping>-parked` on each `<name>-failed` queue's visible messages
  (`modules/terraform/stream`), in ALARM until the record is redriven or removed.
- the room to vend — Organizations caps the accounts in an org (`L-E619E033`, 50 by default,
  adjustable by a request from the management account that takes days) and Control Tower caps
  an OU at 1,000 (fixed); Organizations publishes no usage metric. `bill_customer` measures both
  on each daily run, on the management session it already holds — every account
  `list_accounts` returns counts until it is permanently closed — and publishes
  `gerp/platform OrgAccountsUsedPercent` and `CustomersOuUsedPercent`, no dimension. The
  owner app's create screen shows the same count live, read through a role in management
  (`prod/gradienterp_cloud`).
  `gerp-org-accounts-80pct` and `gerp-customers-ou-80pct` alarm at 80% (Maximum over a day).
  On the first, a person requests the raise from the management account
  (`aws service-quotas request-service-quota-increase --service-code organizations --quota-code
  L-E619E033 --desired-value <n>`). On the second, a second customers OU: created in
  `prod/platform/management`, registered with Control Tower, and `provision_customer`'s
  `CUSTOMERS_OU_MANAGED_NAME` pointed at it. A failed measurement is one ERROR line and the
  bill stands.
- the apply that needed two passes — `tf_apply` in the buildspec applies once more, from a
  fresh plan after 20 s, when the failure is an IAM race (`not authorized`, `does not have
  permission`, `AccessDenied`: a resource that tests its role at create before IAM has the
  policy). Terraform picks up from state, so the second pass is the missing resources only;
  one retry, apply only, never the destroy or stop. It publishes to the topic itself — subject
  "recovered after retry: <gerp>", the resource address and the cause — because the fix is that
  resource's own `time_sleep` edge on the policy it tests, in its module (the KB and the
  browser have theirs), and the address is the message. `apply.sh --stack per_customer` prints the
  `==> apply.retry` line too. No retry mail for a month means every edge is there.

Each of those alarms is also work: the topic delivers every state change to `issue_collector`
(platform/operator), which turns an ALARM into one task per failure KIND on the operator gerp's
tasks — the alarm names the account and the signal, the lines name the function and the kind —
and closes them on the OK. § the alarm as a task in `prod/platform/operator/AGENTS.md`.

`gerp-ops` (this stack) is the dashboard over every account — 22 series with two gerps, about
twelve more per gerp, so it passes the free tier's 50 metrics around the fourth gerp and is $3 a
month from there at any number of gerps (a dashboard is billed flat, not per metric); a SEARCH
caps at 500 series. What scales per gerp is the `ErrorLines` / `ErrorLinesByKind` metric-filter
metrics, $0.30 each a month and only while a kind is publishing. A filter term in a SEARCH is a
bare token (`failed`), never quoted: raises, caught failures by kind,
parked records, invocations, duration, the gateways' 5xx. Every widget is a SEARCH naming no
account, so a vended gerp appears once its OAM link exists (`prod/init_customer`) and the
dashboard is never edited per gerp. The sink is `aws_oam_sink.gerps` here; its arn rides
`config.json` (`OAM_SINK_ARN`) like the ops topic's. A new link takes a few minutes before the
monitoring account can read through it.

A `provisioning` row with no `gateway_url` is therefore always preceded by an email: the build's
30-minute timeout reports `TIMED_OUT`, the provisioner's 900 s wall is an error.

## the owner hears from the operator

`notify_owner` (`notify_owner.tf`): the one lambda that writes to a gerp's owner. A message kind
is a function in `MESSAGES` with the row status it applies to; the send, the once-only stamp
(`notified_<kind>_at` on the row) and the log line are shared. Invoked with `{gerp_id, kind}`,
or with `{kind}` alone to sweep every active row. Two kinds:

- `ready` — from the rule on `tower-per-customer` reaching SUCCEEDED for an apply: the gerp is
  up, the console link with `say=onboard` so the first conversation starts as the onboarding
  walk (`gradienterp.cloud/?gerp=<id>&open=chat&say=onboard`; the console carries it through
  sign-in and onto the chat door as the first message)
- `onboard` — the daily sweep (`tower-onboard-sweep`, 09:00 Pacific): a gerp active a day with
  no chat session and no journal entry, read in its own account through `OperatorOrchestration`
  (the agent's sessions bucket, the ledger), gets the same link once

The mail goes from the operator's sender; the gerp's agent mailbox is another address in another
account.

The walk the link starts, as an owner runs it (westwood, an investor — the lightest case, and
the test of the guide is what it leaves alone):

| the agent asks | the owner says | what lands |
|---|---|---|
| what does the business do? existing books or fresh? | Westwood Investments; we buy distribution rules and collect the payouts; fresh start | an `instruct` line; no migration walk |
| where are you based? | Marina del Rey, California | `set_timezone America/Los_Angeles`; location 1 relabeled |
| public or private? | public | left as set; the agent says where the switch is, does not flip it |
| (sell / vendors / employees / processor / statements) | nothing sold; no vendors; just me; bank transfers only; monthly | items, tax and a processor skipped; a `remember` for the payment method |
| (close) | — | "setup is done; everyday bookkeeping starts now" |

After a stopped gerp's apply (the settings went with the stack) the owner column goes in one message
from the chat door, and the agent applies it without the questions:

    onboard my business, here is everything: Westwood Investments, we buy distribution rules and
    collect the payouts, fresh start. Marina del Rey, California. Public. Nothing sold, no vendors,
    just me, bank transfers only, monthly statements.

## closing an account

`close_account` — the end of a closure. `organizations:CloseAccount` is management-only, so this
assumes `TowerProvisioning` for that one call, the way `provision_customer` does for Service
Catalog, then marks the row `closed`. It refuses a row that is not `close_requested`/`closing`:
being invoked is not authority, and an `active` row is a customer nobody asked to close. Invoked by
the operator gerp's `closure/close.py` through `gerp-closure-requester`, fifteen days after the
build that exported and destroyed the instance. A closed account enters AWS's 90-day post-closure
period; nothing here shortens or extends that. Organizations closes 3 accounts at once
(`ConcurrentModificationException`) and 250 or 20% of the org per rolling 30 days
(`ConstraintViolationException`, `CLOSE_ACCOUNT_QUOTA_EXCEEDED`); a close refused on either is a
429 with `reason` — `concurrent_closes` (`retry_after_s` 3600), `monthly_close_quota` (86400), or
any other `Reason` Organizations gave (3600) — and the row untouched, so the closure script
schedules itself again that far on. The event carries `how` (`unpaid` when the closure script passed an invoice, `requested` otherwise), `invoice_id` and `balance_owed`, and the row is stamped `closed_how`, `closed_invoice_id`, `balance_owed` beside `closed_at` — the gerp-cloud BFF reads them into gerp-priors when the account behind the gerp is deleted.

## a changed login

`update_owner_email` — an owner's changed login reaches the places that read the old one. The
gerp-cloud BFF's email sync invokes it (`{old_email, new_email, gerp_ids}`, the gerps the account
OWNS) the moment the claim differs from the row. One `UpdateUser` renames the Identity Center
user Account Factory made from `SSOUserEmail` — `userName` and the primary email, since the store
is org-wide and there is one user per address however many gerps the owner holds — assuming
`TowerProvisioning` for that one call. Then, per owned gerp that is vended and not closed,
`OperatorOrchestration` into the gerp's account to rewrite `owner_email` in the tenant blob
(`/gradienterp/customers/<gerp_id>` — incidents and the agent-mailbox verify read it) and on the
gerp-customers row. A user already under the new address is `already`, none is `none`, someone
else's user under it is a 409; a closed or never-vended gerp is skipped. Best effort after the
row: the BFF logs a failure and the row stands.

**`update_business_info`** is the same shape for an edited business profile: the BFF writes
`label`, `legal` and `public` on the row and invokes this with `{gerp_id, business_name?, legal?,
public?}`; it assumes `OperatorOrchestration` into the gerp's account and rewrites just the fields
given in the tenant blob, keeping the rest. A closed or never-vended gerp is skipped; a missing
blob is reported, not written.

## trust

- tower lambdas run in the operator account
- `provision_customer` briefly holds an assume-role into management for the single `servicecatalog:ProvisionProduct` call (TowerProvisioning is associated with CT's AF portfolio)
- customer agents cannot invoke tower lambdas (IAM-scoped; cedar enforces this once the operator agent exists)
