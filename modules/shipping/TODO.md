# shipping — open work

`AGENTS.md` covers what's built. open, in pickup order:

- [ ] `receiving_advice.posted` schema + emit on `receive` — the 861-shaped fact (closes the
      sender's three-way match; the approved-payables trigger). addressed variant carries the
      two-gerp handoff: the recipient's confirmation IS the sender's POD.
- [ ] `shipment.sent` emit on `ship` — schema already shipped (purchasing/, PENDING); move or
      alias it under a shipping source when the emit lands.
- [ ] mailroom notify polish — arrival → the agent emails the `recipient` contact (playbook
      covers it today; consider a nudge if a parcel sits at arrived > N days — a task, not a
      lambda).
- [ ] `carrier_gerp_id` — the upgrade slot next to the slug for when a carrier runs a gerp
      (tender = addressed event, tracking = their published movements). field lands with the
      first carrier gerp, not before.
- [ ] parked: carrier API integrations (rate shopping, label purchase, tracking webhooks) —
      connect-time external creds, same posture as Stripe/Plaid; v1 records what happened, it
      doesn't buy labels.
- [ ] parked: landed-cost absorption into item `unit_cost` — with the other absorption
      questions (candle-maker labor/overhead).
- [ ] parked: multi-parcel / pallet hierarchies — path-in-the-key if someone needs it.
- [ ] parked: carrier scorecards — `sla.breached` promise clocks pointed at carriers; rides the
      future clock machinery.
