# clock module

A library, not a service — no lambdas, no infra, no tables. One file, `clock.py`, imported by any
lambda that has to turn a calendar claim into a database query. `scripts/deploy.py` walks the import
graph across `modules/*/`, so `import clock` bundles it with no build wiring (same as
`modules/agreements`).

## current features

- `zone_name()` / `zone()` — the gerp's IANA zone, resolved once per cold start.
- `period_bounds(kind, ms)` — `(start_ms, end_ms)` for the LOCAL day/week/month/quarter/year
  containing an instant. Half-open, weeks start Monday.
- `month_keys(start_ms, end_ms)` / `month_key(ms)` — `YYYY-MM` partition keys on the local calendar.
- `to_utc_ms(value)` — ms / offset-bearing ISO / naive ISO → instant. A naive value is placed in the
  gerp's zone, never assumed UTC.
- `local(ms)` — an instant as an aware datetime in the gerp's zone, for formatting.
- `is_valid_zone(name)` — for validating at write time.

## instants are not civil concepts

The whole module is this distinction.

- An **instant** is a moment: when a payment cleared, when a shift started. UTC ms, always, stored
  and on the wire. Nothing here changes that.
- A **civil concept** is a calendar claim: a month, a business day, a pay week, net-30. It is only
  meaningful against a calendar, and the calendar that matters is the one the business lives on.

`period_bounds` takes the second and returns the first. A Pacific business's July is
`[2026-07-01T07:00Z, 2026-08-01T07:00Z)` — still UTC instants, so every caller queries exactly as it
did before. What moved is where the boundary falls.

Get this wrong and a 6:30pm sale on July 31 books to August. The trial balance still balances, so
nothing catches it, and rendering the timestamp differently cannot fix it — the row was excluded
from the aggregate before anything was rendered.

## where the zone comes from

One source of truth, one precedence, used identically here and in the agent container so the two can
never disagree about what month it is:

1. `GERP#timezone` on the settings config table — owner-editable on the gerp screen, **validated on
   write** (`ZoneInfo(bad)` raises at call time, so an unvalidated typo would surface at period close
   instead of at the moment someone typed it).
2. `GERP_TIMEZONE` env var — the provision-time default from `per_customer`'s `timezone`.
3. `UTC`.

Resolved once per cold start; a settings-table read failure keeps the env value rather than raising.
**Unset means UTC means the behaviour that predates this module**, so a gerp that never configures a
zone is unaffected.

## what this module does NOT touch

- **The ledger's partitioning.** `post_journal_entry` derives `pk` from the UTC month and the readers
  enumerate UTC partitions then filter on the `sk` timestamp. That is internally consistent and
  correct; a reader enumerating *local* months would query partitions that were never written. The
  fix for a wrong period was never in storage — it was in whoever chose the range.
- **Cedar time-based policy.** `context.system.now` is a UTC instant and the policy engine takes no
  zone, so "only during business hours" cannot be expressed there — a fixed UTC window would drift an
  hour twice a year. Split it: absolute bounds (an expiry, a spend cap, a valid-until) belong in
  Cedar, and local-calendar bounds belong in rules, which run in lambdas and can import this.

## who has to call this

The failure mode is silent, so the rule is worth stating flatly: **any code that decides what "this
month", "last week", "today" or "overdue" means is making a civil claim and must resolve it here.**
Storage is not the problem and never was — every timestamp on the wire and in every table is UTC ms,
before and after. The defect is always at the caller that picked a range.

Two live examples of what that looks like in practice, both of which read as ordinary code:

- A recurrence is a civil claim. `FREQ=WEEKLY;BYHOUR=7` means 7am **where the business is**, every
  week of the year, and the instant behind it moves across a daylight-saving change while the wall
  clock does not. `modules/inventory/availability.from_rrule(..., zone=clock.zone())` expands on the
  business's calendar for that reason; expanding against a UTC anchor put a Pacific cafe's 7am
  availability at local midnight and then drifted it an hour twice a year.
- A month is a civil claim. Ask for "July's revenue" from UTC and a 5pm-Pacific sale on July 31 is in
  August. Nothing errors, the trial balance still balances, and the number is just wrong.

## testing

`tests/clock/local/test_clock.py`. The cases that matter are the ones a UTC-only implementation
passes anyway: both DST weeks (167h in spring, 169h in autumn), a half-hour zone (`Asia/Kolkata`) so
nobody assumes integer offsets, a naive timestamp, and the unconfigured-gerp fallback.

`scripts/test.sh` puts this directory on `PYTHONPATH` — locally nothing replicates the bundler, so
without it a lambda that grows an `import clock` passes its own module's tests and fails everyone
else's.
