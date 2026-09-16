"""queries — the SQL the fixed reads run, and the window they run over.

One template per read, two dialects. Athena is Trino and the local engine is duckdb; they agree on
`date_trunc`, `count(distinct …)`, `filter (where …)`, `date_diff` and map subscripts, and differ
on how an ISO string becomes a local timestamp and how a timestamp prints. Those two expressions
are the whole dialect table.

`ts` is the UTC instant as an ISO string with milliseconds and a Z (`metrics.normalize_at`), so a
window is a string comparison and needs no cast. Bins are cut on the firm's own calendar: the
timestamp is placed in the firm's zone (modules/clock) before `date_trunc`.

Every value spliced into a template is checked first: an event name against `metrics.EVENT_RE`,
a property name against `[a-z0-9_]+`, the zone from clock, the window built here.
"""

import datetime as dt
import re

import clock
import metrics

GRAINS = ("day", "week", "month")
PROPERTY_RE = re.compile(r"^[a-z0-9_]+$")
WINDOWS = ("today", "this_week", "this_month", "last_month", "this_year",
           "last_7_days", "last_30_days", "last_90_days")

DIALECTS = {
    "athena": {"local": "from_iso8601_timestamp(ts) AT TIME ZONE '{zone}'",
               "text":  "date_format({x}, '%Y-%m-%d')"},
    "duckdb": {"local": "(ts::TIMESTAMPTZ) AT TIME ZONE '{zone}'",
               "text":  "strftime({x}, '%Y-%m-%d')"},
}


class Bad(ValueError):
    """A read argument the query cannot take; the message names it."""


def _iso(ms: int) -> str:
    t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def window(name=None, start=None, end=None) -> dict:
    """`{start, end, label}` as UTC ISO strings, cut on the firm's calendar.

    `start`/`end` (ISO date or datetime, naive = the firm's zone) win when both are given; else a
    named window, default this_month. `end` is exclusive."""
    if start or end:
        if not (start and end):
            raise Bad("start and end go together, ISO dates in the firm's zone")
        s, e = clock.to_utc_ms(start), clock.to_utc_ms(end)
        if e <= s:
            raise Bad("end is before start")
        return {"start": _iso(s), "end": _iso(e), "label": f"{start}..{end}"}
    name = (name or "this_month").strip().lower()
    if name not in WINDOWS:
        raise Bad(f"window: one of {', '.join(WINDOWS)}, or start and end")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    if name.startswith("last_") and name.endswith("_days"):
        days = int(name.split("_")[1])
        s, e = clock.period_bounds("day", now)
        s = s - (days - 1) * 86_400_000
    elif name == "last_month":
        this_s, _ = clock.period_bounds("month", now)
        s, e = clock.period_bounds("month", this_s - 1)
    else:
        kind = {"today": "day", "this_week": "week", "this_month": "month", "this_year": "year"}[name]
        s, e = clock.period_bounds(kind, now)
    return {"start": _iso(s), "end": _iso(e), "label": name}


def _event(e: str) -> str:
    if not isinstance(e, str) or not metrics.EVENT_RE.fullmatch(e):
        raise Bad(metrics.EVENT_RULE)
    return e


def _grain(g) -> str:
    g = (g or "day").strip().lower()
    if g not in GRAINS:
        raise Bad("grain: day, week or month")
    return g


def _bin(dialect: str, grain: str, zone: str) -> str:
    return f"date_trunc('{grain}', {DIALECTS[dialect]['local'].format(zone=zone)})"


def _text(dialect: str, x: str) -> str:
    return DIALECTS[dialect]["text"].format(x=x)


def _where(event: str, w: dict) -> str:
    return f"event = '{event}' AND ts >= '{w['start']}' AND ts < '{w['end']}'"


