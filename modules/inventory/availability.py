"""modules/inventory — interval availability (the meter's time axis).

availability = time-dependent inventory: an item's **default availability** is an interval (a union of
atomic spans), a booking is an interval (**scheduled**), and **net availability = default − scheduled**.
One object per booking, arbitrary (off-grid) intervals, no exact-match — replaces the single-timestamp
exdate / slot-discretization the calendar availability half used (`modules/calendar/TODO.md`). Bounds are
datetimes (or anything comparable).

This is the **capacity** half of the inventory movement meter — the `@period` (reusable single-quantity)
items whose availability *totals* are computed here from interval arithmetic. The stock half is `@point`
±deltas summed (its time dimension is turnover, not availability windows). One log, two uses of one time
axis — full spec in `TODO.md` § the movement log.

    default          = from_rrule(rule, duration, window)   # the recurrence, as intervals
    net              = default − scheduled                  # what's free
    is_free(a, b)    = [a, b) ⊆ net                         # can this booking land
    double_book(a,b) = [a, b) ∩ scheduled ≠ ∅               # capacity-1 guard

`portion` (github AlexandreDecan/portion) is vendored beside this file (`vendor/`, ~200k, under the 500k
bar) so no lambda dep. `dateutil` (the RRULE expander) is NOT vendored (>500k) but ships with
boto3/botocore, so it's present in every lambda runtime — imported directly.

Pure — no infra.

**CAPACITY-1 ONLY.** These treat availability as boolean coverage — `default − scheduled` answers "is this
instant still covered," not "how many remain." Correct iff each unit's quantity is 1 (a specific room, a
specific seat). For a **pool** (N of a type) the `−` operator silently DELETES an interval instead of
decrementing the count: two overlapping bookings vanish a unit that still had N−2 free. A pool is
**coverage-counting** — `remaining(t) = capacity − overlaps(t)` over an interval→count map
(`portion.IntervalDict`), available where `remaining > 0` — a SEPARATE mechanism that must NOT reuse `−`.
Build it as its own function; do not extend these.

Conventions:
  - **half-open `[start, end)` throughout** — build via `span()` / `intervals()`, never raw `P.closed`.
    mixing boundary kinds reintroduces single-instant overlaps at back-to-back edges (the phantom-conflict
    this whole model exists to kill).
  - **tz-aware, consistent bounds** (UTC recommended) — `portion` is tz-agnostic: mixed naive/aware raises,
    and all-naive-but-different-tz is *silently* wrong. normalize at the boundary (the store) before an
    interval is built; the interval layer stays tz-clean.
  - **persistence** — `portion.to_data(iv)` → `[(lower_inc, lower, upper, upper_inc), …]`, `from_data()`
    round-trips exactly; store those (or plain `(lower, upper)` rows, since always half-open).
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "vendor"))
import portion as P  # noqa: E402  — vendored


def span(start, end):
    """One half-open interval [start, end) — the unit of both availability and consumption."""
    return P.closedopen(start, end)


def intervals(pairs):
    """Fold (start, end) pairs into one Interval — auto-simplified + disjoint (adjacent nights coalesce)."""
    out = P.empty()
    for start, end in pairs:
        out |= P.closedopen(start, end)
    return out


def from_rrule(rule, duration, window, zone=None, dtstart=None):
    """Expand a recurrence into an Interval. Expand `rule` (an RFC 5545 RRULE string, e.g.
    'FREQ=DAILY') over `window` = (start, end); each occurrence opens a slot of length `duration`
    (a `timedelta`), each slot intersected with the window → the union of spans.
    dateutil does the RRULE expansion (a boto3 dep — present in lambda, not vendored).

    Used for BOTH sides of the meter: an item's offered availability, and a booking that consumes it
    (`movements.spans`). They are the same kind of expression, so they expand through the same code.

    **`dtstart` anchors the recurrence**, and omitting it is only safe for a rule whose phase is
    self-pinning. Default is the window start, which is what the offer side has always passed — but
    that makes the rule FLOAT: `BYDAY=SA` survives because BYDAY fixes the phase itself, while
    anything that takes its phase from DTSTART (`INTERVAL=2` for alternating weeks, a three-day
    cycle) answers differently depending on the day someone asked. A booking always passes its own
    start, because a reservation's occurrences are fixed when it is made, not when it is read.

    **`zone` is the calendar the rule is written against, and it matters.** A recurrence is a CIVIL
    claim: "available Saturdays at 7am" means 7am where the business is, not 7am UTC. Expanded
    against a UTC anchor, `BYHOUR=7` is midnight Pacific, and even a pre-converted `BYHOUR=14` drifts
    an hour at every daylight-saving change — the rule would silently mean 8am for half the year.

    So the expansion runs in `zone` (the wall clock the rule was written in) and the resulting spans
    convert back to instants. Pass `clock.zone()` from a lambda; `availability.py` stays pure and
    takes no environment of its own. Omitted ⇒ the window's own tz, which is the old behaviour."""
    from dateutil.rrule import rrulestr
    ws, we = window
    if zone is not None:
        ws_l, we_l = ws.astimezone(zone), we.astimezone(zone)
    else:
        ws_l, we_l = ws, we
    # The anchor must sit in the same frame the expansion runs in, or BYHOUR lands an offset out.
    anchor = ws_l if dtstart is None else (dtstart.astimezone(zone) if zone is not None else dtstart)
    r = rrulestr(rule, dtstart=anchor)
    slots = []
    for occ in r.between(ws_l, we_l, inc=True):
        lo, hi = max(occ, ws_l), min(occ + duration, we_l)
        if lo < hi:
            # back to the window's frame — the movement log it will be differenced against is
            # instants, and a mixed-frame subtraction is how a wrong hour becomes a wrong answer
            slots.append((lo.astimezone(ws.tzinfo) if zone is not None else lo,
                          hi.astimezone(ws.tzinfo) if zone is not None else hi))
    return intervals(slots)


def net_availability(default, scheduled):
    """Net free availability = default − scheduled. Both Intervals (single span or a union). Returns the
    free Interval; iterate it for the atomic free spans (`(a.lower, a.upper) for a in net`).

    CAPACITY-1 ONLY: `−` is boolean coverage — it deletes an interval, it never decrements a count. Never
    call this for a pool (N > 1 of a type); that's coverage-counting, a separate function (see module note)."""
    return default - scheduled


def is_free(default, scheduled, start, end):
    """Is [start, end) fully offered and unbooked — i.e. ⊆ (default − scheduled)? Robust to unions:
    a range is contained iff subtracting the net leaves nothing."""
    req = P.closedopen(start, end)
    return (req - (default - scheduled)).empty


def double_book(scheduled, start, end):
    """Would booking [start, end) collide with what's already scheduled? (capacity-1 overlap guard)."""
    return (P.closedopen(start, end) & scheduled) != P.empty()
