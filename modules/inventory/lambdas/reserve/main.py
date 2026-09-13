"""reserve — a CAPACITY item's one tool: book, cancel, or read availability.

`op: availability` answers "when is it free" — the free windows over a range (`default −
scheduled`, folded from the log) plus utilization. The other two ops write:

A booking is a `−1` `@period` movement; a cancel is a `+1` mirroring it.
They net out in the fold (`movements.consumed` = booked − cancelled), so availability is always
`default − scheduled` — there is no state to mutate, only the log to append.

A booking states EITHER one interval (`start`/`end`) or a pattern (`rule` + `duration`, with `start`
as its DTSTART and `end` as its UNTIL). The pattern form is why a fortnight of shifts for one person
is one call and one row rather than fourteen of each: a schedule is a recurrence, the same kind of
expression as the availability it consumes, and expanding it into intervals on the way in throws that
away. It is bounded by construction — `end` is required — so there is no open-ended reservation.

The guard is interval arithmetic, not an exact slot match: the request must lie inside the item's
offered default availability AND not overlap what's already booked. It runs per OCCURRENCE, so a
recurring booking is refused unless every one of its days is legal, and the error names the day that
failed. An off-grid range (a 2pm-to-11am stay) works exactly like an on-grid one — the reason the
store is intervals rather than per-slot rows.

The `source` is the consumer: `source=job:<invoice_id>` binds the time leg to the invoice that carries
the money leg. `op=cancel` with a `source` and no interval un-books every booking recorded under that
source in one call — the undo a job needs. The mirror `+1` carries the same source, so which bookings
are still standing is itself a fold (a booking is cancelled iff a `+1` with its source and interval
exists).

Physical only — no journal. A booking consumes capacity; the money posts when it's invoiced
(the transaction object), which is where the revenue account is resolved.
"""

import datetime as dt
import json

import availability as A
import movements as M
from capacity import load_capacity_item, parse_range
from _helpers import ok, err


def cancel_by_source(item_id, source):
    """Un-book everything still standing under `source` — the undo a job needs, in one call.

    Releases per BOOKING, not per atomic span: a booking still wholly standing is mirrored by one
    `+1` carrying its own rule, so undoing a fortnight costs one row instead of fourteen. One already
    partly released falls back to spans, so a partial cancel or a rebook is still handled by the same
    fold the availability read uses and the response never claims to release what was already gone.
    Idempotent: a re-run finds nothing standing."""
    loaded, error = load_capacity_item(item_id)
    if error:
        return error
    item, _, _, item_id = loaded

    import clock
    zone = clock.zone()
    log = [m for m in M.read(item_id) if m["source"] == source]
    if M.consumed(log, zone).empty:
        return err(f"no bookings to cancel under source: {source}", status=404, item_id=item_id)

    # Release per BOOKING row, not per atomic span of the fold. A booking still wholly standing is
    # mirrored by one `+1` carrying its own rule, so undoing a fortnight costs one row rather than
    # fourteen — the log stays as compact as the bookings that made it. A booking already partly
    # released falls back to spans, so the response never claims to cancel what was cancelled
    # before, and a re-run still finds nothing standing.
    already = A.intervals([])
    for mv in log:
        if mv["when"]["kind"] == M.PERIOD and mv["delta"] > 0:
            already |= M.spans(mv, zone)

    cancelled = []
    for mv in log:
        w = mv["when"]
        if w["kind"] != M.PERIOD or mv["delta"] >= 0:
            continue
        mine = M.spans(mv, zone)
        standing = mine - already
        if standing.empty:
            continue
        if standing == mine:
            movement_id = M.record_period(item_id, 1, source, w["start"], w["end"],
                                          rule=w.get("rule"), duration=w.get("duration"))
            cancelled.append({
                "start": w["start"].isoformat(), "end": w["end"].isoformat(),
                **({"rule": w["rule"]} if w.get("rule") else {}),
                "movement_id": movement_id,
            })
            continue
        for span in standing:
            movement_id = M.record_period(item_id, 1, source, span.lower, span.upper)
            cancelled.append({
                "start": span.lower.isoformat(), "end": span.upper.isoformat(),
                "movement_id": movement_id,
            })
    return ok({
        "item_id": item_id, "name": item.get("name", ""), "op": "cancel",
        "source": source, "cancelled": cancelled,
    })


