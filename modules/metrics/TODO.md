# metrics — open work

- **the readers' workgroup** (#4, #21) — a second Athena workgroup tagged `payer = api` for keyed
  public queries over the tables a gerp marks published; the door writes the usage row into the
  operator's `gerp-api-usage`, `bill_customer` credits the gerp. The usage table and the
  `payer`-tagged workgroup here are what it lands on
- **partition pruning** — a read filters on `ts` only, so Athena lists every day's partition.
  The store's partition is Firehose's ingestion day, which is never before the event's own
  instant, so the fixed reads can bound `year/month/day` by `[window.start, now]` and prune. Add
  it when a firm's record is large enough to notice
- **the other callsites** — `record_metric` is resolvable where the callsite lambda holds the
  firm's bus: invoicing (invoice_status, invoice_tag, item_transition), inventory (stock_sold,
  stock_adjusted, reorder) and labor (close_shift, pay_run). treasury's `distribution`,
  agreements' `proposal_received` and automation's `automation` take it when their lambdas get
  `INTERNAL_BUS_NAME` and the PutEvents grant
- **`metric.published`** (SPEC in modules/events/TODO.md) — an openly operated firm names a
  product count as a `detail.counters` entry on the shared bus, so the public dashboard carries
  usage beside the P&L
- **a second store** — a rule target beside Firehose (OpenSearch natively, Timestream or Neptune
  through a lambda) for a read SQL does not do; `op: query` grows an `engine`, the usage row an
  `engine` column, #4's price a line per engine
- **the door's PutEvents** — one call per event today; a batch of a hundred is a hundred puts.
  Ten per call when a source sends batches that size
- **`tests/testdata/table-schemas.json`** — `metrics-usage` joins the snapshot on the first sweep
  after the apply; until then `tests/metrics/_helpers.py` creates the table itself
