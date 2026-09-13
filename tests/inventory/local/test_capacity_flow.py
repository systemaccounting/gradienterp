"""Local tests for the CAPACITY tools — get_availability + reserve.

A capacity item (a room) is metered by availability over time, not a stock count: its
availability_rule creates the default windows and a booking subtracts an interval
(net = default - scheduled). Exercises the tools end-to-end in local mode against the
movement log. Needs `portion` — run under the repo .venv.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, load_tool, scratch_env


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _room(create, item_id="1#room_101"):
    code, body = _invoke(create, {
        "item_id": item_id,
        "name": "Deluxe Room 101",
        "unit": "night",
        "unit_cost": 40.00,
        "unit_price": 180.00,
        "availability_rule": "FREQ=DAILY",
        "availability_duration": 86400,  # a full day/night, so consecutive nights coalesce
    })
    assert code == 200, body
    return body


def test_capacity_item_created_with_rule():
    with scratch_env():
        create = load_tool("create_item")
        body = _room(create)
        assert body["item"]["availability_rule"] == "FREQ=DAILY"
        assert body["item"]["quantity"] == 0  # meaningless for capacity — its meter is time


def test_availability_rule_requires_duration():
    with scratch_env():
        create = load_tool("create_item")
        code, body = _invoke(create, {
            "item_id": "bad_room", "name": "Bad", "unit_cost": 1,
            "availability_rule": "FREQ=DAILY",
        })
        assert code == 400
        assert "availability_duration" in body["error"]


def test_default_availability_is_the_rrule():
    # no bookings → the whole requested range is free (daily rule coalesces to one span)
    with scratch_env():
        create, avail = load_tool("create_item"), load_tool("get_availability")
        _room(create)
        code, body = _invoke(avail, {"item_id": "1#room_101", "from": "2026-07-15", "to": "2026-07-18"})
        assert code == 200, body
        assert body["free"] == [{"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-18T00:00:00+00:00"}]
        assert body["free_seconds"] == 3 * 86400


def test_booking_carves_a_hole_off_grid():
    # THE case the interval store exists for: a 2pm→11am stay is not a slot boundary
    with scratch_env():
        create, avail, res = (load_tool(n) for n in ("create_item", "get_availability", "reserve"))
        _room(create)

        code, body = _invoke(res, {
            "item_id": "1#room_101", "start": "2026-07-16T14:00:00Z", "end": "2026-07-17T11:00:00Z",
            "source": "subject:john-smith",
        })
        assert code == 200, body

        # the same booking with the guest's NAME as the source is refused at the tool — the log is
        # append-only, so this is the one write there is no taking back
        code, refused = _invoke(res, {
            "item_id": "1#room_101", "start": "2026-07-19T14:00:00Z", "end": "2026-07-20T11:00:00Z",
            "source": "john smith",
        })
        assert code == 400 and "namespace" in refused["error"], refused

        code, body = _invoke(avail, {"item_id": "1#room_101", "from": "2026-07-15", "to": "2026-07-18"})
        assert code == 200, body
        assert body["free"] == [
            {"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-16T14:00:00+00:00"},
            {"from": "2026-07-17T11:00:00+00:00", "to": "2026-07-18T00:00:00+00:00"},
        ]


def test_double_book_rejected_and_adjacent_allowed():
    with scratch_env():
        create, res = load_tool("create_item"), load_tool("reserve")
        _room(create)
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-16T14:00:00Z", "end": "2026-07-17T11:00:00Z"})

        # overlaps the stay → 409
        code, body = _invoke(res, {"item_id": "1#room_101", "start": "2026-07-16T20:00:00Z", "end": "2026-07-17T08:00:00Z"})
        assert code == 409, body
        assert "already booked" in body["error"]

        # starts exactly at checkout → half-open, no clash
        code, body = _invoke(res, {"item_id": "1#room_101", "start": "2026-07-17T11:00:00Z", "end": "2026-07-18T00:00:00Z"})
        assert code == 200, body


def test_booking_outside_offered_availability_rejected():
    # the rule opens 7/15-7/18 only in the window asked of it; a range the rrule doesn't cover is a 409
    with scratch_env():
        create, res = load_tool("create_item"), load_tool("reserve")
        # a weekday-only resource: no Saturday availability
        _invoke(create, {
            "item_id": "1#bay_1", "name": "Service Bay", "unit_cost": 0, "unit_price": 90,
            "availability_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "availability_duration": 28800,  # 8h
        })
        # 2026-07-18 is a Saturday → not offered
        code, body = _invoke(res, {"item_id": "1#bay_1", "start": "2026-07-18T09:00:00Z", "end": "2026-07-18T12:00:00Z"})
        assert code == 409, body
        assert "outside" in body["error"]


def test_cancel_restores_availability():
    with scratch_env():
        create, avail, res = (load_tool(n) for n in ("create_item", "get_availability", "reserve"))
        _room(create)
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-16T14:00:00Z", "end": "2026-07-17T11:00:00Z"})

        code, body = _invoke(res, {
            "item_id": "1#room_101", "start": "2026-07-16T14:00:00Z", "end": "2026-07-17T11:00:00Z",
            "op": "cancel",
        })
        assert code == 200, body

        code, body = _invoke(avail, {"item_id": "1#room_101", "from": "2026-07-15", "to": "2026-07-18"})
        assert body["free"] == [{"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-18T00:00:00+00:00"}]


def test_cancel_without_booking_404():
    with scratch_env():
        create, res = load_tool("create_item"), load_tool("reserve")
        _room(create)
        code, body = _invoke(res, {
            "item_id": "1#room_101", "start": "2026-07-16T14:00:00Z", "end": "2026-07-17T11:00:00Z",
            "op": "cancel",
        })
        assert code == 404, body


def test_utilization_is_booked_over_offered():
    # occupancy folds out of the same log: 1 of 3 offered nights booked → 1/3
    with scratch_env():
        create, avail, res = (load_tool(n) for n in ("create_item", "get_availability", "reserve"))
        _room(create)
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-16T00:00:00Z", "end": "2026-07-17T00:00:00Z"})

        code, body = _invoke(avail, {"item_id": "1#room_101", "from": "2026-07-15", "to": "2026-07-18"})
        assert code == 200, body
        assert body["offered_seconds"] == 3 * 86400
        assert body["booked_seconds"] == 86400
        assert body["utilization"] == round(1 / 3, 4)


def test_cancel_by_source_releases_the_whole_job():
    # two stays booked under one job:<invoice_id> plus an unrelated booking; one cancel call
    # releases the job's stays and leaves the other guest's alone
    with scratch_env():
        create, avail, res = (load_tool(n) for n in ("create_item", "get_availability", "reserve"))
        _room(create)
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-15T14:00:00Z", "end": "2026-07-16T11:00:00Z",
                      "source": "job:inv_1"})
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-17T14:00:00Z", "end": "2026-07-18T11:00:00Z",
                      "source": "job:inv_1"})
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-16T14:00:00Z", "end": "2026-07-17T11:00:00Z",
                      "source": "subject:jane-doe"})

        code, body = _invoke(res, {"item_id": "1#room_101", "op": "cancel", "source": "job:inv_1"})
        assert code == 200, body
        assert len(body["cancelled"]) == 2

        # only the other guest's stay still consumes availability
        code, body = _invoke(avail, {"item_id": "1#room_101", "from": "2026-07-15", "to": "2026-07-19"})
        assert body["free"] == [
            {"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-16T14:00:00+00:00"},
            {"from": "2026-07-17T11:00:00+00:00", "to": "2026-07-19T00:00:00+00:00"},
        ]

        # nothing left standing under the source — the retry is a 404, not a double-release
        code, body = _invoke(res, {"item_id": "1#room_101", "op": "cancel", "source": "job:inv_1"})
        assert code == 404, body


def test_cancel_by_source_respects_a_partial_cancel():
    # an explicit interval-cancel under the same source shrinks what the by-source undo releases
    with scratch_env():
        create, res = load_tool("create_item"), load_tool("reserve")
        _room(create)
        _invoke(res, {"item_id": "1#room_101", "start": "2026-07-16T00:00:00Z", "end": "2026-07-18T00:00:00Z",
                      "source": "job:inv_2"})
        _invoke(res, {"item_id": "1#room_101", "op": "cancel", "source": "job:inv_2",
                      "start": "2026-07-16T00:00:00Z", "end": "2026-07-17T00:00:00Z"})

        code, body = _invoke(res, {"item_id": "1#room_101", "op": "cancel", "source": "job:inv_2"})
        assert code == 200, body
        assert body["cancelled"] == [{
            "start": "2026-07-17T00:00:00+00:00", "end": "2026-07-18T00:00:00+00:00",
            "movement_id": body["cancelled"][0]["movement_id"],
        }]


def test_stock_item_rejected_by_capacity_tools():
    # the two types are distinguished by the presence of an availability_rule
    with scratch_env():
        create, avail = load_tool("create_item"), load_tool("get_availability")
        _invoke(create, {"item_id": "1#milk", "name": "Milk", "unit_cost": 3.50})
        code, body = _invoke(avail, {"item_id": "1#milk", "from": "2026-07-15", "to": "2026-07-18"})
        assert code == 400, body
        assert "not a capacity item" in body["error"]


def test_reserve_is_idempotent_on_movement_id():
    with scratch_env():
        create, avail, res = (load_tool(n) for n in ("create_item", "get_availability", "reserve"))
        _room(create)
        booking = {
            "item_id": "1#room_101", "start": "2026-07-16T00:00:00Z", "end": "2026-07-17T00:00:00Z",
            "movement_id": "mv-fixed",
        }
        code, body = _invoke(res, booking)
        assert code == 200, body

        # the retry is a no-op, NOT a 409 — it must not be rejected as a clash with its own booking
        code, body = _invoke(res, booking)
        assert code == 200, body
        assert body["duplicate"] is True

        # and it did not stack a second -1; availability is the single carved hole
        code, body = _invoke(avail, {"item_id": "1#room_101", "from": "2026-07-15", "to": "2026-07-18"})
        assert body["free"] == [
            {"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-16T00:00:00+00:00"},
            {"from": "2026-07-17T00:00:00+00:00", "to": "2026-07-18T00:00:00+00:00"},
        ]


def test_an_unprefixed_id_resolves_and_keys_the_log_the_same_way():
    """`create_item` defaults `shift-ava` to the row `1#shift-ava`, so a read of the bare name must
    find it — and, critically, must book against the SAME key. Resolving the item but writing the
    movement under the raw string splits the meter: the item offers hours, the log shows nothing
    booked, and each half is reading a different key."""
    with scratch_env():
        create, reserve, avail = (load_tool(n) for n in ("create_item", "reserve", "get_availability"))
        _invoke(create, {"item_id": "shift-ava", "name": "ava shift", "unit": "shift", "unit_cost": 0,
                         "availability_rule": "FREQ=DAILY", "availability_duration": 86400})
        code, book = _invoke(reserve, {"item_id": "shift-ava", "source": "job:sched-1",
                                       "start": "2026-07-27T00:00:00Z", "end": "2026-07-28T00:00:00Z"})
        assert code == 200, book
        assert book["item_id"] == "1#shift-ava", "the canonical id, not the caller's string"

        code, av = _invoke(avail, {"item_id": "shift-ava",
                                   "from": "2026-07-27T00:00:00Z", "to": "2026-07-28T00:00:00Z"})
        assert code == 200, av
        assert av["booked_seconds"] > 0, "the booking must be visible to the same item's meter"


def _crew(create, item_id="1#shift-deo"):
    """A person's time as a capacity item: Sat-Wed, 7am local, 8-hour shifts."""
    code, body = _invoke(create, {
        "item_id": item_id, "name": "deo shift availability", "unit": "shift", "unit_cost": 0,
        "availability_rule": "FREQ=WEEKLY;BYDAY=SA,SU,MO,TU,WE;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
        "availability_duration": 8 * 3600,
    })
    assert code == 200, body
    return body