def _availability(body):
    """The free windows of a capacity item over a range — the old get_availability, verbatim.

    net availability = default − scheduled. The item's `availability_rule` (RRULE) +
    `availability_duration` create its DEFAULT windows across the requested range; its `@period`
    movements (a booking is a `−1`, a cancel a `+1`) are folded and subtracted. Pure read — the
    totals are computed from the movement log, never stored. Capacity-1: `−` is boolean coverage.
    A stock item has no `availability_rule` and is rejected; its meter is a count."""
    item_id = body.get("item_id")
    if not item_id:
        return err("item_id is required")

    rng, error = parse_range(body)
    if error:
        return error
    start, end = rng

    loaded, error = load_capacity_item(item_id)
    if error:
        return error
    item, rule, duration, item_id = loaded   # the id the row actually has — movements key off it

    # the rule is a CIVIL claim — expand it on the business's calendar, not on UTC
    import clock
    zone = clock.zone()
    default = A.from_rrule(rule, duration, (start, end), zone=zone)
    # `zone` reaches the fold too: a booking may itself be a recurrence, and it expands on the same
    # business calendar the offer does.
    net = M.available(M.read(item_id), default, zone)

    # Report in the BUSINESS's offset, not normalized to Z. Same instants either way — but the
    # lambda already knows the zone (it just expanded the rule with it), and returning `…T14:00Z`
    # makes every caller redo that conversion to answer "which day, what hour". The agent has burned
    # whole turns on it, once fatally: it read `07:00Z` as `11pm PDT the day before` and spent the
    # rest of the turn trying to reconcile a shift that looked like it started at midnight.
    def _local(t):
        return t.astimezone(zone).isoformat()

    free = [{"from": _local(span.lower), "to": _local(span.upper)} for span in net]
    offered_seconds = int(sum((s.upper - s.lower).total_seconds() for s in default))
    free_seconds = int(sum((s.upper - s.lower).total_seconds() for s in net))
    booked_seconds = offered_seconds - free_seconds
    return ok({
        "item_id": item_id,
        "name": item.get("name", ""),
        # The RULE, not just the windows it opened. A caller reading only expanded spans has to
        # re-derive the pattern from dates, and a query window that clips the first and last weeks
        # makes a perfectly regular `SA,SU,MO,TU,WE` look ragged — which is how a scheduler decided a
        # shift lead "has no clean weekly pattern" and booked ten days individually. It is also the
        # rule to hand straight back to book the whole pattern in one call.
        "availability_rule": rule,
        "availability_duration": int(duration.total_seconds()),
        "from": _local(start),
        "to": _local(end),
        "free": free,
        "free_seconds": free_seconds,
        # utilization = booked ÷ offered over the range — a room's occupancy, a bay's utilization
        "offered_seconds": offered_seconds,
        "booked_seconds": booked_seconds,
        "utilization": round(booked_seconds / offered_seconds, 4) if offered_seconds else None,
    })


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    item_id = body.get("item_id")
    if not item_id:
        return err("item_id is required")

    op = body.get("op", "book")
    if op == "availability":
        return _availability(body)
    if op not in ("book", "cancel"):
        return err("op must be book | cancel | availability")

    if body.get("source"):
        bad = M.check_source(body["source"])
        if bad:
            return err(bad)

    # cancel-by-source: a source and no interval — release the whole job in one call
    if op == "cancel" and body.get("source") and not (body.get("start") or body.get("end")):
        return cancel_by_source(item_id, body["source"])

    rng, error = parse_range(body, keys=("start", "end"))
    if error:
        return error
    start, end = rng

    loaded, error = load_capacity_item(item_id)
    if error:
        return error
    item, rule, duration, item_id = loaded

    # A booking may state a RECURRENCE instead of one interval — `start` is its DTSTART, `end` its
    # UNTIL. One call then books a fortnight of shifts, where fourteen were needed before. Bounded
    # by construction: `end` is required, so a rule with no UNTIL of its own is still truncated and
    # there is no unbounded reservation to reject.
    book_rule = body.get("rule")
    book_duration = None
    if book_rule:
        secs = body.get("duration")
        if not secs or float(secs) <= 0:
            return err("duration (seconds per occurrence) is required with rule")
        book_duration = dt.timedelta(seconds=float(secs))

    # the rule is a CIVIL claim — expand it on the business's calendar, not on UTC
    import clock
    zone = clock.zone()
    default = A.from_rrule(rule, duration, (start, end), zone=zone)
    log = M.read(item_id)

    # An idempotent retry has to short-circuit BEFORE the guard: the caller's first attempt already
    # consumed the interval, so re-running the guard would reject its own booking as a double-book.
    movement_id = body.get("movement_id")
    if movement_id and any(m.get("movement_id") == movement_id for m in log):
        return ok({
            "item_id": item_id, "name": item.get("name", ""), "op": op,
            "start": start.isoformat(), "end": end.isoformat(),
            "movement_id": movement_id, "duplicate": True,
        })

    scheduled = M.consumed(log, zone)

    # What this request actually covers: one span, or every occurrence the rule opens. The guard runs
    # per OCCURRENCE — a recurring booking has to be legal on all fourteen days, and reporting which
    # one failed is the difference between "no" and a schedule the owner can fix.
    wanted = M.spans({"when": {"kind": M.PERIOD, "start": start, "end": end,
                              **({"rule": book_rule, "duration": book_duration} if book_rule else {})}},
                     zone)
    if wanted.empty:
        return err("the rule opens no occurrences between start and end", item_id=item_id)

    if op == "book":
        for occ in wanted:
            if A.is_free(default, scheduled, occ.lower, occ.upper):
                continue
            when = f"{occ.lower.isoformat()} → {occ.upper.isoformat()}"
            # both are 409s, but the owner needs to know WHICH: a clash is someone else's booking,
            # an unoffered range is the item's own rule not opening then.
            if A.double_book(scheduled, occ.lower, occ.upper):
                return err(f"already booked over {when}", status=409, item_id=item_id)
            return err(
                f"outside the item's offered availability ({when} — its availability_rule "
                f"does not open then)",
                status=409, item_id=item_id,
            )
        delta = -1
    else:
        if (wanted & scheduled).empty:
            return err("no booking to cancel over that interval", status=404, item_id=item_id)
        delta = 1

    movement_id = M.record_period(
        item_id, delta, body.get("source") or op, start, end, movement_id=movement_id,
        rule=book_rule, duration=book_duration,
    )

    # No availability echoed back: this handler only ever folds over [start, end) — the interval it
    # just consumed — so a "free" list here is empty by construction on a book and the whole range on
    # a cancel. Neither says anything. Ask get_availability over the range you actually care about.
    return ok({
        "item_id": item_id,
        "name": item.get("name", ""),
        "op": op,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "movement_id": movement_id,
        # A recurring booking says what it actually took: one row, N occurrences. Without the count
        # the caller can't tell a fortnight from a single day.
        **({"rule": book_rule, "occurrences": len(list(wanted))} if book_rule else {}),
    })
