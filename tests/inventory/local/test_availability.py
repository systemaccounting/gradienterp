"""Unit tests for modules/inventory/availability — interval availability via `portion`.
net availability = default − scheduled; the fix for single-timestamp exdates (arbitrary
off-grid ranges, no exact-match). Pure; needs `portion` (run under the repo .venv)."""

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "inventory"))

import availability as A  # noqa: E402


def d(day, h=0):
    return dt.datetime(2026, 7, day, h)


def spans(interval):
    return [(a.lower, a.upper) for a in interval]


def test_off_grid_range_carved_cleanly():
    # room open 7/15–7/18; a booking mid-window, off any night grid → two free spans, exact-match not needed
    default = A.span(d(15), d(18))
    scheduled = A.span(d(16, 14), d(17, 11))
    assert spans(A.net_availability(default, scheduled)) == [
        (d(15), d(16, 14)),
        (d(17, 11), d(18)),
    ]


def test_nightly_union_coalesces_then_subtracts():
    # rrule-style per-night defaults coalesce to one span; one night booked carves a hole
    default = A.intervals([(d(15), d(16)), (d(16), d(17)), (d(17), d(18))])
    scheduled = A.span(d(16), d(17))
    assert spans(A.net_availability(default, scheduled)) == [(d(15), d(16)), (d(17), d(18))]


def test_is_free():
    default = A.span(d(15), d(18))
    scheduled = A.span(d(16, 14), d(17, 11))
    assert A.is_free(default, scheduled, d(15), d(16))          # inside a free span
    assert not A.is_free(default, scheduled, d(16, 12), d(17))  # overlaps the booking
    assert not A.is_free(default, scheduled, d(14), d(15))      # outside default (not offered)
    assert not A.is_free(default, scheduled, d(15), d(17))      # spans across the booked hole


def test_from_rrule_daily_over_window():
    # daily availability over a 3-night window coalesces to one [15,18) span (the rrule generator);
    # booking the middle night carves the hole — needs dateutil (a boto3 dep, present in lambda)
    default = A.from_rrule("FREQ=DAILY", dt.timedelta(days=1), (d(15), d(18)))
    assert spans(default) == [(d(15), d(18))]
    net = A.net_availability(default, A.span(d(16), d(17)))
    assert spans(net) == [(d(15), d(16)), (d(17), d(18))]


def test_double_book_guard():
    scheduled = A.span(d(16, 14), d(17, 11))
    assert A.double_book(scheduled, d(16, 20), d(17, 8))   # inside the booked span
    assert A.double_book(scheduled, d(16), d(16, 15))      # partial overlap at the edge
    assert not A.double_book(scheduled, d(15), d(16, 14))  # adjacent, no overlap (half-open)
    assert not A.double_book(scheduled, d(17, 11), d(18))  # after checkout, free


def test_a_recurrence_is_a_civil_claim_not_a_utc_one():
    """"Available Saturdays at 7am" means 7am WHERE THE BUSINESS IS, every week of the year.

    Expanded against a UTC anchor it lands at local midnight, and even a hand-converted BYHOUR
    drifts an hour at each daylight-saving change — so the same rule would mean 7am for half the
    year and 8am for the other half. The wall clock must hold and the INSTANT must move."""
    from zoneinfo import ZoneInfo
    LA = ZoneInfo("America/Los_Angeles")
    U = dt.timezone.utc
    rule = "FREQ=WEEKLY;BYDAY=SA,SU,MO,TU,WE;BYHOUR=7;BYMINUTE=0;BYSECOND=0"
    dur = dt.timedelta(hours=8)

    summer = list(A.from_rrule(rule, dur, (dt.datetime(2026, 7, 26, tzinfo=U), dt.datetime(2026, 7, 29, tzinfo=U)), zone=LA))[0]
    winter = list(A.from_rrule(rule, dur, (dt.datetime(2026, 1, 10, tzinfo=U), dt.datetime(2026, 1, 13, tzinfo=U)), zone=LA))[0]

    # the wall clock is the same in both halves of the year …
    assert summer.lower.astimezone(LA).hour == 7
    assert winter.lower.astimezone(LA).hour == 7
    # … and the instant is not, which is the whole point
    assert summer.lower.astimezone(U).hour == 14   # PDT, −7
    assert winter.lower.astimezone(U).hour == 15   # PST, −8


def test_without_a_zone_it_keeps_the_old_utc_behaviour():
    """The parameter is opt-in: a caller that passes no zone gets exactly what it got before."""
    U = dt.timezone.utc
    rule = "FREQ=WEEKLY;BYDAY=SA;BYHOUR=7;BYMINUTE=0;BYSECOND=0"
    got = list(A.from_rrule(rule, dt.timedelta(hours=8), (dt.datetime(2026, 7, 24, tzinfo=U), dt.datetime(2026, 7, 27, tzinfo=U))))[0]
    assert got.lower.astimezone(U).hour == 7


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all availability tests passed")
