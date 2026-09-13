"""modules/inventory — shared helpers for the CAPACITY tools (`get_availability`, `reserve`).

A **capacity** item is one whose meter is availability over time rather than a stock count: its
`availability_rule` (RRULE) + `availability_duration` (seconds) create the DEFAULT windows, and its
`@period` movements consume them (`net = default − scheduled`). Both tools need the same two things —
resolve the item's `(rule, duration)`, and normalize the caller's range to aware UTC — so they live
here instead of twice.

Each helper returns `(value, None)` on success or `(None, error_response)` on failure, so a handler
reads as a flat sequence of guards.
"""

import datetime as dt

import movements as M
from _helpers import local_get_item, err


def load_capacity_item(item_id):
    """`((item, rule, duration, canonical_id), None)` for a capacity item, else `(None, error)`.

    The CANONICAL id is part of the return because the movement log must be keyed the same way the
    item is. Resolving `shift-ava` to the row `1#shift-ava` and then writing its bookings under the
    raw string splits the meter in two: the item says it offers 40 hours, the log says nothing is
    booked, and both are reading different keys. Callers use the returned id, never their input.

    An unprefixed id falls back to location #1, because `create_item` already defaults that way:
    create `shift-ava` and the row is `1#shift-ava`. Without the same default here, a caller can
    write an item it then cannot read back under the name it used — and the failure looks like
    "no such item", so a caller concludes the thing doesn't exist rather than that it guessed the
    key. That asymmetry cost a scheduler its whole availability set."""
    item = local_get_item(item_id)
    if not item and "#" not in str(item_id):
        item = local_get_item(f"1#{item_id}")
        if item:
            item_id = f"1#{item_id}"
    if not item:
        return None, err(f"item not found: {item_id}", status=404)

    rule = item.get("availability_rule")
    if not rule:
        return None, err(
            f"not a capacity item: {item_id} has no availability_rule (its meter is a stock count)",
            status=400,
        )
    canonical = item.get("item_id", item_id)
    seconds = float(item.get("availability_duration") or 0)
    if seconds <= 0:
        return None, err(f"capacity item {item_id} has a non-positive availability_duration")
    return (item, rule, dt.timedelta(seconds=seconds), canonical), None


def parse_range(body, keys=("from", "to")):
    """`((start, end), None)` as aware UTC, else `(None, error_response)`. Normalizing here is what
    keeps the interval layer tz-clean — `portion` can't compare a naive bound against an aware one."""
    lo, hi = keys
    if not body.get(lo) or not body.get(hi):
        return None, err(f"{lo} and {hi} are required (ISO-8601)")
    try:
        start = M.parse_utc(body[lo])
        end = M.parse_utc(body[hi])
    except ValueError as e:
        return None, err(f"invalid datetime: {e}")
    if start >= end:
        return None, err(f"{lo} must be before {hi}")
    return (start, end), None
