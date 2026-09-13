# demo · labor — COO

**"schedule the FOH for the next 2 weeks"** — deliberately vague. The point is that almost every
answer is already in the business, so the agent measures instead of asking: roles say who works
the floor (baristas + shift lead FOH; the baker is not), past time entries carry the shift
pattern, the POS export ranks the crew by revenue per hour for the rush, and availability
(`reserve (op: availability)` on the `shift-<worker>` capacity items) bounds what it may book.

The take posts the whole fortnight as ONE `reserve` per person (the pattern as an RRULE) and reads
back one line per person — never a day grid.

Roster (cafe seed): Ava Reyes + Ben Osei (barista, FOH), Deo Park (shift_lead, FOH), Cora Vance
(baker, BOH). `seed.py` here resets the meter + time entries and gates on `--check` — the take
WRITES bookings, so it must run before every re-take. Record: `record.mjs --step 3`.
