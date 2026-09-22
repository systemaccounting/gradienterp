# gradientERP

terraform modules for an ai-first, cloud-native ERP. each module provisions serverless erp features (lambda + dynamodb) for one customer's sub-account, managed by a per-tenant bedrock agentcore agent. every transaction is a spec-compliant event; whether it gets *published* is gated per-customer by the `openly_operated` flag (default-true for businesses seeking transparency-driven capital, default-false for private individuals running the ERP for personal use).

the use case, end to end: someone signs up at gradienterp.cloud, provisions a gerp, connects their own payment accounts, gets billed and pays, and — if openly operated — appears on the public dashboard.

signup flow: cognito post-confirmation seeds the account row; a gerp is created in the owner app, and its card landing sends the vend to tower's `tower-vends` queue → `tower-provision-customer` lambda (four at a time) → SC Account Factory product vends a fresh AWS sub-account under the operator's AWS Organization (CT-managed) → codebuild runs `prod/per_customer/` terraform against the new account → modules deploy. cross-account eventbridge connects customers' agents for purchasing, disputes, and other coordination.

## the protocol

underneath the modules, every transaction reduces to one primitive: a shared object that moves **propose → agree (two-sided) → gate (a real-world condition — funds land, goods ship) → settle (a stream-fired, deterministic handler that books the effect and emits the next event)**. the modules are instances of it — treasury's offer, purchasing's order, invoicing's invoice — and a flow like `inventory → purchasing → invoicing → inventory → accounting` is those commit-nodes wired by addressed-event edges. because every node and edge is an observable event, gerp treats the chain as a **sequence to optimize**, not merely record: the optimizer (`prod/optimizer/`) reads the intent events and improves routing, pricing, and timing across firms. traditional ERP records the sequence; gerp records it and optimizes it.

## manual ERP is first class

users can create an invoice, save it as a pdf and email it to a customer who has no gerp. the
seller's books are the same journal entries either way, and payment closes the receivable the same
way whether a webhook reports it or the owner tells the agent a check was deposited. most
counterparties are not gerps and never will be, so this is the base case, and no flow may exist
that only works when the other side is a gerp.

commerce between gerps is the same ERP treated as an automation protocol: the invoice is addressed
to the customer's gerp as an event, their books record the payable with nobody typing, and paid
comes back the same way. the optimization is layered on the manual case, never a replacement for
it. gradienterp bills its own customers this way — every customer is a gerp, so the hosting
invoice is one firm's sale and another's purchase, delivered by email today and by the addressed
event once that leg is built.

## modules

| module | path | purpose |
|--------|------|---------|
| server | `modules/server/` | per-customer HTTP API gateway. domain modules attach their own routes (`/webhooks/<provider>`, `/iot/<device>`, etc). owner-facing webhook URLs |
| agent | `modules/agent/` | bedrock agentcore runtime + gateway + memory. image pulled cross-account from operator ECR. reads per-customer registry DDB at boot for prompt assembly |
| accounting | `modules/accounting/` | DDB ledger (month-partitioned), journal entries, financial statements. the core — every other module feeds it by invoking `post_journal_entry`; nothing posts to the ledger over HTTP. transforms for stripe/square/paypal/plaid/bofa/wf live here too |
| schemas | `modules/schemas/` | per-customer registry DDB (chart of accounts + contact / calendar / note / task / item field shapes). agent tools: write_schema (op: extend), read_schema (canonical), read_schema (local), write_schema (op: merge). weekly canonical-pull cron invokes the agent for owner-approved canonical updates |
| inventory | `modules/inventory/` | items, stock levels; SOLD/RECEIVED/ADJUSTED movements invoke accounting's post_journal_entry. recipe graph planned. depends on accounting |
| contacts | `modules/contacts/` | vendors/customers/employees. 5 thin CRUD lambdas (get/put/update/query/scan); `entity_type` + `is_<role>` flags, validated against the per-customer registry |
| notes | `modules/notes/` | free-form annotations on subjects (FK columns: contact_id, journal_entry_id, …). append-only versioned (`note_id`+`version_ts`). 5 CRUD lambdas |
| tasks | `modules/tasks/` | todos w/ due_date/priority/resolved_at; lambda-managed open_flag sparse GSI. 5 CRUD lambdas. completion semantics defer to labor |
| calendar | `modules/calendar/` | the per-tenant clock — 5 thin EBS Scheduler passthrough tools + an agent_dispatcher lambda (bridges scheduled fires to InvokeAgentRuntime). all time-fired behavior lands in the customer's schedule group |
| clock | `modules/clock/` | a LIBRARY (no lambdas, no infra) — turns a civil claim into a UTC range on the gerp's own calendar: `period_bounds`, `month_keys`, `to_utc_ms`, `local`. Any code deciding what "this month" or "yesterday" means imports this instead of computing it in UTC. Not to be confused with calendar, which FIRES at times; clock decides where a boundary falls |
| purchasing | `modules/purchasing/` | PO lifecycle, agent-to-agent commerce. depends on inventory + accounting + contacts |
| labor | `modules/labor/` | clock in/out, shift tracking, payroll. depends on accounting + contacts |
| payments | `modules/payments/` | the webhook ingestion layer planned to host ingest_<provider> lambdas; transforms already ship in accounting/lambdas/ingest. depends on accounting |
| invoicing | `modules/invoicing/` | AR lifecycle, billable hours, customer billing. depends on contacts + inventory + labor + accounting |
| treasury | `modules/treasury/` | capital structure as rules — capped, non-voting distribution rules paid from the visible margin (stock-free cap table = a rules-params query); a funds-gated offers→settlement flow creates them, `compute_balances` fires them. the event stream is the prospectus. depends on accounting + rules |
| iot | `modules/iot/` | physical device telemetry (toasters, fridges, badge readers) into the customer's event stream (and the public stream for openly-operated customers). polyglot firmware target — smithy contract layer planned. no upstream deps; downstream consumers subscribe via EventBridge |
| metrics | `modules/metrics/` | the firm's product record — each step a subject takes with the product, in the firm's own words: `POST /metrics` for an app with a bearer, a `record_metric` row on any callsite, the agent's own `record`; through the firm's bus into Parquet under the cabinet, read back with count / distinct / funnel / retention / SQL on the gerp's own Athena workgroup, on the firm's calendar. every read leaves a usage row (payer, bytes). depends on events + server + agent |
| printing | `modules/printing/` | onsite manufacturing — owns the PRINT RUN (recipe+variant, printer, material batch, outcome); the design lives in the standards corpus, the installed part on an asset, the failure on tasks. certification = accumulated service record across firms, not a claim. depends on assets + inventory + the standards corpus; iot deepens the evidence |

