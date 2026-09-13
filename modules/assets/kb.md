# equipment onboarding — the asset register

When the owner says they have equipment to manage ("I got a walk-in fridge, two espresso
machines and a delivery van"), walk the register with `manage_assets`, one unit at a time.

## per unit, gather then add

1. **name** — what they call it ("walk-in fridge"). The id is generated from it
   (`1#walk-in-fridge`); the location ordinal leads, so ask which location if they have more
   than one.
2. **vendor + model + serial** — off the nameplate if they have it ("True T-49"). Worth a
   nudge: this is how warranty lookups and service history key.
3. **class** — machinery_equipment | vehicles | computer_equipment | furniture_fixtures |
   leasehold_improvements | buildings. Pick it for them from what the unit is; don't quiz.
4. **warranty_expiry / in_service** — ISO dates if known; skip if not.
5. **cost** — three cases, ask naturally ("did you buy it recently, or has it been around?"):
   - bought now / recently on the books: `cost` + `paid_via: cash` (or `payable` if on terms) —
     this posts the acquisition entry automatically, never post one yourself
   - already owned before the platform: `cost` + `paid_via: opening` — the value belongs to
     the opening balance sheet, no new entry posts
   - cheap gear (typical policy: under $2,500): omit cost entirely — it was an expense, the
     row is purely operational
6. a unit they service but don't own (a client's machine): `owner` = that client's contact_id,
   and never a cost.

## when something breaks

File a task ABOUT the asset: `manage_tasks` (op: put) with `subject_key` = the asset id, `severity`
(watch | degraded | down), `category` (refrigeration, plumbing, pos, …), and the story in
`content`. Resolve it when fixed. `manage_tasks` (op: query) with `subject_key` is the unit's service
history — read it before recommending repair vs replace. If the unit is out of action, also
`manage_assets update` its status to `down` (and back to `in_service` after the fix).

## what does NOT go on the register

- things for sale or sellable capacity (hotel rooms, rental cars) — those are inventory items
- consumables and supplies — inventory
- retiring a unit: `manage_assets retire` flags it; if it carried a cost, tell the owner the
  books still need the disposal entry (that flow is coming — don't hand-post a gain/loss
  without being asked).
