"""Unit tests for modules/inventory/movements — the movement log (the store).

on-hand (stock) and availability (capacity) are folds over an append-only log, never stored
scalars. Exercises the log end-to-end in local jsonl mode: record ± movements → read back → fold.
Pure fold + `portion` interval math; run under the repo .venv (needs `portion`)."""

import datetime as dt
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "inventory"))

# point the store at a scratch table before importing (the module reads env at import)
sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["MOVEMENTS_TABLE"] = make_table("inventory-movements")

import availability as A  # noqa: E402
import movements as M  # noqa: E402


def d(day, h=0):
    # aware UTC — the store's ingest domain; a `default` handed to the fold lives here too
    return dt.datetime(2026, 7, day, h, tzinfo=dt.timezone.utc)


def spans(interval):
    return [(a.lower, a.upper) for a in interval]


def fresh():
    os.environ["MOVEMENTS_TABLE"] = make_table("inventory-movements")


def test_stock_on_hand_is_the_fold():
    # the spec's minibar_coke row: +6 restock, −2 room_102 purchase, −1 room_103 shrink → 3 on hand
    fresh()
    M.record_point("minibar_coke", 6, "restock", d(15, 9))
    M.record_point("minibar_coke", -2, "room_102 purchase", d(15, 21))
    M.record_point("minibar_coke", -1, "room_103 shrink", d(16, 2))
    assert M.item_on_hand("minibar_coke") == 3


def test_on_hand_as_of_a_time():
    # on-hand(t) sums only movements at/before t — the level timeline (turnover / reorder read)
    fresh()
    M.record_point("widget", 10, "restock", d(15))
    M.record_point("widget", -4, "sale", d(17))
    assert M.item_on_hand("widget", at=d(16)) == 10   # before the sale
    assert M.item_on_hand("widget", at=d(18)) == 6    # after it
    assert M.item_on_hand("widget") == 6              # whole log


def test_capacity_availability_is_default_minus_booked():
    # room open 7/15–7/18; book the middle night → availability carves a hole
    fresh()
    default = A.span(d(15), d(18))
    M.record_period("room_101", -1, "reservation", d(16), d(17))
    assert spans(M.item_available("room_101", default)) == [(d(15), d(16)), (d(17), d(18))]


def test_cancel_restores_availability():
    # a −1 booking then a +1 cancel over the same interval nets to free again (booked − cancelled)
    fresh()
    default = A.span(d(15), d(18))
    M.record_period("room_104", -1, "reservation", d(15), d(16))
    assert spans(M.item_available("room_104", default)) == [(d(16), d(18))]
    M.record_period("room_104", 1, "reservation cancel", d(15), d(16))
    assert spans(M.item_available("room_104", default)) == [(d(15), d(18))]


def test_point_and_period_items_dont_interfere():
    # one log holds both kinds; read() is per-item, folds see only that item's rows
    fresh()
    M.record_point("minibar_coke", 6, "restock", d(15, 9))
    M.record_period("room_101", -1, "reservation", d(15), d(18))
    assert M.item_on_hand("minibar_coke") == 6
    assert M.consumed(M.read("minibar_coke")) == A.intervals([])   # no periods for coke
    assert spans(M.item_available("room_101", A.span(d(15), d(18)))) == []  # fully booked


def test_append_is_idempotent_on_movement_id():
    # a retried record with the same movement_id must not double-count
    fresh()
    M.record_point("bolt", 5, "restock", d(15), movement_id="mv-1")
    M.record_point("bolt", 5, "restock", d(15), movement_id="mv-1")
    assert M.item_on_hand("bolt") == 5


def test_row_round_trips():
    # to_row → from_row preserves the movement; bounds come back aware-UTC
    mv = M.period("room_9", -1, "reservation", d(15), d(18))
    back = M.from_row(M.to_row(mv, movement_id="x"))
    assert back["item"] == "room_9" and back["delta"] == -1
    assert back["when"]["start"] == M._utc(d(15)) and back["when"]["end"] == M._utc(d(18))


def test_a_source_must_be_a_reference_not_free_text():
    """`source` is the one field where "publish it and fix it later" has no later: the log is
    append-only, so a name typed here is published permanently. Namespacing it makes the field a
    reference to another object, which resolves — and resolves to nothing for a private person."""
    assert M.check_source("job:inv_1") is None
    assert M.check_source("subject:ava-reyes") is None, "whose shift it is, as a reference"
    assert M.check_source("po:f55b1f65") is None
    assert M.check_source("produce:1#cold-brew") is None

    assert M.check_source("john smith"), "a name is exactly what must not land in the log"
    assert M.check_source("sched-1"), "an unnamespaced key is still free text"
    assert M.check_source("worker:ava"), "an unknown namespace is not a licence"
    assert M.check_source("")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    fresh()
    print("all movement tests passed")
