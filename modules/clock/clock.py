"""The business's clock — the one place civil calendar boundaries are resolved to UTC instants.

Import it from any lambda (`import clock`); `scripts/deploy.py` resolves local imports across
`modules/*/` and bundles it with no build wiring, the same way `modules/agreements` is shared.

WHY THIS EXISTS. Storage is UTC and stays UTC. But a *month* is not an instant — it is a claim about
a calendar, and the calendar that matters is the one the business lives on. Partitioning a ledger by
UTC months books every sale after 5pm Pacific on the last day of a month into the NEXT month: the
trial balance still balances, the P&L is wrong, and nothing flags it. Rendering the timestamps
differently cannot fix that, because the transaction was excluded from the aggregate before anything
was rendered.

So: instants stay UTC ms everywhere. Civil boundaries are computed HERE, in the gerp's zone, and
handed back as UTC ms. Nothing downstream changes shape.

`GERP_TIMEZONE` is an IANA name (`America/Los_Angeles`) supplied per-lambda from the `per_customer`
`timezone` var. Unset or unresolvable falls back to UTC, which is exactly the behaviour every caller
had before this module existed — so a gerp that never configures a zone is unaffected.
"""

import datetime as dt
import os

GERP_TIMEZONE = os.environ.get("GERP_TIMEZONE", "UTC")
CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "")
SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")

UTC = dt.timezone.utc
_zone_cache = {}
_resolved = None   # cold-start resolution of zone_name(); see below


def zone_name():
    """The gerp's IANA zone name.

    ONE source of truth, resolved once per cold start: the owner-editable `GERP#timezone` settings
    row wins, then the `GERP_TIMEZONE` env var (set from terraform at provision time), then UTC. The
    row has to win, or an owner correcting their own clock on the gerp screen would change what the
    agent says while the lambdas kept closing periods on the old one.

    Read failures fall through to the env var rather than raising — an unavailable settings table
    must not take a period close down, and the env var is the value provisioning already set."""
    global _resolved
    if _resolved is None:
        _resolved = GERP_TIMEZONE or "UTC"
        if SETTINGS_TABLE and CUSTOMER_ID:
            try:
                import boto3
                item = boto3.resource("dynamodb").Table(SETTINGS_TABLE).get_item(
                    Key={"gerp_id": CUSTOMER_ID, "sk": "GERP#timezone"}).get("Item") or {}
                if item.get("value"):
                    _resolved = str(item["value"])
            except Exception:  # noqa: BLE001 — no table, no access, throttled: keep the env value
                pass
    return _resolved


def zone():
    """The gerp's ZoneInfo, cached. UTC if unset or unresolvable — a bad zone name must degrade to
    the old behaviour rather than take a period close down. (Reject bad names at WRITE time.)"""
    name = zone_name()
    if name not in _zone_cache:
        z = UTC
        if name != "UTC":
            try:
                from zoneinfo import ZoneInfo
                z = ZoneInfo(name)
            except Exception:  # noqa: BLE001 — unknown/absent zone
                z = UTC
        _zone_cache[name] = z
    return _zone_cache[name]


def is_valid_zone(name) -> bool:
    """Whether an IANA name resolves in THIS runtime. Use at write time so a typo fails at config
    time instead of at period close. Note the zone set differs slightly between our runtimes
    (Lambda AL2023 vs the agent's debian-slim image), so prefer canonical names over aliases."""
    if not name or not isinstance(name, str):
        return False
    if name == "UTC":
        return True
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001
        return False


# ─── instants ↔ wall clock ───

def local(ms) -> dt.datetime:
    """A stored UTC instant as an aware datetime in the gerp's zone, for formatting."""
    return dt.datetime.fromtimestamp(int(ms) / 1000, UTC).astimezone(zone())


def to_utc_ms(value) -> int:
    """A local wall-clock value as a UTC ms instant.

    Accepts ms-epoch (returned unchanged), an ISO string CARRYING an offset (trusted as given), or a
    naive ISO string (placed in the gerp's zone). Raises ValueError on anything unparseable.

    A naive string is placed in the gerp's zone rather than assumed UTC — assuming UTC is the exact
    trapdoor this module exists to close, and it is silent: `datetime.fromisoformat` resolves a naive
    string against the process timezone, which in Lambda is UTC, so "7am" becomes 7am UTC and lands
    seven hours off with no error anywhere."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.isdigit():
        return int(s)
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not a timestamp: {value!r} (want ms-epoch or ISO 8601)") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone())
    return int(parsed.timestamp() * 1000)


# ─── civil boundaries ───
#
# Every one of these takes an instant, works out which local day/week/month/… contains it, and
# returns that period's edges as UTC instants. Half-open [start, end) throughout, matching how the
# ledger is queried.

def _floor_day(d: dt.datetime) -> dt.datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _add_months(d: dt.datetime, n: int) -> dt.datetime:
    """Calendar month arithmetic on a day-1 datetime — timedelta can't do months."""
    y, m = d.year, d.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return d.replace(year=y, month=m, day=1)


def _as_utc_ms(d: dt.datetime) -> int:
    """A local wall clock to a UTC instant. Midnight can be non-existent in zones that spring
    forward at midnight (e.g. America/Santiago) — `fold` picks a real instant either way, and the
    boundary stays monotonic, which is what the ledger query needs."""
    return int(d.timestamp() * 1000)


def period_bounds(kind: str, ms=None):
    """(start_ms, end_ms) for the LOCAL period containing `ms` (default: now).

    kind: day | week | month | quarter | year. Weeks start Monday.

    The whole point of the module: the window is chosen on the business's calendar, and returned as
    UTC instants so callers query exactly as they did before."""
    z = zone()
    now = dt.datetime.fromtimestamp((int(ms) if ms is not None else int(dt.datetime.now(UTC).timestamp() * 1000)) / 1000, UTC).astimezone(z)
    k = (kind or "month").lower()

    if k == "day":
        start = _floor_day(now)
        end = start + dt.timedelta(days=1)
    elif k == "week":
        start = _floor_day(now) - dt.timedelta(days=now.weekday())
        end = start + dt.timedelta(days=7)
    elif k == "month":
        start = _floor_day(now).replace(day=1)
        end = _add_months(start, 1)
    elif k == "quarter":
        start = _floor_day(now).replace(month=3 * ((now.month - 1) // 3) + 1, day=1)
        end = _add_months(start, 3)
    elif k == "year":
        start = _floor_day(now).replace(month=1, day=1)
        end = _add_months(start, 12)
    else:
        raise ValueError(f"unknown period {kind!r} (day|week|month|quarter|year)")

    # DST: a period edge is a wall clock, and adding 24h/7d in local terms can land on a different
    # offset. Re-place both edges in the zone so each is the real instant of that local midnight.
    return _as_utc_ms(start.replace(tzinfo=z)), _as_utc_ms(end.replace(tzinfo=z))


def month_keys(start_ms, end_ms):
    """The `YYYY-MM` ledger partition keys a UTC range spans, in the gerp's LOCAL calendar.

    Replaces per-caller UTC month arithmetic. A range that is local-July yields exactly `2026-07`,
    where the UTC reading of the same instants would also pull `2026-08` (or miss the tail)."""
    s = local(start_ms)
    e = local(end_ms)
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def month_key(ms) -> str:
    """The partition key an instant belongs to, on the business's calendar."""
    d = local(ms)
    return f"{d.year:04d}-{d.month:02d}"