def test_a_fortnight_as_one_row_folds_like_fourteen():
    """The whole point. One rule-bearing booking must consume exactly what N separate bookings would,
    because a schedule IS a recurrence — expanding it into rows on the way in is what made a
    four-person fortnight 42 calls."""
    with scratch_env():
        create, reserve, avail = (load_tool(n) for n in ("create_item", "reserve", "get_availability"))
        _crew(create)
        window = {"from": "2026-07-25T00:00:00Z", "to": "2026-08-09T00:00:00Z"}

        code, offered = _invoke(avail, {"item_id": "1#shift-deo", **window})
        assert code == 200, offered
        assert offered["booked_seconds"] == 0

        code, book = _invoke(reserve, {
            "item_id": "1#shift-deo", "source": "job:sched-1",
            "start": "2026-07-25T07:00:00Z", "end": "2026-08-09T00:00:00Z",
            "rule": "FREQ=WEEKLY;BYDAY=SA,SU,MO,TU,WE;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
            "duration": 8 * 3600,
        })
        assert code == 200, book
        assert book["occurrences"] == 11, book          # Sat-Wed across the fortnight
        assert len(M_rows()) == 1, "one row, not ten"

        code, after = _invoke(avail, {"item_id": "1#shift-deo", **window})
        assert after["free"] == [], "every offered window is consumed"
        assert after["booked_seconds"] == offered["offered_seconds"]