each module has its own `AGENTS.md` with detailed design and lambda descriptions.

internal lambda interfaces (`post_journal_entry`, `compute_balances`, `write_schema (op: extend)`, etc.) are contracted by each lambda's `main.py` + `schema.json` + the module's `AGENTS.md`, not by smithy. the public `api.openlyoperated.biz` api is contracted by its OpenAPI document (`prod/api_openlyoperated/api/v1/openapi.json`; a path names its backend folder, terraform builds the rest). smithy is reserved for iot device integration (`modules/iot/`).

## module test

before adding a new module (or defending an existing one), ask:

> **does it enforce an invariant or produce a side effect an LLM can't synthesize from schema alone?**

- **yes** → the module needs real lambdas. examples: accounting (conservation at write time + pair-row decomposition + `is_account` validator), payments-ingest (signature validation + transform dispatch), purchasing (multi-party state machine), invoicing (AR lifecycle), schemas (write_schema (op: extend) writes DDB + emits convergence event, seed_schema bulk-writes canonical baseline at provisioning).
- **no** → the module collapses to a JSON file in `modules/schemas/data/` (canonical schema; seeded into the per-customer DDB at provisioning) + a ddb table + DynamoDB Streams → EventBridge + a small set of parameterized Gateway tools the agent calls directly. examples: contacts, calendar.

the side-effect externalization escape hatch matters. the rule isn't "no side effects" — it's "no side effects *the module itself synthesizes from bespoke code*." if a stream event hands off to the agent or accounting, that's not a lambda this module owns.

## the schema substrate

`modules/schemas/` owns the per-customer registry DDB that holds chart of accounts + contact fields + calendar fields + future shapes. three things flow through it:

1. **seed at provisioning** — `seed_schema` lambda bulk-writes the canonical baseline from operator's S3 (`gerp-canonical-<operator-acct>/<registry>.json`) into the customer's `gerp-schema-<customer_id>` DDB
2. **extend on demand** — agent tool `write_schema (op: extend)` writes a row + emits `platform.schema.extended.v1` event. operator agent watches the cross-customer event stream for convergence
3. **canonical pull weekly** — cron invokes the agent with a "diff canonical vs local, surface to owner, merge approved" prompt. agent uses `read_schema (canonical)` + `read_schema (local)` + `write_schema (op: merge)` tools with owner approval per entry

validators in domain modules read from the DDB at lambda cold start (cached in module globals). `accounting`'s `is_account()` is the first example; same pattern when contacts/calendar field-validators ship.

JSON Schema event-payload contracts (separate concern) live at `/events/<source>/<detail-type>.v<n>.json`.

## the route catalog

`modules/server/` provisions ONE HTTP API gateway per customer. domain modules attach their own routes. see `modules/server/AGENTS.md` for the full catalog (`/journal`, `/webhooks/{stripe,square,...}`, `/pos/<system>`, `/iot/<device>`, `/sched/<schedule>`, etc) and the four-step ingest-lambda contract (sig-verify → transform → state-write → post-journal-entry-or-handle-400).

## key files

