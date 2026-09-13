# api_openlyoperated — open work

- [ ] **keys** — the Info & Billing screen that mints a key into `gerp-api-keys`, mirrored into
  the keyed usage plan; a daily `GetUsage` read onto `gerp-api-usage` for the monthly invoice;
  a Lambda authorizer on the Events API that checks the same table, so one key works on both
  doors and the public subscribe key retires. Until then the WebSocket door is the public key,
  which AppSync caps at a year: `expires` in `events.tf` moves forward before 2027-09-01.
- [ ] **`GET /v1/screener`** — the join the page fans out client-side today (every published
  gerp's `financials` side by side), computed per request; the first resource added after the
  starter set, and the extensibility claim proven on the page.
- [ ] **the emit-side `openly_operated` flag and the `publication` rule's filter** — nothing
  reads the flag now (the publisher gates on the row, the gerp's reads on its settings row);
  emitters stop stamping it and the contracts drop it from `required`. The firehose target stays
  until the archive's future is decided.
- [ ] **the archive** — a per-gerp event store (columnar for SQL, series, the graph), metered
  through keys, a query page for humans: the FRED-class data product. `raw/` and `curated/`
  prefixes, manifests, `/v1/jobs` — room named, nothing built until an issue asks.
- [ ] **alarms** — the publisher's errors and the relay's held-lambda count; a stream that
  stops is silent today.
