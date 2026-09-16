# events

**The protocol is the fast path, not the requirement.** A firm whose customers and suppliers are not gerps — which is nearly every firm, and nearly every counterparty — must be fully served by gradientERP alone: an invoice is emailed, a deposit is described to the agent, the invoice is closed as paid. Cross-firm is an optimization layered on that base case, never a precondition for it. So a recipient that cannot be resolved is ordinary and not an error; the failure worth alarming on is a gerp that SHOULD have received an event and did not. `modules/invoicing` is the model — it documents `manage_invoice (op: create)` as "the direct (non-agentic) entry" and keeps that flow first-class beside the agreement-driven one.

**Standing doctrine (2026-07-17): every feature built from here on states how its events serve cross-firm coordination.** The full inventory of cross-firm matches — textbook ERP objects the optimizer pairs across gerps — is `TODO.md` § cross firm biz optimization; a new emitter should name its row (or add one). Speculative schemas are welcome there; unused lambdas are not.

the event system: where changes originate, how they travel, and the conventions every emitter
follows. two things live here —

- **the topology** — the full source→sink map. it is deliberately *not uniform*: DDB streams,
  API-Gateway webhooks, EventBridge Scheduler, lambda completion-invokes, and direct PutEvents
  all coexist, because the right transport differs by source.
- **the bus conventions** — shape rules for the shared EventBridge bus `gerp-events` (the hub's,
  `prod/hub`, one per region). the hub's edges, the operator's consumers and the publication
  rule all depend on them.

## current features

no lambdas. one resource (the firm's OWN event bus) and one shared library. this module *defines* the eventing contract, owns the substrate the in-firm half needs, and owns the code that builds an event; emitters live in the producing modules and call in here. what exists today:

- **the firm's own bus** (`infra/main.tf`, `<prefix>-internal-<gerp>`) — messages between two modules of ONE firm, and nothing else can see them. NOT the account's `default` bus: `MatchedEvents`/`TriggeredRules` are per-bus, so on `default` ours would be mixed in with every service event the account emits and an archive of it would drag the same noise into a replay. no dependencies, so it applies before anything that produces or consumes on it.
- **`events.py`** — the one place an event is built and sent, bundled into an emitting lambda by the same import-glob tier that carries `aws.py`. Three functions, one per REACH, so a caller picks the bus by which one it calls rather than by which env var it reaches for: `emit(source, detail_type, detail)` on the firm's own bus, `emit_to(source, recipient, …)` addressed on the shared bus (stamps `from`/`to`), `publish(source, …)` on the shared bus (stamps `schema_version` / `openly_operated` / `customer_id`). It owns local mode, and it NEVER raises: emission is downstream of the durable write, so a failure to announce must not report the write as failed. A `put_events` failure prints the line `create_inc_from_log` reads, keyed `event:<source>.<detail-type>` — a bus refusing every emit of a type collapses into one incident instead of thousands of log lines.
- **two buses, two blast radii** — the firm's own for in-firm; the hub's `gerp-events` for anything addressed to another firm or published to everyone. the firm's own bus also admits `events:PutEvents` from the organization: the hub's spoke edge puts what is addressed to this firm there, and a receiving module's rule (the inbox's `consume`) takes it from there. a module gets `events:PutEvents` on one bus ARN by name, so one wired only for in-firm messages gets AccessDenied from the shared one.
- **the in-firm producer is a RULE, not a module** — a firm attaches an instance to a state transition and that row is the whole configuration (`modules/payments/collection_rules.py` is the first). the transition is a rule callsite that runs after the write, so what is announced is already true.
- **a consumer that can fail must configure the durable half itself.** EventBridge invokes a Lambda target ASYNCHRONOUSLY, so the rule's own DLQ covers DELIVERY only — a target that does not exist, a missing permission. a handler that THROWS is Lambda's business: it gets Lambda's async retries (2, not raisable) and is then DISCARDED unless the function has an `aws_lambda_function_event_invoke_config` with an on-failure destination. measured, not assumed. `modules/payments/infra/collection.tf` is the worked example.

