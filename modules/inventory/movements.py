"""modules/inventory — the movement log (the store).

Every item's on-hand / availability is a **fold** over an append-only log of timestamped `±`
movements, never a stored scalar (`TODO.md` § the movement log). One row shape, two `when` kinds:

    Δ    item          source              when
   +6    minibar_coke  restock             @2026-07-15T09:00     (point)
   −1    room_101      reservation         @[07-15, 07-18)       (period)
   −2    minibar_coke  room_102 purchase   @2026-07-15T21:10     (point)
   +1    room_104      reservation cancel  @[07-15, 07-16)       (period)

  - `@point`  — a stock receive / sell / count-down at an instant; on-hand(t) = Σ Δ ≤ t.
  - `@period` — a capacity booking / cancel over `[start, end)`; availability = default − consumed,
                the interval algebra in `availability.py` (booked minus cancelled).

Totals are **computed** here from the log; the log is the source of truth and truncates to
S3 + Athena without losing anything live (the historical physical-i/o series).

Layers:
  constructors  `point()` / `period()`        — build a movement
  serialize     `to_row()` / `from_row()`      — movement ⇄ stored row (ISO aware-UTC bounds)
  fold (pure)   `on_hand()` / `consumed()` / `available()`
  store         `append()` / `read()`          — local jsonl (tests) or DDB (lambda)
  convenience   `record_point()` / `record_period()` / `item_on_hand()` / `item_available()`

The fold + serialize layers are **pure**; only `append`/`read` touch infra. `available` /
`consumed` are **CAPACITY-1** (boolean coverage via `availability.py`) — a pool (N of a type) is
coverage-counting, a separate mechanism that must not reuse `−` (see `availability.py`).

tz discipline: every datetime normalizes to **aware UTC at ingest** (a naive bound is read as UTC);
`portion` is tz-agnostic, so mixed naive/aware would crash and all-naive-different-tz is silently
wrong. Bounds persist as ISO strings and rebuild to aware-UTC datetimes on read.
"""

import datetime as _dt
import json as _json
import os as _os
import time as _time
import uuid as _uuid
from decimal import Decimal as _Decimal

# `availability` (and its vendored `portion`) is imported lazily, only by the capacity fold
# (`consumed` / `available`). The stock path (`@point`: point/on_hand/append/read) is pure dict +
# arithmetic, so a stock-only lambda (e.g. update_stock) bundles just this file — no vendor/.

POINT = "point"
PERIOD = "period"

from boto3.dynamodb.conditions import Key as _Key

from aws import table as _ddb_table


def _table():
    return _ddb_table(table_name())


def table_name() -> str:
    return _os.environ["MOVEMENTS_TABLE"]


# ─── tz + ids ───

