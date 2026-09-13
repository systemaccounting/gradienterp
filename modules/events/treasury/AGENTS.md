# treasury events

Capital as cargo: instruments are items, sold on the PO rail, no broker to skim or fat-finger.
Emitter module: `modules/treasury`.

## distribution.paid.v1 — LIVE schema (emit attaches with the distribution handler)
- realized returns to holders → the capital-returns index (what public firms ACTUALLY paid, per
  sector) — the yield table that prices the next instrument.

## instrument.issued.v1 / instrument.transferred.v1 — PENDING
- visibility announcements after the durable writes (the DISTRIBUTION# rule instance / holder
  change). `terms` is a summary — the rule instance row is authoritative.

## instrument.offered.v1 — SPEC
- an instrument listed for sale. **match**: buy-side capital — firms/holders with idle cash
  (treasury sees cash positions in open books). The capital half of the flywheel: visible
  margins recruit capital; this is the event where capital shows up to be recruited.
