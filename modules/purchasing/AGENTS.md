# purchasing module

depends on inventory, accounting, contacts, and invoicing (its sell-side mirror). accounting does not depend on purchasing.

## current features

- `create_po` — agent tool: record a PO against a known vendor (`{vendor, lines:[{description, account, accountType, amount}], memo?, location?, job?}`); status `open`. The PO row carries `location` (default "1"), never the `po_id`, which both firms compute; `manage_po` receive and pay copy it into `dimensions.location`. On the agreements path it lands as `buyer_location` at the buyer's stamp (`create_po` or `accept_po` as buyer) and the buyer-side settle opens the PO at it.
- `manage_po` op `receive` — `{po_id}` → post DR <each line's account> / CR ACCOUNTS_PAYABLE; `open → received`.
- `manage_po` op `pay` — `{po_id}` → post DR ACCOUNTS_PAYABLE / CR CASH for the total; `received → paid`.
- `manage_po` op `get` — list/read POs by `po_id` / `status` / `vendor`.
- `request_quote` — agent tool: emit `quote.requested` addressed to the vendor (`detail.to = vendor gerp_id`); single-round price discovery.
- the `create-po` gateway target — the agent's tool; the schema lives here (`lambdas/create_po/schema.json`), the lambda is the shared `agreements/request` service. The negotiation (stamps, inbound events, settle dispatch) lives on `modules/agreements`.
- `create_po` lambda — the direct (non-agentic) recorder: seeds and the owner record an `approved: true` PO synchronously; both slots stamp on the shared agreements store, the PO opens inline, the row marks settled.
- `settle_agreement` lambda — the buy-side settle EFFECT: invoked by `agreements/settle` with `{"agreement": row}` when a po-kind row agrees and this firm is its buyer → an `open` orders row. Domain work only; the dispatcher owns the agreement row.
- storage: `-orders` table (hash `po_id`). Agreement rows of every kind live on the shared `gerp-agreements-<gerp>` table (`modules/agreements`) — the module's own `-agreements` table was DROPPED, along with the terraform fallback that chose between the two: one fact with two storage paths means a reader has to know which, and `shared_agreements_table_name` is now required rather than defaulted.
- outputs: `orders_table_name`.

## what it does

the buy side — procure-to-pay. two paths — the direct flow and the agentic cross-firm commit — produce the *same* PO object, so receipt/payment serve both. (design rationale: `README.md`.)

## the direct flow

the everyday case: the owner records a purchase against a known vendor, no negotiation. `create_po` writes the PO + line items (each line debits an asset like INVENTORY or an expense, at the agreed amount); `manage_po` (op: receive) posts DR <lines> / CR ACCOUNTS_PAYABLE (`open → received`); `manage_po` (op: pay) posts DR ACCOUNTS_PAYABLE / CR CASH (`received → paid`); `manage_po` (op: get) reads them. Idempotent via the status guard + a deterministic entryId/timestamp on each post.

## the agentic cross-firm commit

the same PO object, reached by negotiation across two firms as **addressed** events (`detail.to = <vendor>`), each put on the recipient's hub bus (`modules/events` `emit_to`), never broadcast. an addressed event is never published; for openly-operated firms the journal entries the settled order posts reach api.openlyoperated.biz.

1. `inventory` flags low stock (or the owner asks to reorder) → its stream pokes the purchasing agent — or, with an `auto_order` row on `REORDER#<item>`, `manage_stock` proposes the PO itself through the shared request service (vendor, unit price and the vendor's `sku` from the row; `on_order` stamped on the item, released by the receipt) and nobody is woken.
2. `request_quote` emits `quote.requested` addressed to the vendor → the hub's spoke edge → the vendor's bus → its inbox. Optional single-round price discovery; it does not invoke the vendor's agent.
3. the agent's `create-po` tool (the shared `agreements/request` service) stamps the buyer slot on the shared `(thread, terms_hash)` store, keyed by the terms fingerprint; a line may carry the vendor's `sku` and a `qty`, which hash with it. On the vendor's side the sequence in `modules/agreements/AGENTS.md` § a proposal lands runs: its `PROPOSAL#po` rule answers from the shelf with no turn, or its agent is poked and its owner's word accepts (`accept-po`), counters (`return-quote`, the same service from the sell side) or declines (`decline-po`). Both stamps ⇒ agreed; re-sending is a gated no-op.
4. an inbound `po.accepted` stamps this firm's seller slot via the shared `agreements/apply_inbound` (router-invoked) — a row's two stamps are always (one local call) + (one inbound event), no agent.
5. `agreements/settle` (the shared stream consumer) dispatches the agreed row to this module's `settle_agreement`, which opens the orders row — where `manage_po` (op: receive) / `manage_po` (op: pay) settle it exactly as the direct path.

Steps 3–5 are deterministic settlement off the shared agreements stream (the `close_handler` pattern) — no agent. Every state change is a spec event addressed to the counterparty.

## the two cases

- **vendor runs gerp** — the addressed event lands in the vendor's inbox; their own-stream-poked agent (or invoicing's deterministic apply) advances it. Journals post both sides; the vendor's mirror is `invoicing` (DR ACCOUNTS_RECEIVABLE / CR SALES_REVENUE).
- **vendor is just a contact** — no cross-account hop; the buyer's agent records the order on the vendor's behalf, and the buyer pulls status. Same object, single tenant.

## storage

dynamodb. `-orders` (hash `po_id`): vendor ref (contact_id local, business_id cross-firm), line items, status, amounts. Agreement rows are on the shared `gerp-agreements-<gerp>` table (modules/agreements) — this module has no agreements table.
