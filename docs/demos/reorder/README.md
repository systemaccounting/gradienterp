# demo · reorder — purchasing/CPO, the cross-firm loop

**"we're down to 5 bags of espresso beans"** → the agent books the variance ($420 against COGS),
reads par off the item's reorder rules, names the vendor from the item's `vendors` link, and
offers the order. **"yeah, Blue Ridge Roasters"** → cross-firm create_po; the puppet accepts and
dispatches as Blue Ridge over the real rail. **"did they take it? when's it getting here?"** →
the PO answers with the vendor's own delivery: carrier, live tracking link, ETA.

What each beat slays: par is a VALUE rule (`required_count` + `order_required` attached to
`INVOICE_LINE#1#beans` — a control loop tripped by the count report, nothing polls); the delivery schedule
is the VENDOR's to state (`shipment.sent` → custody row with their ETA, joined by `manage_po get`); the
count report is the only human input the loop needs.

A possible closing beat for a future take: "the beans just showed up" → receipt books the payable
and moves the shelf (`docs/demos/TODO.md`).

Fixture: `seed.py` (shelf to par THROUGH `manage_stock` move, Blue Ridge rows off the shared agreements
store, open POs + custody rows cleared; rules/vendor-link/contact are REQUIRED, never seeded).
Record: `record.mjs --step 6`. The puppet needs no capture infra — `--send` rides the real
dispatcher (`tests/puppet/AGENTS.md`).
