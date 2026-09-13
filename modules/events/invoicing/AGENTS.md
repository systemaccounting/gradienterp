# invoicing events

The sell side: receivables, bids, standing offers. Emitter module: `modules/invoicing`.

## invoice.issued.v1 — SPEC (emit attaches to the live issue_invoice)
- a receivable now exists. **match**: financing offers — factoring as instrument-as-item on the
  existing PO rail (no new financial infra; the instrument is an item for sale).
- `invoice_id` is `<n>#<id>` on the direct path or a bare thread id cross-firm (agreement key,
  never prefixed) — consumers must not parse location from it; it rides `location`.

## quotation.submitted.v1 — SPEC
- this firm's bid into a counterparty's `purchasing/rfq.issued` window — the explicit repricing
  opportunity the auction exists to create. `valid_until_ms: 0` = the window's close.

## early_pay_discount.offered.v1 — SPEC
- dynamic discounting: the seller sells time-value of money, firms with cash surplus buy it.
  Bilateral cousin of the multilateral netting scan (a scheduled query over the published
  payables graph — no event) — when a cycle exists, netting strictly dominates (no cash moves).

## contract.offered.v1 — SPEC
- **the 4th textbook procurement object** (requisition → RFQ → PO → blanket agreement): volume
  pricing committed over a term. **match**: recurring purchase patterns visible in other firms'
  event history — the optimizer can see a firm buys 40lb of the same input monthly without being
  told. **graduation target** for pooled purchasing: a pool that fires every month wants a
  standing agreement, not a monthly auction.

## return.rma_created.v1 — SPEC
- an authorized return seeking second-life demand (another firm's purchase request for used
  goods). Distinct from `inventory/asset.listed` (own fixed assets) — this is sold goods coming
  back.