def M_rows():
    """The raw movement log — proving row COUNT, which is the thing this change is about."""
    import os
    from helpers.localaws import rows
    return rows(os.environ["MOVEMENTS_TABLE"], "mv_sk")


def test_a_recurring_booking_is_refused_when_one_occurrence_clashes():
    """All-or-nothing, and the error has to name the day. A schedule that silently books 9 of 10
    days leaves a hole nobody is tracking."""
    with scratch_env():
        create, reserve = load_tool("create_item"), load_tool("reserve")
        _crew(create)
        # someone already holds the Tuesday
        code, held = _invoke(reserve, {"item_id": "1#shift-deo", "source": "job:other",
                                       "start": "2026-07-28T07:00:00Z", "end": "2026-07-28T15:00:00Z"})
        assert code == 200, held

        code, body = _invoke(reserve, {
            "item_id": "1#shift-deo", "source": "job:sched-1",
            "start": "2026-07-25T07:00:00Z", "end": "2026-08-09T00:00:00Z",
            "rule": "FREQ=WEEKLY;BYDAY=SA,SU,MO,TU,WE;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
            "duration": 8 * 3600,
        })
        assert code == 409, body
        assert "already booked" in body["error"] and "2026-07-28" in body["error"], body
        assert len(M_rows()) == 1, "the refused booking wrote nothing"


