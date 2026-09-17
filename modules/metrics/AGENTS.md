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
  bus is `{subject_id, ts, properties, via}` plus `caller` (the door) or `rule_exec_id` (the rule)
- **the door** — `POST /metrics`, `authorization_type = NONE` at the gateway; the function compares
  the bearer in constant time with every `METRICS_TOKEN_<CALLER>` under the firm's metrics env
  path (`/gradienterp/customers/<gerp>/metrics/env`). Tokens are cached `TOKEN_CACHE_S` seconds
  (30) and re-read on a miss: a new token admits at once, a rotated-out one stops within the
  cache's life. One event or a list; every event is checked before any is sent; 202 `{accepted,
  source}`, 401 `{error: unauthorized}` naming nothing, 400 `event <i>: <field>: …`
- **`manage_metrics`** (the gateway tool) — `publish_source {caller}` writes the token (256 bits
  from the OS, SecureString) and returns it once with the url; the same caller again rotates.
  `unpublish_source` deletes it. `list_sources` names callers (DescribeParameters, no values).
  `record` writes one event from the conversation. `count {event, grain, by?}`, `distinct {event,
  grain}`, `funnel {events}`, `retention {event, grain}` and `query {sql}` are the reads
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
  `metrics/year=/month=/day=/` (64 MiB or 60 s, whichever first, so a quiet firm's event is
  queryable about a minute later). The partition is Firehose's ingestion day; `ts` is the event's
  own instant and every read filters on `ts`
- **the catalog** — one Glue database `<prefix>_metrics_<gerp>` with one table `metrics`: columns
  `event, subject_id, ts, via` (string) and `properties map<string,string>`, partition keys
  `year, month, day` projected (no crawler). `event` is a column, not a partition: an injected
  partition would refuse any query that does not name the event, and a firm's record is small
  enough that Parquet column pruning is the saving that matters
- **the engine** — one Athena workgroup per gerp, tagged `payer = gerp`, results under
  `metrics/results/metrics/` (SSE-KMS, expired after a day by the cabinet's lifecycle rule in
  `prod/init_customer`). The tool role reaches this workgroup, this database and these two
  prefixes and nothing else, so the agent's SQL reads the firm's own record only
- **the reads** — `queries.py` builds each fixed read from one template and a two-entry dialect
  (how an ISO string becomes a local timestamp, how a timestamp prints); everything else is SQL
  both Athena and duckdb share. Bins are cut in the firm's zone (`modules/clock`): a 06:30 UTC
  event is the day before in Los Angeles. Windows: `today | this_week | this_month | last_month |
  this_year | last_7_days | last_30_days | last_90_days`, or `start` + `end` (ISO in the firm's
  zone, end exclusive); default this_month. A funnel counts a subject at step i only when every
  earlier step's first time precedes it. Retention folds cohorts by first period × offset
- **the usage row** — after every read, `{payer: gerp, sk: <ts>#<query_id>, query_id, ts, op,
  engine, bytes_scanned}` on `<prefix>-usage`, from Athena's `DataScannedInBytes` (locally the
  bytes of the files read). The firm's own record of what its analytics cost it, and the row
  shape a metered reader's query will write (#4)
- **local mode** — no `AWS_LAMBDA_FUNCTION_NAME` means duckdb: the engine lists the store prefix
  in the (moto) bucket, pulls the Parquet, runs the same SQL over `read_parquet(…,
  hive_partitioning = true)`. duckdb is a test dependency imported by name
  (`importlib.import_module`), so the deploy walk never bundles it. `tests/metrics/_helpers.py`
  seeds the bucket with the same partitioned Parquet Firehose writes

## the row, end to end

```
POST /metrics  {"event": "member.checked_in", "subject_id": "c_8812", "properties": {"location": "pier"}}
  → bus: source metrics, detail-type member.checked_in, detail {subject_id, ts, properties, via: door, caller: pos}
  → Firehose row: {"event": "member.checked_in", "subject_id": "c_8812", "ts": "2026-09-15T21:02:00.000Z", "via": "door", "properties": {"location": "pier"}}
  → s3://<cabinet>/metrics/year=2026/month=09/day=15/<file>.parquet
  → SELECT count(DISTINCT subject_id) FROM metrics WHERE event = 'member.checked_in' AND ts >= '…' AND ts < '…'
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