- **bus conventions** for `gerp-events` — envelope (`source` = `<module>`, `detail-type` = `<resource>.<action_past>`) + required `detail` fields (`schema_version`, `openly_operated`, `customer_id`).
- **emission rule** — emit *after* the durable write; fire-and-forget (PutEvents failure → log + alarm, never fail the operation).
- **publication rule** — `detail.openly_operated == true` → Firehose → S3 archive (applied, read by nothing).
- **the stream** — the operator's `publisher` rule takes every event with `detail.customer_id` and no `detail.to` onto the Events API (`prod/api_openlyoperated`): each `detail.counters` entry to `/oob/counters` with no gerp id; the event itself to `/oob/<gerp_id>/<kind>` when the gerp row reads `published`, its detail projected through the event's contract. **The contract is the channel filter**: a property the schema does not name is dropped, and a property marked `"class": "subject"` (a person reference — `worker_id`, `holder`, `created_by`) or `"class": "secret"` (`escalation.raised.private`) is dropped; a kind with no contract has no channel. A new event kind is on the stream the day its contract lands, and a person-referencing field carries its class in the contract.
- **shipped JSON Schemas** — `accounting/journal_entry.posted.v1`, `platform/registry.extended.v1`, `treasury/distribution.paid.v1`.
- **per-customer wiring** — `CUSTOMER_ID` + `OP_EVENT_BUS_ARN` env on every emitting lambda; `openly_operated` read from SSM tenant metadata at cold start.
- **local mode** — emit appends to `LOCAL_EVENTS` jsonl (default `out/events.jsonl`).
- **source→sink topology map** (below) — the non-uniform ingress/egress table across the durable (ledger) + visibility (bus) planes.

## the two planes

- **durable plane — the ledger.** ingress (webhooks, DDB streams, crons, the agent) funnels through `post_journal_entry` into the append-only ledger over synchronous, retried paths. nothing here may drop; projections (balances, cap accumulators) fold from it.
- **visibility plane — the bus.** `gerp-events` is fire-and-forget: emit *after* the durable write, on PutEvents failure log + alarm. it feeds the public archive (`openly_operated` → Firehose → S3) and soft cross-module routing — never a financial trigger.

## sources & sinks

### ingress — where events originate (non-uniform by design)

