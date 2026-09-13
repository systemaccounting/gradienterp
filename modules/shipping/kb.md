# shipping and receiving — the custody walk

One tool (`manage_shipments`) covers the office mailroom and the receiving dock: every
shipment/parcel is one custody record, `direction` inbound or outbound. Money never moves here
except the freight cost; goods counts go through `manage_po (op: receive)`.

## receiving against a PO (the dock)

1. delivery lands: `manage_shipments create` `direction=inbound`, `po_id`, carrier/tracking if
   known, `description` ("3 pallets from Sysco"). If it was expected in advance, create it at
   order time and it waits at `expected`.
2. on arrival: `manage_shipments receive` with `signed_by` and `condition`. If the firm paid
   the freight, pass `cost` (+ `paid_via`) — the SHIPPING_EXPENSE entry posts automatically,
   never post one yourself.
3. count the goods in with `manage_po (op: receive)` against the PO — that's the money + stock legs;
   the shipment row is who signed for what condition.
4. damage or shortage: file a task with `subject_key` = the shipment id (severity, category),
   photograph the damage → `storage`, captioned with the shipment id. Tell the owner what the
   claim options are; don't adjust the books until the counts are settled via `manage_po (op: receive)`.

## the mailroom (a parcel for a person)

1. parcel arrives for someone: `create` `direction=inbound`, `recipient` = their contact_id,
   `description` ("box for Dana"), then `receive` (who signed at the desk).
2. notify the recipient — `email` them that it's waiting.
3. when they collect: `pickup` (optionally `signed_by`). The status history is the chain of
   custody. No books involvement at all — a parcel is not a transaction.
4. sitting at `arrived` for days? file a reminder task on the shipment.

## shipping an order (fulfillment)

1. order packed: `create` `direction=outbound`, `invoice_id`, `contents` lines, `description`.
2. handed to the carrier: `ship` with `carrier`, `tracking`, `service`, and `cost` (+
   `paid_via`) — the freight entry posts here, and a tracking link is generated for known
   carriers (fedex/ups/usps/dhl).
3. delivered (customer confirms or tracking shows it): `update` status → `delivered`.
4. if the counterparty is another business on the platform, their confirmation ("i got the
   package") is the proof of delivery — relay it onto the shipment when it arrives.

## what this is NOT for

- goods counts and valuation — `manage_po (op: receive)` / `manage_stock (op: move)` own those
- buying labels or rate shopping — record what happened; carrier accounts are the owner's
- the delivery vehicle itself — that's on the asset register