def count(dialect, event, w, grain="day", by=None, zone="UTC") -> str:
    event, grain = _event(event), _grain(grain)
    bin_ = _bin(dialect, grain, zone)
    cols, keys = [f"{_text(dialect, bin_)} AS period"], ["1"]
    if by:
        if not isinstance(by, str) or not PROPERTY_RE.fullmatch(by):
            raise Bad("by: a property name, lowercase letters, digits and _")
        cols.append(f"properties['{by}'] AS by_value")
        keys.append("2")
    return (f"SELECT {', '.join(cols)}, count(*) AS n FROM metrics WHERE {_where(event, w)} "
            f"GROUP BY {', '.join(keys)} ORDER BY {', '.join(keys)}")


def distinct(dialect, event, w, grain="day", zone="UTC") -> str:
    event, grain = _event(event), _grain(grain)
    bin_ = _bin(dialect, grain, zone)
    return (f"SELECT {_text(dialect, bin_)} AS period, count(DISTINCT subject_id) AS n FROM metrics "
            f"WHERE {_where(event, w)} GROUP BY 1 ORDER BY 1")


def funnel(dialect, events, w) -> str:
    """One row: how many subjects reached each step, in order. A subject counts at step i when it
    did every earlier step first (by the time of its first occurrence of each)."""
    if not isinstance(events, list) or len(events) < 2:
        raise Bad("events: two or more steps in order")
    steps = [_event(e) for e in events]
    firsts = ", ".join(f"min(CASE WHEN event = '{e}' THEN ts END) AS t{i}" for i, e in enumerate(steps))
    inlist = ", ".join(f"'{e}'" for e in steps)
    counts = ["count(t0) AS s0"]
    for i in range(1, len(steps)):
        ordered = " AND ".join(f"t{j + 1} >= t{j}" for j in range(i))
        counts.append(f"count(CASE WHEN {ordered} THEN 1 END) AS s{i}")
    return (f"WITH steps AS (SELECT subject_id, {firsts} FROM metrics WHERE event IN ({inlist}) "
            f"AND ts >= '{w['start']}' AND ts < '{w['end']}' GROUP BY subject_id) "
            f"SELECT {', '.join(counts)} FROM steps")


def retention(dialect, event, w, grain="week", zone="UTC") -> str:
    """cohort (the period a subject first did `event`) × offset (periods since) → subjects."""
    event, grain = _event(event), _grain(grain)
    bin_ = _bin(dialect, grain, zone)
    return (f"WITH firsts AS (SELECT subject_id, min({bin_}) AS cohort FROM metrics WHERE {_where(event, w)} GROUP BY 1), "
            f"seen AS (SELECT DISTINCT subject_id, {bin_} AS period FROM metrics WHERE {_where(event, w)}) "
            f"SELECT {_text(dialect, 'f.cohort')} AS cohort, date_diff('{grain}', f.cohort, s.period) AS offset_n, "
            f"count(DISTINCT s.subject_id) AS n FROM firsts f JOIN seen s ON f.subject_id = s.subject_id "
            f"GROUP BY 1, 2 ORDER BY 1, 2")


def fold_retention(rows: list) -> list:
    """`[{cohort, offset_n, n}]` → `[{cohort, size, periods: [n0, n1, …]}]`, gaps as 0."""
    by = {}
    for r in rows:
        by.setdefault(r["cohort"], {})[int(r["offset_n"])] = int(r["n"])
    out = []
    for cohort in sorted(by):
        cells = by[cohort]
        width = max(cells) + 1 if cells else 0
        periods = [cells.get(i, 0) for i in range(width)]
        out.append({"cohort": cohort, "size": periods[0] if periods else 0, "periods": periods})
    return out


def fold_funnel(events: list, row: dict) -> list:
    first = int(row.get("s0") or 0)
    out = []
    for i, e in enumerate(events):
        n = int(row.get(f"s{i}") or 0)
        out.append({"event": e, "subjects": n, "rate": round(n / first, 4) if first else 0.0})
    return out