| mechanism | source | path → handler | plane |
|---|---|---|---|
| DDB Stream + ESM | `labor.time_entries` (filter `status=closed`) | stream → `close_handler` → `post_journal_entry` (DR WAGES_EXPENSE / CR WAGES_PAYABLE) | durable |
| DDB Stream (reserved) | `accounting.ledger` (NEW_IMAGE), `contacts`, `notes`, `tasks` | stream on, no consumer yet — future EventBridge Pipe → bus for fan-out | — |
| API-GW webhook | Stripe / Square / PayPal | `/webhooks/{stripe,square,paypal}` → `ingest_*` → `post_journal_entry` (dedup `${provider}#${event_id}`) | durable |
| API-GW route | internal | `/journal` → `post_journal_entry` | durable |
| EventBridge cron | `report_pending` (7d), `get_statement (balances)` + `the get_statement suite writer` (`reporting_schedule`) | schedule → lambda | durable |
| EventBridge Scheduler | `canonical_pull` (7d), `rule_params_seed` (7d) | schedule → `canonical_pull_invoke` / `seed_schema` | durable |
| EventBridge Scheduler (per-tenant) | `calendar` group | schedule → `agent_dispatcher` → bedrock-agentcore runtime | durable |
| lambda completion-invoke | `get_statement (balances)` finishes | async `Event` invoke → `the get_statement suite writer` (+ treasury's `distribution` handler) | durable |
| S3 notifications | — | none wired; report + archive buckets are sinks, not sources | — |

### egress — where events go

| emitter | source / detail-type | sink |
|---|---|---|
| `post_journal_entry` (direct PutEvents) | `accounting` / `journal_entry.posted` | `gerp-events` bus |
| `write_schema (op: extend)` (direct PutEvents) | `platform` / `registry.extended` | `gerp-events` bus |
| bus publication rule (`detail.openly_operated=true`) | — | Kinesis Firehose → S3 archive `gerp-events-archive-<op>` (date-partitioned, gzip); read by nothing |
| operator `publisher` rule (`detail.customer_id` set, no `detail.to`) | — | the Events API: `/oob/counters` (every counter delta) and `/oob/<gerp_id>/<kind>` (a published gerp's events, projected through their contracts); `GET /v1/events` relays a channel as SSE |

### the cross-invoke hub

`post_journal_entry` is the durable hub — payments, inventory, labor (`close_handler`,
`pay_run`), and `classify_pending` all `lambda.invoke(RequestResponse)` it. that
synchronous funnel, not the bus, is what gets a financial event into the ledger exactly once
(dedup by `entry_id`). the bus only *announces* what the ledger already recorded.

### where treasury fits

distributions fire on the **durable** plane: the `distribution` handler posts DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE via `post_journal_entry`, then *emits* the visibility event `treasury` / `distribution.paid`. treasury never *triggers* off the bus.

---

# bus conventions

every module that emits to `gerp-events` follows the conventions below.

## envelope

eventbridge controls these. modules set them as:

| field | format | example |
|---|---|---|
| `source` | `<module>` | `accounting` |
| `detail-type` | `<resource>.<action_past>` — lowercase, dots between hierarchy, underscores within | `journal_entry.posted` |
| `account` | customer's aws sub-account id — auto-set by PutEvents | — |
| `time` | iso timestamp at PutEvents — auto-set | — |

split across both envelope fields rather than smushing into one: lets EB rules subscribe by either dimension independently. `{"source":["accounting"]}` filters on producer; `{"detail-type":[{"prefix":"quote."}]}` filters on resource across modules.

every hub's bus is shared org-wide; its policy allows any org member to PutEvents. an addressed event is put on the RECIPIENT'S hub: `emit_to` reads the recipient's row in the platform directory (`gerp-directory`, an operator table with a replica per hub region, `DIRECTORY_TABLE_ARN`), which names its hub and that hub's bus arn, stamps `to_hub` and `from_hub` beside `to` and `from`, and puts there with a client for that region. a recipient with no row (no such gerp, or closed) is refused at the sender. the emitters that address (agreements, invoicing, purchasing) hold PutEvents on every hub's `gerp-events` by name and GetItem on the directory; the customers OU's region pin excepts `events:PutEvents`, since the put lands in the recipient's region (`prod/platform/management`). measured: a `po.proposed` from gradienterp (us-east-1) was a row in the Irish gerp's inbound table (eu-west-1) one second after the request returned. what leaves a hub is an edge: a rule per spoke whose target is that gerp's own bus, and one forward to the operator's `gerp-operator` bus for everything unaddressed (`prod/hub/AGENTS.md`). hubs hold nothing about each other.

## detail conventions

every `detail` payload carries these fields regardless of event type:

```json
{
  "schema_version": 1,
  "openly_operated": <bool>,
  "customer_id": "<customer-id>",
  ...event-specific fields...
}
```

- **`schema_version`** — bump on breaking shape change for this event type. consumers branch on it.
- **`openly_operated`** — the archive rule's filter (`{"detail":{"openly_operated":[true]}}`), set at emit time. Nothing reads the archive; the stream gates on the gerp row's `published` instead, so the stamp is on its way out (`prod/api_openlyoperated/TODO.md`).
- **`customer_id`** — redundant with envelope `account` but saves consumers from joining account-id → customer-id.

deliberately not in detail:
- **PII** — names, contact info, free-form notes that may carry identifiers. A field that references a person carries `"class": "subject"` in the contract and never reaches a channel.
- **operator signals** — a contract with `"audience": "operator"` at its top (`platform/registry.extended`) is for the operator's consumers and has no public channel, whatever the gerp's publish setting.
- **internal storage shapes** — e.g. accounting's pair-decomposed ledger rows. implementation detail, not contract.

## schema source of truth

each event type has a JSON Schema file under this module:

```
modules/events/<source>/<detail-type>.v<n>.json
```

shipped today: `accounting/journal_entry.posted.v1.json`, `platform/registry.extended.v1.json`, `treasury/distribution.paid.v1.json`. the filename must equal `<source>/<detail-type>` (`registry.extended` is the emitted detail-type).

versioning by filename. breaking change → new `.v2.json`, emit both during transition, retire `.v1.json` once no consumers remain.

emitters do **not** validate at runtime. they construct a plain dict and call PutEvents. no pydantic, no jsonschema, no extra zip weight, no cold-start cost.

tests enforce shape. `tests/<module>/_helpers.py` exposes `validate_event(detail, schema_path)` wrapping `jsonschema` (the schema path is `REPO_ROOT/modules/events/<source>/<detail-type>.v<n>.json`). each emitter has a happy-path test that asserts its output conforms.

drift modes:
- emitter changes, schema doesn't → test fails ✓
- schema changes, emitter doesn't → test fails ✓
- both change in sync → test passes (intended evolution)

one source (schema), one enforcement (tests). emitter is implementation.

ad-hoc validation against the schema files themselves uses the sourcemeta `jsonschema` CLI (`brew install --cask sourcemeta/apps/jsonschema`):

```
jsonschema metaschema modules/events/<source>/<detail-type>.v1.json   # check schema is valid Draft 2020-12
jsonschema validate   modules/events/<source>/<detail-type>.v1.json sample.json
```

## emission rules

modules call `events:PutEvents` only after the operation succeeds — not on validation error, not on idempotent duplicate, not on partial state (e.g. accounting's `pending_classification`). publication implies the operation actually happened.

**failure mode: fire-and-forget.** publication is downstream of the source-of-truth write. if the bus is unreachable, log + cloudwatch alarm, do not fail the underlying operation. the journal (or stock ledger, etc.) is authoritative; the bus is for visibility, not durability.

## per-customer wiring

per_customer/ terraform sets two env vars on every emitting lambda:

| env var | source | purpose |
|---|---|---|
| `CUSTOMER_ID` | per_customer's `customer_id` input | populates `detail.customer_id` |
| `OP_EVENT_BUS_ARN` | `prod/platform/operator/`'s `op_event_bus_arn` remote-state output | target for PutEvents |

`openly_operated` is read from SSM at `/gradienterp/customers/<customer_id>` and cached as a module global at lambda cold start. flag-flips propagate without redeploy via the next cold start.

lambda IAM needs `events:PutEvents` on `OP_EVENT_BUS_ARN` and `ssm:GetParameter` on the tenant metadata key.

## local mode

emit appends to `LOCAL_EVENTS` jsonl (default `out/events.jsonl`), same pattern as `LOCAL_LEDGER` / `LOCAL_PENDING`. tests use `scratch_env()` to redirect into tmp dirs, then read the jsonl and validate against the schema.

## reference example — journal_entry.posted

emitted by `modules/accounting/lambdas/post_journal_entry/main.py` after a balanced entry is written to the ledger.

```json
{
  "schema_version": 1,
  "openly_operated": true,
  "customer_id": "gradienterp",
  "entry_id": "uuid-or-supplied",
  "posted_at_ms": 1746547200000,
  "origin": "stripe",
  "line_items": [
    {"account": "cash", "accountType": "ASSET", "side": "DEBIT", "amount": 4.50},
    {"account": "sales_revenue", "accountType": "REVENUE", "side": "CREDIT", "amount": 4.50}
  ]
}
```

authoritative spec: `modules/events/accounting/journal_entry.posted.v1.json`.

## adding a new event type

1. **schema** — create `modules/events/<source>/<detail-type>.v1.json`. strict mode (`additionalProperties: false`), required fields enumerated, enums where the value space is fixed. validate the schema itself: `jsonschema metaschema <file>`.
2. **emit call** — `import events` and call the function for the reach: `emit` in-firm, `emit_to` addressed to one firm, `publish` to everyone. The envelope, the bus, the fire-and-forget and local mode all come with it; the caller passes only its own source and the event-specific fields. Do NOT hand-roll a `put_events` — four modules each did, byte-identical apart from the source literal, and two of the four docstrings ended up describing local-mode behaviour none of them implemented.
3. **per-customer wiring** — emitter's terraform adds `events:PutEvents` to its IAM role policy (resource = `var.op_event_bus_arn`) and `ssm:GetParameter` on the tenant metadata key. `CUSTOMER_ID` + `OP_EVENT_BUS_ARN` env vars on the function.
4. **test** — `tests/<module>/local/test_event_shape.py`: invoke the happy path, read `LOCAL_EVENTS` jsonl, validate via `validate_event(detail, schema_path)`. include negative cases (validation error / pending / duplicate paths must NOT emit).

## the event catalog — index

40 shipped schemas. **Detail lives beside the schemas: each source directory has its own
`AGENTS.md`** (`accounting/`, `inventory/`, `purchasing/`, `invoicing/`, `labor/`, `payments/`,
`treasury/`, `iot/`, `platform/`) documenting every event's emitter, firing moment, match, and
open design questions — read the one you're working in. Status: **LIVE** (emitting) · **PENDING**
(schema shipped; the sending code still to add in an existing lambda) · **SPEC** (speculative; shape may
change when built — it sits there asking when it'll be put in the game).

| source | event | status |
|---|---|---|
| accounting | `journal_entry.posted` | LIVE |
| inventory | `stock.moved` | PENDING |
| inventory | `price.updated` / `catalog.published` — **build first** | SPEC |
| inventory | `inventory.low` (pooled purchasing) · `inventory.surplus` | SPEC |
| inventory | `capacity.idle` · `production_order.created` · `reservation.requested` | SPEC |
| inventory | `byproduct.offered` · `lot.recalled` · `asset.listed` · `demand_forecast.published` | SPEC |
| purchasing | `po.proposed` / `po.accepted` (addressed; through agreements) | LIVE |
| purchasing | `quote.requested` / `quote.returned` · `shipment.sent` | PENDING |
| purchasing | `purchase_request.created` · `rfq.issued` (quick auction) · `backorder.created` · `work_order.requested` · `lease_request.created` | SPEC |
| invoicing | `invoice.issued` · `quotation.submitted` · `early_pay_discount.offered` · `contract.offered` (blanket agreement) · `return.rma_created` | SPEC |
| labor | `shift.clocked_in` / `shift.clocked_out` | PENDING |
| labor | `shift.unfilled` · `job_opening.created` | SPEC |
| payments | `webhook.received` (fee-drag signal) | PENDING |
| treasury | `distribution.paid` | LIVE schema |
| treasury | `instrument.issued` / `instrument.transferred` | PENDING |
| treasury | `instrument.offered` | SPEC |
| iot | `device.telemetry` | SPEC |
| platform | `registry.extended` | LIVE |
| platform | `bug.reported` / `feature.requested` (the self-heal intake) | LIVE |
| platform | `module.published` (engineer×firm) | SPEC |
| metrics | `<the firm's own names>` — a product event, the firm's own bus only (modules/metrics) | LIVE |