- `instructions.md` — full project architecture
- `TODO.md` — root-level POC checklist (per-module TODOs in each module's `TODO.md`)
- `modules/accounting/README.md` — "flow is primitive" design philosophy
- `modules/accounting/lambdas/ingest/transform.py` — provider-webhook → journal-entry transforms (stripe / square / paypal / plaid / bofa / wf)
- `modules/schemas/data/` — chart of accounts + contact / calendar field schemas as JSON files (operator's source of truth; published to canonical S3, seeded into customer DDB at provisioning)
- `modules/server/AGENTS.md` — route catalog + ingest contract
- `modules/treasury/README.md` — the gradient thesis (kirchhoff → capital flow → negentropy)
- `events/AGENTS.md` — JSON Schema event-payload conventions
- `events/{accounting,platform}/<detail-type>.v<n>.json` — event-payload contracts
- `modules/agent/prompts/bookkeeper.md` — the agent's persona and workflows: onboarding, posting, classification
- `tests/testdata/{stripe,paypal,square,bofa,wf,plaid}/` — provider webhook fixtures
- `tests/AGENTS.md` — test authoring conventions (`scratch_env`, `load_lambda`, dev container)

## dependencies

| tool | install | docs |
|------|---------|------|
| terraform | `brew install terraform` | https://developer.hashicorp.com/terraform |
| python 3.12 | lambda runtime, boto3 included | no external deps |
| docker | `brew install --cask docker` | required for `scripts/docker.sh` (the agent image). The local dev stack runs as plain processes — see `scripts/local-dev.sh` |

## local mode

every lambda branches on `IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))`. in prod the handler talks to boto3 clients (dynamodb, s3, ses, secrets manager, lambda invoke); in local mode the same handler reads/writes jsonl files under `out/` and `logs/` (both gitignored). the branch lives at the top of each lambda's `main.py` — no separate local harness file.

state is addressed by `LOCAL_*` env vars so tests and dev harnesses can redirect into scratch dirs:

| env var | default | production analogue |
|---|---|---|
| `LOCAL_LEDGER` | `out/ledger.jsonl` | dynamodb journal_entries (month-partitioned) |
| `LOCAL_PENDING` | `out/pending.jsonl` | dynamodb pending table |
| `LOCAL_BALANCES` | `out/balances.jsonl` | dynamodb balances cache |
| `LOCAL_CLASSIFICATIONS` | `out/classifications.jsonl` | dynamodb classifications table (accounting) |
| `LOCAL_SCHEMA` | `out/schema.jsonl` | dynamodb per-customer registry table (modules/schemas/) |
| `LOCAL_EVENTS` | `out/events.jsonl` | eventbridge `gerp-events` bus (the hub's) |
| `LOCAL_SECRETS` | `out/secrets.jsonl` | secrets manager / per-customer SSM webhook secrets |
| `LOCAL_CONFIG` | `out/config.jsonl` | eventbridge rule per customer |
| `LOCAL_S3` | `out/reports` | s3 report bucket (directory) |
| `LOCAL_LOGS` | `logs` | cloudwatch logs + ses delivery |

tests use `scratch_env()` from `tests/<module>/_helpers.py` — a context manager that points every `LOCAL_*` var at a throwaway tmp dir, unsets `AWS_LAMBDA_FUNCTION_NAME`, and pairs with `load_lambda()` for fresh imports so env changes aren't cached. same handler code runs against `LOCAL_*` paths and against the real AWS substrate.

for the http API surface, `tests/server/per_customer/` IS the per_customer stack, locally: its routes come from the gateway's own OpenAPI export and each one runs the REAL handler in-process against real tables on moto (`image.json`, taken by `snapshot.py`). iteration loop: `bash scripts/local-dev.sh --start` then `curl -X POST localhost:8080/webhooks/stripe -d @tests/testdata/stripe/charge.succeeded.json` — the entry lands on the ledger, readable back through `GET /oob/financials`.

## module conventions

- **versioning via git tags, not directories.** `modules/accounting/` lives at HEAD. stable release points are git tags per module: `accounting-v1.2.0`, `agent-v2.0.1`, `inventory-v0.4.1`. per-customer terraform pins `source = "git::....//modules/<name>?ref=<tag>"`. semver applies: major = contract break, minor = additive (new var with default, new output), patch = internal fix. rolling a customer forward is a ref bump. never duplicate modules into `v001/`, `v002/` — git history already has them.
- **no per-customer tfvars in git.** `.tfvars.example` files are dev templates; real per-customer config lives in SSM Parameter Store at `/gradienterp/customers/<customer_id>` (operator-seeded at onboard by `tower-provision-customer`). the composition layer (`prod/per_customer/`) takes only `customer_id` as a var and looks up the rest. customer identity never enters source control.
- **resource-name prefix + operator account id come from `config.json`.** repo-root `config.json` holds `STACK_PREFIX` (e.g. `gerp`) and `OPERATOR_ACCOUNT_ID` — the single source for both. per-customer modules + tower take `stack_prefix` as a var (threaded by `prod/per_customer/`); the standalone root stacks (`platform/operator`, `api_openlyoperated`, tower) read it via a `jsondecode(file(...))` local. names resolve to `${STACK_PREFIX}-<module>-<customer_id>`, so a rebrand is a one-line flip. the only deliberate literals: state-backend names (`gradienterp-tfstate-*` — `backend` blocks can't interpolate and must match the live state) and module `op_event_bus_arn`/`canonical_bucket` var-defaults (overridden at the root). the operator account id is authoritative in `prod/platform/management` (`aws_organizations_account.operator.id`); `config.json` mirrors it for cross-account stacks.
- **every lambda is a `modules/terraform/lambda` call.** the module owns what every function owns — the artifact pin, the `gerp:src-dir` tag, the log group at `LOG_RETENTION_DAYS` (90; `config.json`), the metric filters on its ERROR lines. the alarms are the root's, ONE per signal per account (`AWS/Lambda Errors` with no dimension is the account's sum; `gerp/app/<gerp> ErrorLines` the caught failures) to `OPS_ALERTS_TOPIC_ARN` (the operator's `gerp-ops-alerts`) — alarms bill per alarm, and the log group names the function. `prod/per_customer` threads the retention and the topic to every module like `stack_prefix`; the operator stacks read them off the same `config.json` local. a bare `aws_lambda_function` anywhere fails `tests/tower/local/test_codebuild_source.py`. alarms about the app (a duration, a queue's age) live in the module's own `alarms.tf`.
- **a refusal is a 4xx; a failure is one `[ERROR]` line with the ids, then the shape's exit.** a refusal is the caller asking for what the system says no to (bad input 400, a name that is not there 404, a state that forbids it 409): the body carries the reason, `log.info` at most, never an alarm. a failure is the work not happening because something we depend on broke (a provider call, a downstream invoke, a read that threw): `aws.log.error(msg, **ids)` — the gerp and the function go on the line by themselves, the call site names the row (thread, po_id, item_id, invoice_id) — and then the exit its invoke shape wants: a tool, a route or a direct invoke answers `err(reason, 502)`; an async handler (a rule, a schedule) raises when a retry could succeed; a DynamoDB stream handler goes through `aws.stream_batch` and never raises (`modules/terraform/AGENTS.md` has the table). an `except Exception` that answers a not-found status is split: the specific not-found stays a refusal, everything else is a failure. `log.warning` is a fallback taken and the work still done. the lint is `tests/tower/local/test_handler_exits.py`.
- **every line is one JSON object with a level.** functions run in Lambda's JSON log format (`modules/terraform/lambda`); `aws.log.<level>(msg, **ids)` writes `level`, `message`, `requestId`, `location` (the call site), `gerp_id`, `function` and the ids as top-level fields, so a filter reads `{ $.level = "ERROR" && $.po_id = "…" }` and any log backend parses the line on ingest. the level is the line's kind: `error` a failure, `warning` a fallback taken, `info` the narrative and a refusal. never `print` a string (no level, no fields — the lint refuses it); a `print(json.dumps({...}))` is a data line a filter or a reader consumes (`automate`'s `incident` lines, the send log) and lands as-is. never put JSON in the message: it nests as text and no filter can read it. `log_level` per function in the lambda module is the volume knob (INFO; a noisy function says WARN) instead of deleting lines.
- **a failure record has a `kind`, a `category` and the row's ids.** `kind` is the type — `snake_case`, fixed, what a metric counts, a task keys on and an investigator filters by: a `Kind` declared once in the module's `lambdas/_kinds.py` (`log.error(STOCK_MOVE_FAILED, po_id=…)`, `raise Failure(STOCK_MOVE_FAILED, po_id=…)`), or the slug of a fixed-string message; both accepted, an old site moves to a kind when its module is next touched. `category` is what class of thing broke — `dependency`, `permission`, `config`, `data`, `timeout` — on the kind, or classified from the exception's type and botocore code. `error_type` is the exception's class. an id is named what the table's key is named (`aws.IDS`: the four-id model and `thread`, `po_id`, `invoice_id`, `item_id`, `entry_id`, `task_id`, `contact_id`…); anything else is a value (`aws.VALUES`). `thread=` and `name=` land as `thread_id` and `resource_name` (a LogRecord owns the words), `kind=` on an agreement line as `agreement_kind`. `bind(po_id=…)` at the top of a handler puts an id on every line of the invocation. a `Failure` is the raise that carries the record, so a generic catch (`stream_batch`, a handler-level `except Exception`) writes it whole and puts its fields on the 502 body; a plain exception stays the norm. the checks run where the words are declared: `aws.Kind` refuses a bad declaration at import, `tests/tower/local/test_kinds_registry.py` imports every `_kinds.py`, and under `scripts/test.sh` (`LOG_STRICT=1`) a field outside the vocabulary raises on any exercised path — a new id is added to `aws.IDS`, not invented at a call site.
- **inter-module wiring uses terraform outputs/inputs, not data sources.** if `modules/accounting` depends on `modules/schemas`'s write_schema (op: extend) function arn, accounting's `variables.tf` declares it as input — the dependency is visible in the contract. SSM data sources are for *tenant metadata* (operator-set, ambient config); terraform outputs are for *resource identifiers produced by an apply*.
- **routes attach to the shared per-customer API.** domain modules don't own their own gateway — they own routes on `modules/server/`'s api. see `modules/server/AGENTS.md` § "attaching a route".
- **a GSI needs its own arn in the IAM policy.** `dynamodb:Query` on `table/X` does **not** grant Query on `table/X/index/*` — adding an index to a table a lambda already reads gets AccessDenied until the index arn is added alongside the table arn. This has cost twice (the contacts `account-index`, invoicing's `item-index`) and the second time cost extra: after fixing the policy it kept failing, because a **warm execution environment holds the credentials it booted with**. Force a fresh one (any config update — a no-op env var change) rather than waiting for propagation.
- **registry validation at lambda cold start.** lambdas that need to validate against the per-customer registry (e.g., accounting's `is_account`) query the DDB once at cold start and cache in module globals. warm invocations are zero-DDB.
- **module test** (canonical — see existing section). a module needs real lambdas only if it enforces an invariant or produces a side effect the LLM can't synthesize from schema alone.
- **the fleet self-describes through tags — `scripts/tags.json` is the schema** (which tags exist, what each value means, which resource types must carry which). Enums live there so a reader gets them in one look and the lint reads the same values it documents; this file carries only why they exist. A tag is a DECLARATION by a deployed resource about itself, which is why nothing here keeps a list: `deploy.py` has no list of 122 lambdas, `reset_dev.py` no list of tables.
- **`gerp:src-dir` — one tag, two jobs**: presence = fleet membership, value = the exact source dir to zip, so a function name never has to follow a convention (`agentcore-<gerp>-chat`, labor's hyphenated `close-handler`, calendar's `agent_dispatcher` all self-describe). Fleet + mapping is ONE query, ~1.5s for 101 functions; deploy fans `update-function-code` over the result, and `status` is that query × batch `get-function` `CodeSha256` vs local zip hashes — direction-aware, no terraform plan. Same discovery idiom as `agent_frame_sink = "true"` (the render_frame sink allowlist).
- **`gerp:layer` — the same trick for STATE instead of code.** One query enumerates the tenant's stores AND classifies each by reset lifecycle (the four layers and what each covers: `scripts/tags.json`). `scripts/reset-dev.sh` (→ `reset_dev.py`) queries the tag and EMPTIES every layer but `config` (`--layer <name>` targets one; `--dry-run`/`--yes`); it empties, never drops (DDB items scanned + batch-deleted per each table's key schema; S3 objects + versions), so table/stream config + bucket policies survive and the tenant stays functional with empty books. the reset serves demo re-seeds AND e2e/integration tests (reset to a known state, then seed). the seed half is `scripts/seed_dev.py` — a general toolkit of composable helpers (`contact` / `item` / `sale` / `bill` / `je` / …) that invoke the live module lambdas, so a seed drives the same write paths a real tenant does; fixtures + e2e tests import it (the demo cafe is `docs/demos/seed.py`, `bash scripts/seed-dev.sh --check` re-prints the TB + P&L). so `reset-dev → seed_dev` is the dev-state loop. the third sibling is `tests/puppet` + `scripts/puppet.sh` — a puppet gerp on the addressed-event boundary that lets a test (or you) play the OPPOSITE side of a cross-firm integration without a second gerp: `--apply`/`--destroy` the capture infra, `--send`/`--poll`/`--receive` the counterparty's events (see `tests/puppet/AGENTS.md`). the tag is MANDATORY — reset warns loudly about any stateful resource missing it (it'd be silently skipped). the target account is hardcoded in `reset_dev.py` (reassign to a dedicated dev account later). caveat: AgentCore Memory events aren't DDB/S3, so `session` clears the saved-chat index + session store but Memory transcripts age out on TTL.

## commands

- `bash scripts/test.sh` — run every module's local-mode suite; `--module <name>`, `--name <substring>`, `--env integ`, `--logs` available. see `tests/AGENTS.md` for authoring conventions
- `bash scripts/deploy.sh status|push [--gerp <gerp_id>]` — artifact deploys (TF owns shape, S3 owns bytes), **no terraform anywhere in the push path**: the builder is `if package.json → zip the dir; elif .py → zip the dir + its transitively-resolved local imports; else exit 1` — **the import graph IS the bundle manifest** (shared `_helpers`/`*_rules`/vendored trees resolve off the code itself; no recipe files, no `archive_file` — those blocks are deleted). Bespoke needs are `match src_dir:` cases in `build_artifact` (scripts/deploy.py) that *do things first* — prune, stage — then call `build_node`/`build_py`; it's a build script, explicit cases at any count are fine. `status` = the `gerp:src-dir` tag query × live `CodeSha256` × artifact checksums (~12s, direction-aware); `push [--dirs …] [--notes …]` = deterministic zips → versioned artifacts + `provenance`/`release` annotations (operator-profile writes; org read) → `update-function-code` at the pinned version (tenant profile); `--deploy` builds and puts nothing, so the functions and the BFF take the artifacts already in the bucket (what `zip.sh lambda` + `upload.sh lambda` put); `--all` pushes every active gerp through its own `gerp-<id>` profile, whose region picks the bucket, and the BFF once. deployed state is never recorded — it's one `get-function` away. **functions SOURCE code from the bucket** (`data "aws_s3_object"` pins latest per function), so ANY applier — codebuild's bundled snapshot, a stale checkout — deploys bucket truth, never its local tree; a NEW function is push-then-apply (the data source fails the plan until its artifact exists). post-push, a plan shows function updates until the next apply — that's the state's `s3_object_version` pointer re-syncing FORWARD to the version the push already deployed (AWS doesn't expose code source, so state can't refresh it); the apply is a same-bytes no-op
- `bash scripts/deploy.sh image` — the agent-container deploy: build → push (auto-incremented immutable `vNN` tag) → CLI-update a gerp's runtime to the new digest, from the copy ECR replication makes in the gerp's own region, once it has arrived (FULL config carried — UpdateAgentRuntime replaces wholesale) → wait READY → re-pin the named endpoint (DEFAULT auto-tracks). The gerp is gradienterp by default (the dogfood takes an image first), `--gerp <id>` for another, `--all` for every active gerp — a fleet push is said, never implied; `--no-build` moves the named gerps onto the image already at the top of ECR, or `--no-build --tag vNN` onto that one. No terraform: the runtime tf reads `data.aws_ecr_image` `most_recent`, so a post-deploy plan is ALREADY clean (AgentCore exposes its config, unlike lambda code). No `agent_image_tag` var exists anymore; no fake gateway-target churn — routine image deploys change zero tf
- `bash scripts/docker.sh --build|--run|--stop|--push <ecr-uri>` — the underlying agent-container build/run primitives (`deploy.sh image` drives build/push; use directly for local container smoke)
- `bash scripts/local-dev.sh --install` — once per clone: creates `.venv` (python3.12), installs the tests', the agent container's and the local servers' python deps, and runs `npm ci` in each Node lambda. Every script that runs `.venv/bin/python` (`test.sh`, `deploy.sh`, `investigate.sh`) needs it
- `bash scripts/local-dev.sh --start|--status|--stop` — the local dev stack as plain processes: moto :5000, the owner-app BFF :3000 (sign in at `/dev/login?sub=local-dev`; `/dev` lists the rest), the per_customer stack :8080, the Stripe stand-in :4242, the Cognito stand-in :4243, openlyoperated.biz :3001, and the pump (no port)
- `bash scripts/e2e.sh [--env local|prod] [--suite smoke|full|lifecycle] [--configure]` — the browser suite (`tests/e2e`); local by default, starting the local stack when a surface is down; `--configure` installs the suite and writes the local seed (`LOCAL_GERPS`, a local owner of gradienterp) into `.env`
- reading the fleet — open alarm tasks, a day's errors across every gerp, the agent's tokens, this month's cost per gerp: the commands are in `prod/platform/operator/AGENTS.md` § reading the fleet
- `python3 tests/server/per_customer/snapshot.py` — re-take the per_customer manifest (routes + every function's env) from the running stack
- `bash scripts/zip.sh source|lambda <src-dir>…|bff` — every zip a deploy makes, into `.build/`, no AWS: `source` is the working tree as git lists it (tracked and untracked, nothing gitignored; the lock files of the roots CodeBuild applies left out) with `source.json` (commit, uncommitted changes, `--dirs`) and `deploy-dirs.txt`; `lambda` and `bff` are the bytes `deploy.sh push` builds
- `bash scripts/apply.sh --stack <stack> [--plan]` — every terraform apply the operator starts, the same from any tree on any machine: `per_customer` (`--gerp <id>|all`, `--action apply|stop`; an apply of a stopped row is the start, a `closing` or `closed` row and a stop of the seller's own gerp are refused) and `hub` (`--region`) build in CodeBuild from the latest `upload.sh source`, `--build` (this tree, zipped and uploaded first) or `--source-version`; the operator stacks (`api_openlyoperated`, `dns`, `email`, `gradienterp_cloud`, `openlyoperated_biz`, `optimizer`, `platform/operator`, `tower`, `platform/management`) run init, a plan to a file and its apply in place as the `default` profile, printing resource addresses and the `Plan:` line. Nothing waits for approval. Their live values are tracked in `config.json` (`PROVISION_QUEUE`, `CLOSURE_ENABLED`, `CHAT_CALLBACK_URLS`, `CLOSURE_REQUESTER_ACCOUNTS`); only `platform/management` reads a local `terraform.tfvars`, and without it its plan fails
- `bash scripts/workflow.sh run|wait|log` — GitHub workflow runs. `run deploy.yaml|apply.yaml [--dirs <src-dir>…] [-f key=value …] [--no-wait]` zips and uploads the working tree, committed or not, starts a run on that version and waits on it: one exit status for the run and its failing steps on a failure (`-f source_version=` names an earlier upload instead). `wait <run-id>` waits on any run; `wait --commit [<sha>]` waits on every run a push starts — after pushing, in the background, one notification when CI is done. `log <run-id> [--failed]` reads a run's log. `deploy.yaml` (`gerp`: an id, a comma list or `all`; `image`: `none|build|no-build`) runs one job per gerp at once — the `--dirs` pushed, the BFF once, the modules' integ tests once — and builds the agent image on an arm64 runner, then moves each gerp's runtime onto that tag in its own job; `apply.yaml` (`stack`, `gerp`, `action`, `region`, `plan_only`) runs that tree's `apply.sh`; `playbooks.yaml` (`gerp`) runs on its checkout, no upload: `sync_playbooks.sh` into each named gerp's knowledge base, one job per gerp — it also runs on every push to `main` that touches a `modules/**/kb.md`, and from `deploy.yaml` with `-f playbooks=true`. All run in the `prod` environment through `gerp-github-deploy` (`.github/actions/aws`)
- `bash scripts/upload.sh source [--release]|lambda <src-dir>…|bff|assets|image` — every put a deploy makes, operator credentials: `source` puts `.build/source.zip` at `source.zip`, the key `apply.sh` builds from, and refuses a zip that is not this tree (another commit, a change since); `--release` refuses a tree with uncommitted changes and puts it at `release/source.zip`, both CodeBuild projects' own location, so what a signup, a hub vend and a closure build from; `lambda` and `bff` put the `.build` zip through `push`'s put, refusing one the tree no longer builds to; `assets` syncs the demo gifs and invalidates what changed; `image` pushes the local agent image under the next `vNN` tag
- `bash scripts/bedrock-authorize-anthropic-org.sh` — one-time-per-org call to cascade Anthropic FTU approval to every member account
- `terraform validate` — run from `modules/*/infra/`; every directory at once, in parallel, is `bash .github/workflows/tf-validate-all.sh`, which `.github/workflows/terraform.yaml` runs on every pull request after `terraform fmt -check -recursive` (format with `terraform fmt -recursive`); it reads `validate -json` and a warning diagnostic fails it, so the tree writes nothing the pinned providers deprecate (`data.aws_region.*.region`, `key_schema` inside an index) and validate says nothing when nothing is wrong. every directory's lock file names one version per provider and carries `h1:` for `darwin_amd64` and `linux_amd64`, written by `bash scripts/tf-version.sh` (on a runner: `gh workflow run tf-lock.yaml -f ref=<branch>`, which pushes them back; `--summary` prints what is pinned where); `tests/tower/local/test_codebuild_source.py` checks each directory has one with both. terraform's one version is `config.json` `TF_VERSION`, read by the buildspecs and the workflows
- `terraform plan|apply` in a stack's dir — the debug surface behind `apply.sh`; which base profile each stack takes is `prod/AGENTS.md` § local apply

## the accounts

- AWS Org under Control Tower. Management `335667362239`; Control Tower's `audit` `002904791175` and
  `log_archive` `238599091185`; operator `185369506315` (tfstate,
  `gerp-customers`, the owner app, the collector); a hub per region (`config.json` `HUBS`; the
  first, `582129522725`, in us-east-1 — the bus its region's gerps put to, its edges); gradienterp
  customer `867637277314` — the dogfood, fully provisioned via `prod/per_customer/`; the staging gerp `westwood-c40fd8` (`222165865776`,
  us-east-1); the Irish gerp
  `dublin-test-roasters-d542eb` (`832348493159`, eu-west-1) — the first outside us-east-1. No
  real customer bookkeeping yet (the books carry the demo cafe); destroy / re-apply is reversible
  churn.
- **reaching an account:** `bash scripts/awsacct.sh <target>` points `[profile current]` at
  `management`, `operator`, `hub:<region>` or a gerp id and prints the caller identity; `--list`
  shows the targets, `--all` writes a named profile per target (`operator-org`, `hub-<region>`,
  `gerp-<gerp_id>`). A machine needs only `[default]`, the management account's credentials: every
  other profile is a role chain written from `config.json` and the gerp's row. `current` is for
  reads — a command that changes AWS names its gerp (`deploy.sh push --gerp <gerp_id>`).
- `prod/per_customer/` is the canonical bring-up. Adding a module = a `module` block + outputs +
  (if it has a `<module>_fields` registry) a canonical-S3 upload + a reseed.
- The four-id model: `account_id` (login) / `gerp_id` (instance) / `gerp_profile_id` (public) /
  `aws_account_id` (infra).

## the regions

A region is an entry in `config.json` (`REGIONS` with its `model`, `HUBS`, `OAM_SINKS`,
`OPS_ALERTS_TOPICS`), and a gerp is built where its owner's country is: a customers OU per
region, the operator's bucket / sink / topic / ECR replica per region, a hub per region, global
services in us-east-1. The map of every piece, adding a region and the measured numbers:
`prod/tower/region/AGENTS.md`.

## the gerp lifecycle

One row on `gerp-customers` (operator account) carries a gerp from purchase to the account's
end, and `status` is the spine. Each step is owned by one component; the row says where a gerp
is, and the docs named say how. Nothing moves the status backwards except the operator's
stop/start loop, which is the same stack destroyed and applied again into the same account.

| status | what moves it there | owner | detail |
|---|---|---|---|
| `awaiting_payment` | Create a gerp: the row, the legal profile, the seller's contact | the owner app's BFF | `prod/gradienterp_cloud/AGENTS.md` § the gerp row, by who writes it |
| `queued` | a card lands; the BFF sends the provisioning payload to tower's `tower-vends` queue. The card says how many gerps are in line before it | the owner app's BFF | `prod/tower/AGENTS.md` § the vends queue |
| `provisioning` | the provisioner takes the message (four at a time; Control Tower runs five account operations at once), vends the AWS account (Account Factory, ~15 min), seeds the tenant blob, grants model access, starts the build | tower `provision_customer` | `prod/tower/AGENTS.md` § what `provision_customer` does |
| `active` | the build applies `init_customer` (the export bucket and lambda, which outlive the stack) then `per_customer` in one pass, syncs the guides, stamps `gateway_url`; the ready mail goes once | `tower-per-customer`, `TF_ACTION=apply` | `.codebuild/per-customer.yml`; `prod/tower/AGENTS.md` § one account, many stacks; § the owner hears from the operator |
| `stopped` ⇄ `active` | the operator's `apply.sh --stack per_customer --gerp <id> --action stop` (the export, the row, the destroy) and the apply of the stopped row (`apply.sh --stack per_customer --gerp <id>`). Nothing scheduled, nothing mailed; the settings go with the stack and the onboarding line restores them | `TF_ACTION=stop` / `apply` | `prod/tower/AGENTS.md` § one account, many stacks |
| `close_requested` | the owner types the phrase, or an invoice is 15 days unpaid; the BFF hands it to the seller gerp's `closure/begin.py`, which records the case, starts the build and schedules the notices (every 3 days) and the close (at `closes_on`) | the BFF, gradienterp's closure scripts | `modules/automation/AGENTS.md` § one closure, two reasons |
| `closing` | the build has exported the books (the row carries `export_id`, `download_until`) and is destroying `per_customer` | `TF_ACTION=destroy` | `modules/export/AGENTS.md` |
| `closed` | the destroy finished. The card still opens: the export downloads for fifteen days off `init_customer`, which stands | the build's post_build | `prod/gradienterp_cloud/AGENTS.md` § the gerp row, the card table |
| the account closed | day 15: `closure/close.py` invokes tower's `close_account`, the one `organizations:CloseAccount`; the slot is held 90 days | tower `close_account` | `prod/tower/AGENTS.md` § closing an account |
| swept | day 30: the export bucket and `init_customer` go — not built | — | `modules/export/TODO.md` |

Every step re-reads before it acts and each is heard when it fails (`prod/tower/AGENTS.md` § a
failure reaches a person). The status the card renders, one line each, is the table under
`prod/gradienterp_cloud/AGENTS.md` § the gerp row, by who writes it. westwood (`westwood-c40fd8`) has run the whole arc once and
runs the stop/start loop between sessions.

## decisions (locked — don't re-litigate)

- **the repo is public, and these identifiers stay in it:** AWS account ids, the organization id,
  ECR repository urls, Cognito pool and client ids, the Stripe publishable key, the events subscribe
  key in the openlyoperated.biz page, and the controller's name and mailing address in
  `docs/PRIVACY.md`. None of them grants access. Secret values, credentials and people's personal
  details stay out of the repo, with test fixtures masked.
- **account vending: Control Tower Account Factory**, not DIY create-account. `OperatorOrchestration`
  lands via a customers-OU stackset. AFT rejected (its per-account `.tf` leaks the customer roster).
- **one shared event bus**, flag-on-event, rule-as-gate — not per-customer buses (300-rule/bus cap).
- **a turn is the last line of defense, not the path.** Anything a firm can state as a policy runs
  as a rule at a callsite with no model call — a counterparty's proposal (`PROPOSAL#<kind>`), a gap
  on the shelf (`REORDER#<item>`) — and the agent is woken only for what no row permits an answer
  to. Rules permit, never refuse (`modules/rules/AGENTS.md`); and a poked turn, with no owner in
  the conversation, commits the firm to nothing — accept, counter and decline are the owner's
  word, through the agent. Map a flow hop by hop: rule, physical act, or turn, and push toward
  rules.
- **per-business reads are module-owned + in-account** — each module carries an `oob.tf`; the
  operator holds only a directory + aggregate counters, never a central read API or a stored copy.
  `openly_operated` gates every exit, read per-request. Two scopes, two consents: the economic
  aggregate is terms of use (schemaless counters, no opt-in, no k-floor), per-business detail is
  opt-in, read through from the gerp's own tables; the personal namespace (contacts / legal /
  secrets) is never served. How-to: `modules/server/AGENTS.md`.
- **cross-firm = agree-and-settle, async, on ONE shared agreements store**: every kind's
  `(thread, terms_hash)` rows live on `gerp-agreements-<gerp>`; `request`/`accept` services front
  the gateway targets, one `apply_inbound` takes counterparty stamps, one `settle` dispatches each
  side's domain effect via `AGREEMENT#<kind>` config rows. A peer event writes into the recipient's
  account (the hub's spoke edge → the recipient's own bus → its consume rule → inbox → router); the
  recipient's own stream pokes its own agent — no sync
  invoke. `modules/agreements/AGENTS.md`.
- **registries**: canonical JSON in `modules/schemas/data/` → S3 → per-customer DDB (seeded
  canonical, agent-extended). Not baked-at-build, not env-var (4KB cap).
- **operator SES goes to PRODUCTION; customer SES stays in SANDBOX forever.** The operator must
  reach strangers who have verified nothing — that is what signup is. A gerp's agent must not, and
  sandbox is the enforcement: it can only mail identities someone deliberately verified
  (`modules/agent/infra/email.tf`, on `<gerp_id>.agents.gradienterp.cloud`). Sandbox status is
  per-account, so the two never collide. A customer account in sandbox is not an oversight.
- **the private/public line**: four field classes (`economic` / `operational` / `subject` /
  `secret`) plus a shape (`copied` / `referenced`) on every registry field; absent class means
  secret in the READER. A key may carry a person's `contact_id` and never anything derived from who
  they are. Free-form text publishes as a template with its values held separately.
- **bank feed = operator Plaid gateway + per-gerp reconcile**; cash recognition B (`payout.paid →
  CASH_PENDING`, bank deposit → `CASH`). `connect_bank` routes institution → connector.
  `modules/accounting/AGENTS.md`.
- **per-customer sub-account = one `prod/per_customer/` stack** (one tfstate each).
- **gateway targets live in the domain modules** — each tool's `schema.json` is source; the shared
  request/accept lambdas get their invoke grant in `modules/agreements`.
- **signup creates an account only**; a gerp is bought on Create a gerp and vended when the card
  lands.
- **design for cross-org, don't build it** — org-scoped IAM uses list-form `PrincipalOrgID`,
  rendezvous through the bus, not RAM.
- **`gerp-*` naming is config-driven** (`STACK_PREFIX` in `config.json`).
- **substrate stays Terraform.** Deploys leave TF via the artifact bucket: TF owns shape, S3 owns
  bytes; the agent image is one command.
- **AWS invoice units, not Billing Conductor**, for the hosting bill; the fee is cost x 1.2 issued
  through gradienterp's own gerp; no payment terms, the saved card charged on issue.
- **hosted checkout, not Elements** — no Stripe.js in the SPA, no card fields, SAQ A.

## gotchas (baked into code — don't "clean up" away)

- `aws_bedrockagentcore_gateway_target.description` caps at **200 chars** — and AWS counts an
  em-dash as 2, so lint's char count can pass at 199 while the apply fails. Keep them ASCII / under
  ~195. Under `-target` a too-long description silently prunes the target.
- Gateway MCP `tools/list` **paginates at 30/page**; `entrypoint.py` follows the cursor.
- AgentCore Runtime env cap is **4000 bytes** — registries bake into the container, not env vars.
- AgentCore doesn't forward the invoke `accept` header — streaming signals via a **payload flag**;
  after an image bump a warm `session_id` sticks to the old container.
- `seed_schema` is all-or-nothing idempotent — a new registry on a live customer needs a manual
  per-registry reseed.
- `post_journal_entry` **rejects non-positive legs** — a negative leg passes `debits==credits` but
  inverts balances; transforms decode sign into DR/CR with positive magnitude.
- A presigned S3 **GET** of an SSE-KMS object needs **SigV4**.
- The region pins share one exceptions list (`prod/platform/management/main.tf`
  `local.region_pin_exceptions`); `bedrock-agentcore:*` stays pinned. A cross-region call a gerp
  makes on purpose is an entry there, never a per-account grant.
- Gateway target first-apply can hit IAM eventual-consistency — re-run apply.
- Non-empty ECR / S3 buckets block `terraform destroy` — `force_delete` / `force_destroy`.
- A fresh DDB-stream ESM at `LATEST` skips rows written while it's still enabling — touch a row to fire.
- A test file with no `if __name__ == "__main__"` block runs NOTHING and still counts its tests as
  passing; `scripts/test.sh` fails such a file rather than counting it.
- A module-level `depends_on` (automation → agent, payments) defers every data source in that
  module while a dependency has pending changes, so a plan shows its env maps and policies as
  "(known after apply)". Not drift: apply the pending change and the next plan is clean.
- The terraform registry lists a provider release about a day after the GitHub tag, and the tag
  shows 0 assets even then. `aws_bedrockagentcore_gateway_target` imports as `<gateway>,<target>`.
