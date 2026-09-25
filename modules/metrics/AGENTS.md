# metrics module

The firm's product record: each step a subject takes with the firm's product, recorded in the
firm's own words and read back as counts, distinct subjects, funnels, retention and SQL, on the
firm's own calendar. The agent reads it, so a product question in chat is answered from the record
and joined to the books. Why in `README.md`.

## current features

- **one write, three producers** — `metrics.record(raw, via, **extra)` checks the shape and puts
  the event on the firm's own bus (source `metrics`, detail-type the event). The door
  (`lambdas/record`, `POST /metrics`, a bearer per caller), the rule (`metric_rules.record_metric`,
  a row on a callsite key) and the agent (`manage_metrics op=record`) all call it
- **the event** — `{event, subject_id, at?, properties?}`. `event` is `<resource>.<action_past>`,
  checked against `^[a-z0-9_]+(\.[a-z0-9_]+)+$` and nothing else; `subject_id` a non-empty string;
  `at` ISO 8601 or epoch milliseconds, default now, stored as `ts` (UTC, milliseconds, `Z`, so
  string order is time order); `properties` flat scalars, stored as strings. The detail on the
  bus is `{customer_id, zone, subject_id, ts, properties, via}` plus `caller` (the door) or
  `rule_exec_id` (the rule): the firm's id and its clock's zone ride on every record, so the
  platform cuts a period on the firm's calendar without asking it. Contract:
  modules/events/metrics/metrics.record.v1.json
