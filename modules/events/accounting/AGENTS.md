# accounting events

The durable plane's mirror: accounting owns the ledger, so its events are the money facts other
firms' matches read. Emitter module: `modules/accounting`.

## journal_entry.posted.v1 — LIVE

- **emitter**: `lambdas/post_journal_entry/main.py`, after the balanced entry lands (ledger, not
  pending). Emits the LOGICAL entry (line_items), never the pair-decomposed storage rows.
- **fires**: every ledger write, all sources (manual, webhook ingest, module posting paths).
- **fields**: `origin` is the renamed input `source` (envelope-`source` collision); `line_items`
  sums to zero across sides — enforced pre-emit in the lambda, inexpressible in JSON Schema.
  entry `dimensions` (incl. `location`) ride the durable row; surfacing them on the event is an
  additive v2 candidate when per-location public-feed slices build.
- **consumers**: the publication rule (openly_operated filter) → public stream; the future feed
  aggregator (LIVE GDP, sector aggregates) — `prod/openlyoperated_biz/TODO.md`.

## multilateral netting — a scheduled scan, not an event (schema removed 2026-07-17)

the netting match reads the payables/receivables graph straight from published ledger state —
a payable is a fold over the books, so `payable.outstanding` was the query's shadow and its
schema was removed. the scan: find cycles (A owes B owes C owes A) and propose a multilateral
net settling debt with **zero cash movement** — the match only the all-books substrate can
compute. open questions (design later): scan cadence; whether the netting proposal returns as
an addressed event or an ERP doc (credit-memo pair); partial-cycle netting.
`invoicing/early_pay_discount.offered` is the bilateral cousin.
