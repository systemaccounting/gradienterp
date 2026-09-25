# purchasing events

The buy side: intent → structured request → accepted exchange. Emitter module:
`modules/purchasing`. The agreement keys (`thread`, `terms_hash`) are peer-shared — both firms
must compute them identically — so they are NEVER location-prefixed (the id-generating exception
class); a location is per side and private — `buyer_location` / `seller_location` on the agreement
row, each written at that firm's own stamp, and `location` on the PO or invoice row it settles into.

## po.proposed.v1 / po.accepted.v1 / po.declined.v1 — LIVE RAIL
- **emitters**: the shared agreements services (`create_po` and `return_quote` → `request`,
  `accept_po` → `accept`, `decline_po` → `decline`) and `apply_inbound` when a rule on
  `PROPOSAL#po` accepts or counters with no turn. Addressed events (`detail.to` = gerp_profile_id)
  routed by the hub's edges — a delivery path, not an optimizer concern. An accepted row =
  the buyer's open PO + the seller's draft invoice, two projections of one exchange. A declined
  row is terminal on both stores; the items carry `sku` + `qty` when the buyer names them, so the
  seller's shelf can answer.

## quote.requested.v1 / quote.returned.v1 — PENDING
- the LOOSE intent pair: natural-language need + a counterparty's answer. The reactive hub flow
  ("find me a tech") in async event form. Structured needs graduate to `rfq.issued`.

## purchase_request.created.v1 — SPEC
- the textbook requisition, upstream of any PO. Matches other firms' on-hand inventory,
  published catalogs/prices, standing quotations — the demand half of the core commerce match.

## rfq.issued.v1 — SPEC
- **the quick auction**: a structured bid window. Firms matched on the lines get the explicit
  opportunity to reprice via `invoicing/quotation.submitted` before `bid_window_closes_at_ms`.
- **mechanics (design later)**: a step function per window (open → collect → close → award) once
  hot; the optimizer dequeue handles the cold path. Award returns as `po.proposed` on the live
  rail — the auction ends where the existing machinery begins.

## shipment.sent.v1 — LIVE RAIL
- dispatch against a PO; receipt booking downstream. The lane (from/to locations) is what
  freight matching (`transfer/shipment.needed` row) reads.
- **emitter**: the seller's `manage_shipments ship`. **inbound**: shipping's `apply_shipment_event`
  (router-dispatched) opens the buyer's inbound custody row in `expected`, carrying the sender's
  carrier / tracking / ETA — the delivery schedule is the counterparty's fact, so it lands as a row
  the moment they state it. Keyed on the PO `thread` (= the buyer's `po_id`), so the two firms'
  custody records land on one row without a second identifier; `manage_po get` joins it onto the PO.

## backorder.created.v1 — SPEC
- unfilled demand at sale/build time seeking other firms' surplus of the same `catalog_key`.

## work_order.requested.v1 — SPEC
- "need a tech at my laundromat today" as an event: `trade` + `location` match firms with open
  capacity/shifts. Precomputable via the spare-capacity index; the hub's reactive flow is the
  synchronous twin.

## lease_request.created.v1 — SPEC
- temporary use of an `asset_class` over a window. Matches `capacity.idle` / underutilized
  assets; distinct from `asset.listed` (permanent disposal).
