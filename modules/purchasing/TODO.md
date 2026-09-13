# purchasing — implementation plan

Spec in [`AGENTS.md`](AGENTS.md). Buy-side (procure-to-pay); the sell-side mirror is `modules/invoicing`.

**Built and live** (see AGENTS *built — the direct flow*): the non-agentic core — `create_po` → `manage_po` (op: receive) → `manage_po` (op: pay) (+ `manage_po` (op: get)), the orders table, IAM, gateway registration, local tests. The owner records purchases against a known vendor; receipt/payment post the AP lifecycle.

This plan is the **agentic cross-firm negotiation layer** that produces the *same* PO object, so the built receipt/payment serve both paths. The delivery substrate is live — the operator dispatcher + `modules/inbox` route addressed events to the recipient and poke its agent (see [`modules/inbox/AGENTS.md`]). The quote phase reuses `modules/treasury`'s offers shape (append-only request/approve); receipt + payment are stream-fired settlement (the `close_handler` pattern).

**`request_quote` is built and live** (loopback-proven): records a `quote_requested` thread + emits `quote.requested` addressed (`detail.to = vendor's gerp_id`) → dispatcher → vendor's inbox → poke. The optional, single-round price-discovery precursor (not a negotiation).

**The full cross-firm commit is built and loopback-proven end-to-end** (one gerp books both halves): `propose_po` (buy) + `accept_po` (now in `modules/invoicing`, sell) → the crossing addressed events → **both** an open PO and a draft invoice. The agreement is the shared `modules/agreements/agreements.py` lib (`request`/`approve`/`terms_fingerprint` — append-only `(thread, terms_hash)`, both stamps ⇒ agreed, counter = new row, re-request = gated no-op). `apply_po_event` (router-invoked, `po.accepted` only) stamps the seller slot from the inbound event; `settle_agreement` writes the `open` PO; the existing `manage_po` (op: receive)/`manage_po` (op: pay) settle it. The single inbox **router** dispatches inbound by `detail_type` (no per-module ESMs).

## deliverables

- [ ] `return_quote` tool — the vendor replies **`quote.returned`** (price) to a `quote.requested`. Optional; only when the buyer doesn't already know the price.
- [ ] Cedar policy — `spend_threshold` on `propose_po`; per-customer threshold from SSM tenant metadata.
- [ ] `get_orders` tool — read a thread: standing proposals (the agreements rows) + order status.
- [ ] event schemas — `quote.requested` / `po.proposed` / `po.accepted` under `modules/events/purchasing/`.
- [ ] second-gerp e2e — cafe proposes to the roaster; on agreement both post mirror journals (buyer DR INVENTORY / CR AP; seller via `invoicing` DR AR / CR SALES_REVENUE).
- [ ] inventory stock-count sync on receipt — `manage_po` (op: receive) posts the AP journal but doesn't bump the per-item count; ride `inventory.manage_stock (op: move)` ADJUSTED (no re-post). applies to both paths.

- [ ] **an inbound `invoice.issued` becomes a bill.** The buy-side object exists — `create_po`
      needs only `vendor` + `lines`, which an invoice carries, and `manage_po receive` books
      `DR <lines> / CR ACCOUNTS_PAYABLE` — so the missing piece is the `ROUTES` entry that builds
      the PO from the inbound invoice, no agent turn. The platform's own hosting invoice is the
      first thing every gerp would receive (`modules/events/TODO.md` § the poke must become
      opt-in). A firm that is not a gerp gets the invoice by email; this is the fast path for
      the ones that are.