- **the door** — `POST /metrics`, `authorization_type = NONE` at the gateway; the function compares
  the bearer in constant time with every `METRICS_TOKEN_<CALLER>` under the firm's metrics env
  path (`/gradienterp/customers/<gerp>/metrics/env`). Tokens are cached `TOKEN_CACHE_S` seconds
  (30) and re-read on a miss: a new token admits at once, a rotated-out one stops within the
  cache's life. One event or a list; every event is checked before any is sent; 202 `{accepted,
  source}`, 401 `{error: unauthorized}` naming nothing, 400 `event <i>: <field>: …`
- **`manage_metrics`** (the gateway tool) — `publish_source {caller}` writes the token (256 bits
  from the OS, SecureString) and returns it once with the url; the same caller again rotates.
  `unpublish_source` deletes it. `list_sources` names callers (DescribeParameters, no values).
  `record` writes one event from the conversation. `query {name, params, window|start+end}` is
  the read: a `metric_queries` registry row run by name (below). `pin {name, pinned}` sets or
  clears the row's `pinned` flag, which the prompt's dynamic tail reads every turn (the agent
  module), with no cap on how many. There is no inline SQL
- **`record_metric`** (`metric_rules.py`) — params `{event, subject: <ctx field>, properties:
  {name: <ctx field>}}`. Reads the subject and the properties off the callsite's ctx, sends with
  `via: rule` and the exec id, returns `[]` so a callsite that folds returns into postings is
  untouched. A missing subject field records nothing and prints one incident line. Resolvable on
  the callsites whose lambdas hold the firm's bus: `invoice_status`, `invoice_tag`,
  `item_transition` (invoicing), `stock_sold`, `stock_adjusted`, `reorder` (inventory),
  `close_shift`, `pay_run` (labor); the ctx at `close_shift` carries `worker_id` and `entry_id`,
  at `pay_run` `worker_id` and `period`, for it
- **the store** — a rule on the firm's bus (`source: metrics`) targets a Firehose stream through
  an input transformer that flattens the envelope to the row; Firehose converts to Parquet with
  the Glue table's schema and writes under the cabinet at
  `metrics/dt=YYYY-MM-DD/` (64 MiB or 60 s, whichever first, so a quiet firm's event is
  queryable about a minute later). The partition is Firehose's ingestion day; `ts` is the event's
  own instant and every read filters on `ts`
- **the catalog** — one Glue database `<prefix>_metrics_<gerp>` with one table `metrics`: columns
  `event, subject_id, ts, via` (string) and `properties map<string,string>`, one partition `dt`
  projected as a date from the store's first day to today (no crawler). One key, because Athena
  lists every projected partition a query does not exclude, and the reads filter on `ts`: three
  integer keys enumerated ~28,000 partitions and took 35 to 48 s to scan nothing. `event` is a column, not a partition: an injected
  partition would refuse any query that does not name the event, and a firm's record is small
  enough that Parquet column pruning is the saving that matters
- **the engine** — one Athena workgroup per gerp, tagged `payer = gerp`, results under
  `metrics/results/metrics/` (SSE-KMS, expired after a day by the cabinet's lifecycle rule in
  `prod/init_customer`). The tool role reaches this workgroup, this database and these two
  prefixes and nothing else, so the agent's SQL reads the firm's own record only
- **the reads are registry rows** — `metric_queries` in the gerp's registry table
  (`modules/schemas`), bucket the engine, name the query: a description, the SQL with `?` markers,
  the ordered typed params. `rows.py` reads a name from the table, or from the canonical file
  (`metric_queries.json`, the operator's canonical bucket; `modules/schemas/data` locally) and
  copies it into the table as a canonical row on that first use, and refreshes a canonical row
  from the file on a later call when the file changed; a name in neither is a 404 that says how
  to list, search and save. The canonical set: `active`, `count`, `count_by`, `sum` (the
  `amount` property added per period), `cumulative` (a stock: the running total of an in-event
  minus an out-event over the whole history to the window's end, `at_start` and `at_end` per
  period, measure count or sum), `funnel_3`, `retention`. A gerp's own row is one the agent saved with `write_schema op=extend`;
  the registry is listed and readable but never seeded (`NOT_SEEDED` in the schemas module)
- **the catalogue** — `metric_definitions` in the gerp's registry table (`modules/schemas`,
  seeded from `metric_definitions.json`, promoted like the vocabulary), bucket the domain, name
  the metric: a definition in dbt MetricFlow's words. Every event is a semantic model (entity
  `subject_id`, dimensions the properties, aggregation time `ts`); the measures are `count`,
  `count_distinct` and `sum` of `amount`; the types `simple` (the rows `count`, `active`, `sum`),
  `cumulative` (the row), `ratio` (an entry: `{type, unit, grain, description, numerator: [leg…],
  denominator: [leg…]}`, a leg `{event, measure, sign?}` or `{cumulative: {in_event, out_event,
  measure}, at: start | end}`, legs summed with their signs, so margin is revenue minus expense
  over revenue), `conversion` (the rows `funnel_3`, `retention`); `derived` is not a row. Six
  entries per bucket: conversion, churn, engagement, unit_revenue, unit_cost, margin for saas,
  commerce and membership. `definitions.py` reads a name (`<bucket>.<name>`, `membership.churn`;
  a bare name when one bucket holds it), copies it in on first use, and runs it as its legs' rows
  joined per period: `manage_metrics op=query name=membership.churn window=last_month` takes
  `grain` alone and answers `{period, numerator, denominator, ratio}` rows, a period whose
  denominator is zero with no ratio, a leg over an unrecorded event contributing nothing.
  `op=pin` takes the same name; the prompt's tail lists a pinned definition as `<bucket>.<name>`.
  The lint (`scripts/lint_schemas.py`) refuses a leg naming an event outside the vocabulary
- **the money measure** — `post_journal_entry` (modules/accounting) records one metrics event
  per journal line, `<type>.posted` for the five account types (`revenue.posted` …, the `books`
  bucket of the vocabulary), the entry the subject, `amount` signed by the type's normal balance,
  `via: ledger`; the platform's counter forms the `sum` key from it and the api's card says `USD`
- **the binding** — Athena substitutes `ExecutionParameters` into the SQL as text before planning,
  so the declared type is the boundary: a `string` becomes a single-quoted literal with quotes
  doubled, a `number` only if it parses, a `timestamp` in the store's format
  (`YYYY-MM-DDTHH:MM:SS.mmmZ`) so a comparison against `ts` holds at the window's edges. Four
  names are reserved and filled when the call omits them: `start` and `end` from a named window
  (`today | this_week | this_month | last_month | this_year | last_7_days | last_30_days |
  last_90_days`, or explicit dates in the firm's zone, end exclusive) through `modules/clock`,
  `zone` the firm's zone, `grain` `day`. A parameter the row does not declare is refused by name
- **the usage row** — after every read, `{payer: gerp, sk: <ts>#<query_id>, query_id, ts, name,
  engine, bytes_scanned}` on `<prefix>-usage`, from Athena's `DataScannedInBytes` (locally the
  bytes of the files read). The firm's own record of what its analytics cost it, and the row
  shape a metered reader's query will write (#4)
- **local mode** — no `AWS_LAMBDA_FUNCTION_NAME` means duckdb: the engine lists the store prefix
  in the (moto) bucket, pulls the Parquet, substitutes the literals into the `?` markers the way
  Athena does, and runs the SQL over `read_parquet(…, hive_partitioning = true)` with macros for
  the Trino functions the canonical rows use (`from_iso8601_timestamp`, `date_format`,
  `element_at`, `at_timezone`); a gerp's own row outside that set may not run locally. Athena
  parses a query before it substitutes the parameters, so a marker is taken where an expression
  is (a function argument, a comparison) and not after `AT TIME ZONE`, which takes a literal:
  the canonical rows shift the zone with `at_timezone(ts, ?)` (found on the first integ run). duckdb is a test
  dependency imported by name (`importlib.import_module`), so the deploy walk never bundles it. `tests/metrics/_helpers.py`
  seeds the bucket with the same partitioned Parquet Firehose writes
- **reports on the portal** — prompt-tier, no tool: after a data answer the agent offers a page under `pages/reports/<slug>.html` (modules/storage `manage_storage op=put`, the link back), the standing preference `data-questions-as-reports` by `remember`, and a periodic one as an automation the calendar fires, its runs `pages/reports/<slug>/<YYYY-MM-DDTHH-MM>.html` with `latest.html` rewritten; the page and script shapes are in kb.md
- **the public metric key** — `public_key(definition, period)`, `parse_public_key(key)` and `slug(definition)` in `metric_key.py` (import-free, bundled into the platform's counter and api archives), the one place the platform's counters key is built or read: `<event>#<kind>[#<property>=<value>]#<grain>#<period>` under the firm's partition on `gerp-counters`, `kind` the measure's aggregation, `count | count_distinct | sum | count_by`, `grain` one of `day | week | month`, `#`, `=` and `%` in a value percent-encoded. Two firms' identical events share a key; the vocabulary's bucket is the metric's class, read off `metric_events`, never stored. The counter and the api import it; nothing else splits a key
- **the platform copy** — `metrics.record` calls `events.publish` beside `emit`: when the firm is openly operated the event goes to the hub with the publication envelope and the hub forwards it to the operator, whose `metrics` rule counts it under the firm's partition when the gerp's row reads `published`; a private firm's record stays on its own bus (`published: None` in the return). Every function that records carries `OP_EVENT_BUS_ARN`, `SETTINGS_TABLE` and the grants for both: the door and `manage_metrics` here, and the callsite functions that run `record_metric` in labor, inventory and invoicing. Nothing is published by name: an openly operated firm's every product event counts, at day, week and month, `count` and `active`, from the event's name, `ts` and `zone`. `subject_id` crosses as recorded and is never served: the api answers a set's size, and a channel drops it by its contract class `subject`

