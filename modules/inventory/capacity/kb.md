# booking capacity over time

A **capacity item** is a reusable quantity-1 unit whose meter is time, not count: a hotel room,
a salon chair, a service bay, a table, a rentable machine — and a person's working hours, which
behave identically. Its whole state is `net = default − scheduled`, folded from the movement log.

Most businesses have none of this. A shop that only sells things has stock, not capacity, and
nothing here applies. Reach for these tools when the owner talks about something being BOOKED,
RESERVED, or WORKED over a span rather than counted at a moment.

## the two reads and the one write

- `get_availability` — a capacity item's free windows over a range (`default − scheduled`), plus
  its `utilization` (booked ÷ offered — a room's occupancy, a bay's utilization). It rejects a
  stock item, whose meter is a count.
- `reserve` — books (`−1`) or cancels (`+1`) over one interval OR a repeating pattern.
- Everything is an append to the log: a booking and its cancel net out in the fold, so there is
  no state to mutate and no partial write to repair.

## book a whole pattern in ONE call

Pass `rule` (an RRULE), `duration` in seconds, `start` at the first occurrence and `end` at the
last day of the range. A fortnight of daily bookings is one call and one row rather than fourteen
of each. You usually already hold the pattern — sending it as separate intervals throws that away
and is how a booking half-lands.

The guard runs per occurrence, so a clash refuses the WHOLE pattern and names the day that
conflicted. Fix that day and re-send; nothing partial was written. `op=cancel` with a `source`
and no interval releases everything standing under that source in one call, which is what makes
a booked plan fully reversible.

Booking is what makes a plan real rather than a message: the next reader (or the next you) sees
the time is gone. An unreserved plan double-books.

## when the capacity is a person's shifts

A worker's availability is a capacity item and their agreed windows are the `default`. **Read it,
and never book outside it** — history says what someone did; availability says what they have
agreed to, and a pattern is not consent. No capacity item means you don't know: ask, don't assume.

**Never put the person in the item's id or name.** Create it with a neutral id and set
`subject_contact` to their contact id — `create_item(name="shift", subject_contact="ava", ...)`.
The id and name are copied into every movement row and published by the inventory read, and a key
cannot be reclassified later; `subject_contact` is a reference, so whether that worker is named in
public data follows their contact's profile link and can change afterwards. Find a person's
capacity by querying on `subject_contact`, not by guessing at a name-shaped id.

The rest of a staffing pattern is already in the business — roles say who can work which floor,
past time entries say what shifts the business actually runs, the sales log says when it's busy.
Look before asking.

How to ASSIGN people across those shifts is a business decision, not a mechanic:
`get_standard("gerp/scheduling.md")` carries the platform's recommended practice. The firm's own
election overrides it — follow their standing
instruction when they have one, and `instruct` the policy when they state it.

## reporting a booked plan

**Book it, then report it.** Not "shall I book this?" — the reservation is what makes the plan a
plan, and it's fully reversible. Asking first costs the owner a round trip to say yes to
something they already asked for.

For a multi-party plan (a staff schedule, a week of room bookings), the output is **ONE LINE PER
SUBJECT — never a day-by-day list.** Two weeks as fourteen dated rows repeats the same few names
and makes the reader reassemble it:

```
<name> · Sat–Wed · 7am–3pm
<name> · Mon–Fri · 6am–2pm
<name> · Thu–Sun · 10am–6pm (paired — new hire)

exceptions: <name> off <day>
```

Then exceptions, then what you assumed. A pattern that genuinely changes partway through is an
exception line, not a reason to switch to a grid. The recurring line already says which days —
don't expand it back into the dates it covers.

Present it and invite CORRECTIONS — one opening, not five questions. An owner correcting one line
of a real plan is faster than an owner answering an intake form. Ask only for what genuinely
isn't there.
