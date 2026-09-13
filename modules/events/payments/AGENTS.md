# payments events

The processor boundary. Emitter module: `modules/payments` (ingest lambdas, post-dedup).

## webhook.received.v1 — PENDING
- **emit site**: each `ingest_*` lambda after `record_event` dedup, regardless of transform
  outcome (a dead-lettered transform still announces the event landed).
- **why**: the `gross_amount`/`fee_amount` pair across tenants is the raw signal for fee-drag
  benchmarks ("you pay 2.9%, network median 2.6%") AND the engineer×firm match's trigger
  (`platform/module.published`) — twelve cafes burning 14% on Square fees is a PATTERN in these
  events before it's anyone's decision.
- `event_id` is the provider's id (idempotency key — never prefixed); `location` is resolved at
  the boundary from the provider's location signal (Square carries it; Stripe/PayPal default).
