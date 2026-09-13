# calendar

the tenant's **time domain**, narrowed to the two faces of time that aren't a quantity:

- **committed time → `events`** (standalone dated commitments)
- **fired time → EBS Scheduler** (the actionable subset: a notification or an automation at T)

## what moved out, and why

the third face — **offered time**, the free/busy of sellable capacity — is **not here**. it lives in [`modules/inventory`](../inventory/README.md).

the argument that moved it: *availability is time-dependent inventory*. a stocked good and a bookable room are the same meter — `quantity(item, time)` — differing only in whether the quantity carries forward. an unsold hour is gone at midnight and a sack of beans isn't, but "perishable" is a **read condition** (you can't draw from a bucket whose time has passed), not a different data model. keeping capacity in calendar meant two catalogs, so an invoice line had to know *which store* its item came from. now: one catalog, one meter, one append-only movement log — a stock item's movements are `@point` (a count over time), a capacity item's are `@period` (an interval subtracted from what its rule offers).

the utilization thesis survives the move intact — `booked ÷ available` is a service business's inventory turns, the unit-economics number the public feed most wants. it's just computed in inventory now, off the same log that yields stock turnover. so does *capacity is multiplicity, not a scalar* (a resource serving N at once is N quantity-1 rows), which turned out to be a **correctness** constraint there rather than a modelling preference: interval subtraction deletes an interval, it never decrements a count.

what the move fixed: the old model was `rrule − exdate`, and an RFC 5545 EXDATE negates exactly **one occurrence** — so a 3-night stay had to be shredded into 3 exdates, and a 2pm→11am stay couldn't be expressed at all. inventory replaces it with true interval subtraction: one `−1` over `[start, end)`, any range, no exact match.

## an event negates nothing

a conference next month, an owner's off-site, a dentist appointment — these consume no sellable offer. they aren't capacity being drawn down; they just *are*. which is exactly why they stayed behind when availability left: an **event** is an `expr` (an ISO date) + a `description` on a `subject`'s calendar, indexed by date, so "my calendar in August" is a range query.

"my calendar" is the union of a subject's events and whatever it has booked in inventory — two reads, because they are two different things.

## the record is the row; the schedule is the alarm

most of a calendar entry is a *record* — it holds no action. the **actionable** part — remind me, remind the customer, run a report — is the only thing that fires, and that's an EBS schedule. so an event (or a booking) wires 0..N schedules when it needs to act, and none when it just needs to be remembered.

this is the load-bearing distinction calendar keeps: the record layer for commitments, and the **per-tenant clock** for everything in the stack that fires at a time. see [`TODO.md`](TODO.md) for open work.
