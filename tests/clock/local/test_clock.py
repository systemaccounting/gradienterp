"""Local tests for modules/clock — the civil-boundary library.

The cases that matter are the ones a UTC-only implementation gets wrong, and the ones a naive
implementation gets wrong at DST transitions. A test suite that only checks mid-July noon would
pass against the buggy code this module replaces.
"""
import datetime as dt
import importlib
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "clock"))
LA = ZoneInfo("America/Los_Angeles")


def _clock(tz):
    os.environ["GERP_TIMEZONE"] = tz
    sys.modules.pop("clock", None)
    return importlib.import_module("clock")


def _ms(y, mo, d, h=0, mi=0, tz=LA):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)


def test_month_bounds_are_local_midnight_not_utc_midnight():
    c = _clock("America/Los_Angeles")
    start, end = c.period_bounds("month", _ms(2026, 7, 15, 12))
    assert c.local(start).strftime("%Y-%m-%d %H:%M") == "2026-07-01 00:00"
    assert c.local(end).strftime("%Y-%m-%d %H:%M") == "2026-08-01 00:00"
    # expressed as UTC instants — storage never changes
    assert dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).hour == 7  # PDT is -7


def test_the_defect_this_module_exists_for():
    """A 6:30pm Pacific sale on Jul 31 belongs to July. Under UTC months it lands in August."""
    c = _clock("America/Los_Angeles")
    sale = _ms(2026, 7, 31, 18, 30)
    jul_start, jul_end = c.period_bounds("month", _ms(2026, 7, 15))
    assert jul_start <= sale < jul_end, "the evening sale must be inside local July"
    # and the UTC reading of the same instant is August — which is the bug
    assert dt.datetime.fromtimestamp(sale / 1000, dt.timezone.utc).month == 8
    assert list(c.month_keys(sale, sale)) == ["2026-07"]


def test_month_keys_do_not_bleed_into_the_next_partition():
    c = _clock("America/Los_Angeles")
    start, end = c.period_bounds("month", _ms(2026, 7, 15))
    assert list(c.month_keys(start, end - 1)) == ["2026-07"]


def test_bounds_hold_across_the_spring_forward_week():
    """DST starts Sun 2026-03-08, which is in the week beginning Mon 2026-03-02. That week is seven
    LOCAL days but only 167 real hours — the boundaries must still be local midnights."""
    c = _clock("America/Los_Angeles")
    start, end = c.period_bounds("week", _ms(2026, 3, 4, 12))
    assert c.local(start).strftime("%Y-%m-%d %H:%M") == "2026-03-02 00:00"
    assert c.local(end).strftime("%Y-%m-%d %H:%M") == "2026-03-09 00:00"
    assert (end - start) / 3_600_000 == 167.0


def test_bounds_hold_across_the_fall_back_week():
    """DST ends Sun 2026-11-01, in the week beginning Mon 2026-10-26 — seven local days, 169 hours."""
    c = _clock("America/Los_Angeles")
    start, end = c.period_bounds("week", _ms(2026, 10, 28, 12))
    assert c.local(start).strftime("%Y-%m-%d %H:%M") == "2026-10-26 00:00"
    assert c.local(end).strftime("%Y-%m-%d %H:%M") == "2026-11-02 00:00"
    assert (end - start) / 3_600_000 == 169.0


def test_quarter_and_year_are_local_too():
    c = _clock("America/Los_Angeles")
    qs, qe = c.period_bounds("quarter", _ms(2026, 8, 20))
    assert c.local(qs).strftime("%Y-%m-%d") == "2026-07-01"
    assert c.local(qe).strftime("%Y-%m-%d") == "2026-10-01"
    ys, ye = c.period_bounds("year", _ms(2026, 8, 20))
    assert c.local(ys).strftime("%Y-%m-%d") == "2026-01-01"
    assert c.local(ye).strftime("%Y-%m-%d") == "2027-01-01"


def test_naive_iso_is_placed_in_the_gerp_zone_not_assumed_utc():
    """The silent seven-hour trapdoor. `datetime.fromisoformat` would resolve this against the
    process timezone (UTC in Lambda) and be wrong with no error."""
    c = _clock("America/Los_Angeles")
    got = c.to_utc_ms("2026-07-27T07:00:00")
    assert got == _ms(2026, 7, 27, 7)
    assert dt.datetime.fromtimestamp(got / 1000, dt.timezone.utc).hour == 14


def test_an_explicit_offset_is_trusted_as_given():
    c = _clock("America/Los_Angeles")
    assert c.to_utc_ms("2026-07-27T14:00:00Z") == _ms(2026, 7, 27, 7)
    assert c.to_utc_ms(1785160800000) == 1785160800000


def test_unparseable_raises_rather_than_guessing():
    c = _clock("America/Los_Angeles")
    for bad in ("", "next tuesday", "07/27/2026"):
        try:
            c.to_utc_ms(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass


def test_a_half_hour_zone_works():
    """Guards against anyone assuming integer-hour offsets."""
    c = _clock("Asia/Kolkata")
    start, _ = c.period_bounds("month", int(dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc).timestamp() * 1000))
    assert dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).strftime("%H:%M") == "18:30"


def test_unset_or_bad_zone_degrades_to_utc():
    """A gerp that never configures a zone must behave exactly as before this module existed."""
    for tz in ("UTC", "Not/AZone"):
        c = _clock(tz)
        start, end = c.period_bounds("month", int(dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc).timestamp() * 1000))
        assert dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-07-01 00:00"
        assert dt.datetime.fromtimestamp(end / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-08-01 00:00"


def test_zone_validation():
    c = _clock("UTC")
    assert c.is_valid_zone("America/Los_Angeles") and c.is_valid_zone("UTC")
    assert not c.is_valid_zone("Pacific") and not c.is_valid_zone("") and not c.is_valid_zone(None)


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all clock tests passed")