## the row, end to end

```
POST /metrics  {"event": "member.checked_in", "subject_id": "c_8812", "properties": {"location": "pier"}}
  → bus: source metrics, detail-type member.checked_in, detail {subject_id, ts, properties, via: door, caller: pos}
  → Firehose row: {"event": "member.checked_in", "subject_id": "c_8812", "ts": "2026-09-15T21:02:00.000Z", "via": "door", "properties": {"location": "pier"}}
  → s3://<cabinet>/metrics/dt=2026-09-15/<file>.parquet
  → manage_metrics {op: query, name: active, params: {event: member.checked_in, grain: week}, window: this_month}
  → the `active` row's SQL with its `?` markers filled: count(DISTINCT subject_id) per week, cut in the firm's zone
```

## gotchas

- `at` is the API field and `ts` the column: `at` is a keyword in duckdb and awkward in Trino, and
  a column the agent has to quote in every query is a papercut forever
- Firehose format conversion drops a field the Glue table does not name and needs `buffering_size
  >= 64` and `compression_format = UNCOMPRESSED`
- the input transformer's placeholders are unquoted (`<event>`): EventBridge quotes a string
  itself and inserts `properties` as an object
- `ssm:DescribeParameters` takes no resource, so the tool role holds it on `*`; the token values
  are reachable only by the door's role, through `GetParametersByPath` on the one path
- `tests/testdata/table-schemas.json` learns `metrics-usage` on the first sweep after the apply;
  until then the test helper creates the table itself