def test_a_recurring_booking_outside_the_offer_is_refused():
    """Deo does not work Thursdays. A rule that asks for them is not partially honoured."""
    with scratch_env():
        create, reserve = load_tool("create_item"), load_tool("reserve")
        _crew(create)
        code, body = _invoke(reserve, {
            "item_id": "1#shift-deo", "source": "job:sched-1",
            "start": "2026-07-30T07:00:00Z", "end": "2026-08-09T00:00:00Z",
            "rule": "FREQ=WEEKLY;BYDAY=TH;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
            "duration": 8 * 3600,
        })
        assert code == 409, body
        assert "outside the item" in body["error"], body


def test_cancel_by_source_releases_a_recurring_booking_with_one_row():
    """The mirror carries the rule, so undoing a fortnight costs one row rather than fourteen —
    otherwise the log grows a span-per-occurrence every time a schedule is redone."""
    with scratch_env():
        create, reserve, avail = (load_tool(n) for n in ("create_item", "reserve", "get_availability"))
        _crew(create)
        booking = {
            "item_id": "1#shift-deo", "source": "job:sched-1",
            "start": "2026-07-25T07:00:00Z", "end": "2026-08-09T00:00:00Z",
            "rule": "FREQ=WEEKLY;BYDAY=SA,SU,MO,TU,WE;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
            "duration": 8 * 3600,
        }
        code, made = _invoke(reserve, booking)
        assert code == 200, made

        code, body = _invoke(reserve, {"item_id": "1#shift-deo", "op": "cancel", "source": "job:sched-1"})
        assert code == 200, body
        assert len(body["cancelled"]) == 1, "one mirror, not one per occurrence"
        assert body["cancelled"][0]["rule"] == booking["rule"]
        assert len(M_rows()) == 2, "the booking and its mirror"

        window = {"from": "2026-07-25T00:00:00Z", "to": "2026-08-09T00:00:00Z"}
        code, after = _invoke(avail, {"item_id": "1#shift-deo", **window})
        assert after["booked_seconds"] == 0, "fully released"

        # re-booking after the release is allowed — the meter really is free again
        code, again = _invoke(reserve, {**booking, "source": "job:sched-2"})
        assert code == 200, again