def _utc(dt: _dt.datetime) -> _dt.datetime:
    """Normalize to aware UTC. Naive ⇒ assumed UTC (translate-at-the-boundary); aware ⇒ converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return _utc(dt).isoformat()


def _parse(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s)


def parse_utc(s: str) -> _dt.datetime:
    """ISO-8601 string → aware-UTC datetime (naive ⇒ UTC). The store's ingest normalizer for
    caller-supplied bounds: every datetime crossing into the log or the fold goes through here,
    so the interval layer never sees a mixed naive/aware comparison."""
    return _utc(_dt.datetime.fromisoformat(s))


def new_movement_id() -> str:
    return _uuid.uuid4().hex


# ─── source ───

SOURCE_NAMESPACES = ("job", "po", "produce", "subject")


def check_source(source: str) -> str | None:
    """None when a caller-supplied `source` is legal, else the reason it isn't.

    `source` names WHAT CONSUMED the movement, and it must be `<namespace>:<id>` — a reference to
    another object, never free text. Two reasons, and the second is why it is enforced here rather
    than left to convention:

      - cancel-by-source matches the EXACT string, so a source is a key. Free text makes the undo
        path depend on a caller retyping a phrase identically.
      - the log is APPEND-ONLY and physical movements publish. A name typed into this field is
        published permanently, with no write that can take it back — the one shape where the
        usual "the reference resolves, and a private person resolves to nothing" cannot save it.

    `subject:<contact_id>` is how a booking says whose it is: a reference, resolved like every
    other subject, so a private person is absent from the published row rather than named in it.
    """
    if not source or ":" not in source:
        return (f"source must be <namespace>:<id> — one of {', '.join(SOURCE_NAMESPACES)}. "
                "The movement log is append-only and publishes, so a source is a reference to "
                "another object, never free text (a person goes in as subject:<contact_id>)")
    namespace = source.split(":", 1)[0]
    if namespace not in SOURCE_NAMESPACES:
        return f"unknown source namespace: {namespace} (expected one of {', '.join(SOURCE_NAMESPACES)})"
    return None


def _now_ms() -> int:
    return int(_time.time() * 1000)


# ─── constructors ───

def point(item: str, delta, source: str, at: _dt.datetime) -> dict:
    """A stock movement at an instant: `+`receive / `−`sell / `−`count-down."""
    return {"item": item, "delta": delta, "source": source, "when": {"kind": POINT, "at": at}}


def period(item: str, delta, source: str, start: _dt.datetime, end: _dt.datetime,
           rule: str | None = None, duration: _dt.timedelta | None = None) -> dict:
    """A capacity movement: `−1` booking / `+1` cancel.

    Without `rule` it covers the single interval `[start, end)` — the original shape, and still what
    a one-off booking is. With `rule` it covers a RECURRENCE: `start` is DTSTART, `end` is UNTIL, and
    `duration` is how long each occurrence lasts. One row then carries a fortnight of shifts instead
    of fourteen, which is the point — the caller states the pattern it already holds rather than
    expanding it into rows on the way in."""
    when = {"kind": PERIOD, "start": start, "end": end}
    if rule:
        when["rule"] = rule
        when["duration"] = duration
    return {"item": item, "delta": delta, "source": source, "when": when}


# ─── serialize ───

def to_row(mv: dict, movement_id: str | None = None) -> dict:
    """Movement → stored row. `mv_sk` sorts movements per item by effective time (point `at` /
    period `start`), then by id for uniqueness + idempotent re-put (`attribute_not_exists`)."""
    w = mv["when"]
    mid = movement_id or new_movement_id()
    if w["kind"] == POINT:
        sort = _iso(w["at"])
        bounds = {"at": sort}
    else:
        sort = _iso(w["start"])
        bounds = {"start": _iso(w["start"]), "end": _iso(w["end"])}
        if w.get("rule"):
            bounds["rule"] = w["rule"]
            bounds["duration"] = int(w["duration"].total_seconds())
    return {
        "item_id": mv["item"],
        "mv_sk": f"{sort}#{mid}",
        "movement_id": mid,
        "delta": mv["delta"],
        "source": mv["source"],
        "when_kind": w["kind"],
        # the unit cost AS OF THIS MOVEMENT, when the caller knows it. The item row holds TODAY's
        # cost, so a value-consumed series recomputed from the log would move retroactively every
        # time a cost changed — and disagree with the ledger, which copied the cost at the time.
        **({"unit_cost": mv["unit_cost"]} if mv.get("unit_cost") is not None else {}),
        **bounds,
    }


def from_row(row: dict) -> dict:
    """Stored row → movement (bounds rebuilt to aware-UTC datetimes). Carries `movement_id` so a
    caller can spot its own already-recorded write (an idempotent retry) without a second read."""
    if row["when_kind"] == POINT:
        when = {"kind": POINT, "at": _parse(row["at"])}
    else:
        when = {"kind": PERIOD, "start": _parse(row["start"]), "end": _parse(row["end"])}
        if row.get("rule"):
            when["rule"] = row["rule"]
            when["duration"] = _dt.timedelta(seconds=float(row["duration"]))
    return {
        "item": row["item_id"],
        "movement_id": row.get("movement_id"),
        "delta": row["delta"],
        "source": row["source"],
        "when": when,
        **({"unit_cost": row["unit_cost"]} if row.get("unit_cost") is not None else {}),
    }


# ─── fold (pure) ───

def on_hand(movements: list[dict], at: _dt.datetime | None = None):
    """Stock on-hand = Σ Δ over `@point` movements with `at' ≤ at` (default: the whole log).
    `at` lets a caller ask "on-hand as of T" — a level-timeline read for turnover / reorder."""
    cutoff = _utc(at) if at is not None else None
    total = 0
    for mv in movements:
        w = mv["when"]
        if w["kind"] != POINT:
            continue
        if cutoff is None or _utc(w["at"]) <= cutoff:
            total += mv["delta"]
    return total


def spans(mv: dict, zone=None):
    """The Interval a `@period` movement actually covers.

    A plain movement is the single span `[start, end)`. A rule-bearing one EXPANDS: the recurrence
    anchored at `start` (DTSTART), truncated at `end` (UNTIL), each occurrence `duration` long. So a
    fortnight booked as one row folds to exactly the spans fourteen rows would have.

    `zone` is the calendar the rule was written against, for the same reason the offer needs one — a
    recurrence is a civil claim, and "7am every Saturday" means 7am where the business is, on both
    sides of a daylight-saving change."""
    import availability as A  # lazy: only the capacity fold needs vendored `portion`

    w = mv["when"]
    start, end = _utc(w["start"]), _utc(w["end"])
    if not w.get("rule"):
        return A.span(start, end)
    return A.from_rrule(w["rule"], w["duration"], (start, end), zone=zone, dtstart=start)


def consumed(movements: list[dict], zone=None):
    """Net booked interval over `@period` movements: booked (Δ<0) − cancelled (Δ>0). CAPACITY-1
    boolean coverage — returns a `portion` Interval; not a pool count (see `availability.py`).

    Boolean coverage is why cancelling more than stands is harmless: `cancelled` is a union, so a
    mirror `+1` over an already-released span is a no-op rather than an over-credit."""
    import availability as A  # lazy: only the capacity fold needs vendored `portion`

    booked, cancelled = A.P.empty(), A.P.empty()
    for m in movements:
        if m["when"]["kind"] != PERIOD or m["delta"] == 0:
            continue
        if m["delta"] < 0:
            booked |= spans(m, zone)
        else:
            cancelled |= spans(m, zone)
    return booked - cancelled


def available(movements: list[dict], default, zone=None):
    """Net availability = `default − consumed`. `default` is an availability Interval (e.g.
    `availability.from_rrule(...)` or `span()`); the result is the free Interval. CAPACITY-1.

    `default` must live in the log's tz domain — **aware UTC**. `consumed` is aware-UTC (bounds
    normalize at ingest); `portion` can't compare a naive `default` against it. Normalize the
    window at the boundary (before `from_rrule`), as the store does for every stored bound.

    `zone` is passed through to `consumed` so a rule-bearing booking expands on the business's own
    calendar — the same zone the caller expanded `default` with."""
    return default - consumed(movements, zone)


# ─── store ───

def row_for_write(mv: dict, movement_id: str | None = None) -> dict:
    """The stored row, DDB-ready. Split out from `append` so a caller can carry the same Put inside
    a transaction with the count it belongs to — the two have to land or fail together."""
    row = to_row(mv, movement_id)
    item = {k: (_Decimal(str(v)) if isinstance(v, float) else v) for k, v in row.items()}
    item["created_at"] = _now_ms()
    return item


def append(mv: dict, movement_id: str | None = None) -> str:
    """Append a movement to the log (append-only). Idempotent on `mv_sk`: a re-put with the same
    `movement_id` AND the same effective time is a no-op — `mv_sk` is `<effective time>#<id>`, so a
    caller replaying a movement has to state the time it happened, not let the clock supply one.
    Returns the movement_id."""
    item = row_for_write(mv, movement_id)
    t = _table()
    try:
        t.put_item(Item=item, ConditionExpression="attribute_not_exists(mv_sk)")
    except t.meta.client.exceptions.ConditionalCheckFailedException:
        pass  # already recorded — idempotent
    return item["movement_id"]


def read(item_id: str) -> list[dict]:
    """All movements for an item, as movement dicts, ordered by `mv_sk` (effective time)."""
    rows = []
    kwargs = {"KeyConditionExpression": _Key("item_id").eq(item_id)}
    while True:
        resp = _table().query(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [from_row(r) for r in rows]


# ─── convenience (build + persist / read + fold) ───

def record_point(item, delta, source, at, movement_id=None, unit_cost=None) -> str:
    mv = point(item, delta, source, at)
    if unit_cost is not None:
        mv["unit_cost"] = unit_cost
    return append(mv, movement_id)


def record_period(item, delta, source, start, end, movement_id=None, rule=None, duration=None) -> str:
    return append(period(item, delta, source, start, end, rule, duration), movement_id)


def item_on_hand(item_id, at=None):
    return on_hand(read(item_id), at)


def item_available(item_id, default, zone=None):
    return available(read(item_id), default, zone)
