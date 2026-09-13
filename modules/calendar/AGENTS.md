# calendar module

The tenant's **time domain**: the record layer (the `events` DDB table) + the firing layer (EBS Scheduler).
Why the module is scoped this way — and where availability went — is in [`README.md`](README.md). Field names
on the DDB table validate against the `calendar_fields` registry. Not to be confused with `modules/events`
(the event-bus / public-ledger publication system) — the `events` table here is dated calendar commitments.

**Availability is not in this module.** The free/busy of sellable capacity lives in `modules/inventory`
(a capacity item, metered by an append-only movement log — `net = default − scheduled`). Calendar owns
*committed* time and *fired* time; inventory owns *offered* time. "How much is free when" → inventory;
"fire X at T" → here.

## current features

- **`events` table** (`pk = event_id`, GSI `calendar-index` = `subject` + `starts_at`) — standalone dated
  commitments: `{event_id, subject, expr, starts_at, description, duration, contact_id, invoice_id}`. An event
  consumes no capacity — it just *is* (a conference, an appointment). `subject` is a **contact_id** — the
  same person-reference labor's `worker_id` and inventory's `subject_contact` carry. It was specified as a
  `gerp_profile_id`, which would have made having a calendar depend on being publicly listed (a private
  employee could not be scheduled at all) and would have frozen "this person is public" into a GSI hash
  that cannot be rewritten. `description` is class `secret` until it gains a template — an event
  description is free-form, and "lunch w/ Dana" is the ordinary case.
- **`manage_event`** (agent tool) — CRUD over `events` (`put`/`get`/`query`/`delete`). `query` is "my calendar
  in [start,end]" — a GSI range query on `subject` + `starts_at`. v1 indexes ONE-OFFS (`starts_at` defaults to
  a one-off `expr`); recurring events need an explicit `starts_at`.
- **5 EBS schedule tools** (`calendar_{create,get,list,update,delete}_schedule`) — thin boto3 passthroughs over
  `scheduler:*`, scoped to the per-tenant schedule group (`gerp-calendar-<gerp_id>`). Plus **`agent_dispatcher`**
  (internal, not a gateway tool) — bridges an `agent_runtime` schedule fire to `InvokeAgentRuntime`.
- all agent tools registered on the shared AgentCore gateway (one unified `local.tools` map); `calendar_fields`
  seeded into the per-customer registry (bucket `event` enforced on the DDB table; `schedule` / `target` /
  `provenance` are the EBS shape-doc).
- outputs: `events_table`, `schedule_group_name`, `scheduler_target_role_arn`, `lambda_functions`, `lambda_arns`.

## the record layer vs the firing layer

The table is the **record** — a conference, an appointment — and holds no action. The **firing** layer (EBS
Scheduler) is the actionable subset: a schedule fires a notification or an automation at T. An event wires
0..N schedules only when it needs to *act*; a bare commitment wires none. `target_type` on
`manage_schedule` (`op: create`): `agent_runtime` (default; fires the dispatcher → the tenant's runtime, no
`target_arn` needed), `lambda`, `sns` (both require `target_arn`). Meeting-style metadata that isn't a field
here lands in `modules/notes` with an FK.

Calendar is the **per-tenant clock**: anything in the stack that fires at a time gets a schedule in this group
rather than its own `aws_cloudwatch_event_rule`. Migrating the stragglers is in `TODO.md`.

## the scheduler target role is an ALLOWLIST

EventBridge Scheduler fires under a role this module owns, and that role grants
`lambda:InvokeFunction` on **`agent_dispatcher` alone** — not `function:*`. A wildcard there lets
anything schedulable be pointed at any lambda in the account with any payload, which for a tool
whose argument is code means unreviewed code on a cadence.

**So a new `target_type=lambda` target has to add its own ARN to that role, in the same change.** A
schedule created without the grant is ACCEPTED, appears in listings, and fails at fire time with
AccessDenied — visible only as an `AWS/Scheduler` `TargetErrorCount`, with nothing in any log saying
which schedule or why.

`target_type=agent_runtime` goes through the dispatcher and needs nothing. Note the dispatcher is
not replaceable by a universal target: `arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime`
validates at `CreateSchedule` and then fails at fire time, with raw and base64 payloads alike.

## lambdas

- `manage_event` — CRUD over `events`; bundles `_crud.py` (registry-validated DDB CRUD, entity-parameterized)
- `calendar_{create,get,list,update,delete}_schedule` — EBS passthroughs; bundle `_helpers.py`
- `agent_dispatcher` — internal; a schedule fire → `InvokeAgentRuntime`

`_crud.py` carries one entity (`event`) since the availability half left. It keeps the entity-parameterized
shape — the registry-validation + id-creation plumbing is worth reusing if calendar grows a second record type.
Nothing here needs dateutil: v1 events are one-offs, so no RRULE is expanded (that moved to inventory with
availability). Local tests in `tests/calendar/local/`.

## status: live on gradienterp

The EBS layer + the `events` table + all agent tools are applied via `prod/per_customer` (`module "calendar"`,
`schema_table_name` wired, `depends_on = [module.agent]`). The `scheduled` + `availability` tables and the
`manage_availability` / `reserve (op: availability)` / `reserve` tools were removed when availability moved to inventory.
**`reserve (op: availability)` and `reserve` now belong to `modules/inventory`** on the shared gateway — don't
reintroduce either name here.