def test_a_booking_rule_anchors_on_the_booking_not_the_query():
    """`from_rrule`'s dtstart. A reservation's occurrences are fixed when it is MADE; if the anchor
    came from the read window, the same booking would mean different days depending on when it was
    read back. INTERVAL=2 is where a floating anchor shows up — BYDAY-pinned rules hide it."""
    import datetime as _dt
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "inventory"))
    import availability as A

    rule = "FREQ=WEEKLY;INTERVAL=2;BYHOUR=9;BYMINUTE=0;BYSECOND=0"
    dur = _dt.timedelta(hours=8)
    anchor = _dt.datetime(2026, 7, 25, 9, tzinfo=_dt.timezone.utc)          # a Saturday
    late = _dt.datetime(2026, 8, 1, 0, tzinfo=_dt.timezone.utc)             # a week in

    pinned = A.from_rrule(rule, dur, (anchor, anchor + _dt.timedelta(days=28)), dtstart=anchor)
    read_later = A.from_rrule(rule, dur, (late, late + _dt.timedelta(days=28)), dtstart=anchor)
    floating = A.from_rrule(rule, dur, (late, late + _dt.timedelta(days=28)))

    def phase(iv):
        """Which 14-day slot each occurrence lands in, relative to the booking's own start."""
        return {(s.lower - anchor).days % 14 for s in iv}

    # anchored: on the booking's phase whichever window it is read over
    assert phase(pinned) == {0}, list(pinned)
    assert phase(read_later) == {0}, list(read_later)
    # unanchored: the phase came from the QUESTION — a week off, and it would move again tomorrow
    assert phase(floating) == {7}, list(floating)


def test_a_booking_recurrence_is_a_civil_claim():
    """A shift rule expands on the business's calendar, so 7am is 7am on both sides of a DST change —
    the instant moves, the wall clock does not. Same rule as the offer side."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "inventory"))
    import movements as M

    la = ZoneInfo("America/Los_Angeles")
    mv = {"when": {"kind": M.PERIOD, "rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
                   "duration": _dt.timedelta(hours=8),
                   "start": _dt.datetime(2026, 1, 10, 15, tzinfo=_dt.timezone.utc),
                   "end": _dt.datetime(2026, 7, 10, 0, tzinfo=_dt.timezone.utc)}}
    got = list(M.spans(mv, zone=la))
    winter = [s for s in got if s.lower.month == 1][0]
    summer = [s for s in got if s.lower.month == 7][0]
    assert winter.lower.astimezone(la).hour == 7, winter.lower.astimezone(la)
    assert summer.lower.astimezone(la).hour == 7, summer.lower.astimezone(la)
    assert winter.lower.hour == 15 and summer.lower.hour == 14, "PST vs PDT — the instant moved"


def test_windows_are_reported_in_the_businesss_offset():
    """`get_availability` expands the rule on the business's calendar, so it must REPORT on it too.
    Normalizing to Z makes every caller redo that conversion to answer "which day, what hour", and
    the agent has burned whole turns on it — once reading `07:00Z` as `11pm the previous day` and
    never recovering. Same instant either way; only the frame changes."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "clock"))
    import clock

    with scratch_env():
        # `GERP_TIMEZONE` is read at module IMPORT and the answer cached in `_resolved`, both of
        # which happen once per lambda cold start. Setting the env here would be too late, so set
        # what a cold start would have seen.
        prior = clock.GERP_TIMEZONE
        clock.GERP_TIMEZONE, clock._resolved = "America/Los_Angeles", None
        try:
            create, avail = load_tool("create_item"), load_tool("get_availability")
            _crew(create)
            code, body = _invoke(avail, {"item_id": "1#shift-deo",
                                         "from": "2026-07-25T00:00:00-07:00",
                                         "to": "2026-08-02T00:00:00-07:00"})
            assert code == 200, body
            first = body["free"][0]["from"]
            assert first.endswith("-07:00"), f"expected the business's offset, got {first}"
            assert first.startswith("2026-07-25T07:00"), f"7am local, got {first}"
        finally:
            clock.GERP_TIMEZONE, clock._resolved = prior, None   # don't leak into later tests


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
        print(f"ok {fn_name}")
    print("all capacity tests passed")
