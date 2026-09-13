# shipping — one custody module for shipping and receiving

## current features

- `apply_shipment_event` (router-invoked, not a gateway tool): a counterparty's `shipment.sent`
  opens our INBOUND custody row in `expected` against their PO thread, carrying their carrier /
  tracking / ETA. Idempotent on the thread — a re-sent notice moves the date, never a second row
- `manage_shipments` gateway tool: create | update | receive | pickup | ship | get | list over
  one DDB table (`gerp-shipping-<gerp_id>`, PK `shipment_id` = `<location-ordinal>#s-<hex>`,
  stream on; sparse `open-shipments-index` = the working view: in flight / on the dock /
  awaiting pickup)
- **direction is a field**: inbound flows expected → arrived → putaway | delivered | picked_up;
  outbound flows packed → shipped → delivered; exception/cancelled from any open state.
  `receive` stamps `signed_by` + `condition` (chain of custody); `pickup` closes a mailroom
  parcel; `ship` dispatches
- **destination is the polymorphic key**: stock (the dock — custody here, money + quantity legs
  on purchasing's `manage_po (op: receive)`, cross-referenced by `po_id`), person (the mailroom —
  `recipient` contact, notify → hold → picked_up, no books involvement), customer (fulfillment
  against `invoice_id`). inferred from recipient/po_id/invoice_id when not given
- **the freight leg**: `cost` posts Dr SHIPPING_EXPENSE / Cr CASH-or-AP through accounting's
  `post_journal_entry` at `ship` (outbound) or `receive` (inbound), at most once
  (`freight_entry` + deterministic entryId `ship-<shipment_id>`). no cost → no entry — a
  received parcel is not a transaction
- carrier fields: open `carrier` slug (fedex | ups | usps | dhl | ontrac | freight | courier |
  other), `tracking` stored verbatim (provider ids are never platform-prefixed), freeform
  `service`, `tracking_url` generated from per-carrier templates for known carriers
- field validation against the per-customer `shipping_fields` registry (canonical:
  `modules/schemas/data/shipping_fields.json`)
- exceptions ride `tasks` (`subject_key` = the shipment id, severity/category); POD + damage
  photos file on `storage` captioned with the id

## the boundary

subledger discipline (same as assets): this module owns the physical/custody record + the
freight leg ONLY. money stays on the PO ⇄ invoice rails; quantities stay in inventory —
`manage_po (op: receive)` counts goods in, this row records who signed for them.

## cross-firm coordination (events doctrine)

matching-table rows this module feeds (modules/events/TODO.md): `shipment.sent` (schema under
purchasing, **LIVE** — the `ship` op emits it; inbound, `apply_shipment_event` opens our custody row
with the sender's ETA and `manage_po (op: get)` joins it onto the PO), `receiving_advice.posted` (the
861-shaped fact: closes the sender's three-way match, starts the approved-payables clock),
`shared_delivery.pooled` / `backhaul.offered` (the route-pooling shape reads these rows' zones
and windows). between two gerps a shipment is ONE logical object with two custody records
converging on a shared key (tracking / thread — never ordinal-prefixed): the recipient's "i got
the package" closes their inbound row AND returns as the sender's POD. standing assumption on
the freight row: carriers become gerps — tendering becomes an addressed event, tracking becomes
their published movements, backhauls become signals.

## local mode

`LOCAL_SHIPMENTS` jsonl store + `LOCAL_SHIPPING_JOURNAL` capture for freight-entry assertions;
registry validation passes through. tests: `tests/shipping/local/`.
