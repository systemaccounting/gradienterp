"""The public metric key (issue #46): one layout, built and read in modules/metrics/metrics.py alone."""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _p in ("metrics", "aws", "events", "rules", "clock"):
    sys.path.insert(0, str(REPO / "modules" / _p))
import metrics  # noqa: E402


def test_every_kind_and_grain_round_trips_and_a_value_with_the_delimiters_survives():
    cases = [
        ({"event": "account.signed_up", "kind": "count", "grain": "day"}, "2026-09-21"),
        ({"event": "session.started", "kind": "count_distinct", "grain": "week"}, "2026-W38"),
        ({"event": "revenue.posted", "kind": "sum", "grain": "month"}, "2026-09"),
        ({"event": "member.joined", "kind": "count", "grain": "month"}, "2026-09"),
        ({"event": "order.placed", "kind": "count_by", "property": "plan", "value": "a#b=c%d e", "grain": "day"}, "2026-09-21"),
        ({"event": "loaf.baked", "kind": "count_by", "property": "kind", "value": "", "grain": "day"}, "2026-09-21"),
    ]
    for d, p in cases:
        k = metrics.public_key(d, p)
        back = metrics.parse_public_key(k)
        assert back["event"] == d["event"] and back["kind"] == d["kind"] and back["grain"] == d["grain"] and back["period"] == p, k
        assert back["property"] == d.get("property") and back["value"] == d.get("value"), k
    assert metrics.public_key(cases[4][0], "2026-09-21") == "order.placed#count_by#plan=a%23b%3Dc%25d e#day#2026-09-21"


def test_two_firms_identical_definitions_share_a_key_and_a_changed_part_never_does():
    d = {"event": "account.signed_up", "kind": "count", "grain": "day"}
    assert metrics.public_key(dict(d), "2026-09-21") == metrics.public_key(dict(d), "2026-09-21")
    base = metrics.public_key(d, "2026-09-21")
    for change in ({"kind": "count_distinct"}, {"grain": "month"}, {"event": "account.closed"}):
        assert metrics.public_key({**d, **change}, "2026-09-21" if change.get("grain") != "month" else "2026-09") != base
    a = metrics.public_key({"event": "order.placed", "kind": "count_by", "property": "plan", "value": "pro", "grain": "day"}, "2026-09-21")
    b = metrics.public_key({"event": "order.placed", "kind": "count_by", "property": "plan", "value": "team", "grain": "day"}, "2026-09-21")
    assert a != b and metrics.parse_public_key(a)["value"] == "pro"


def test_points_of_one_key_sort_in_time_by_the_range_key_alone():
    d = {"event": "account.signed_up", "kind": "count", "grain": "day"}
    keys = [metrics.public_key(d, p) for p in ("2026-09-03", "2026-09-21", "2026-10-01")]
    assert sorted(keys, reverse=True) == list(reversed(keys))
    prefix = metrics.public_key(d, "x").rsplit("#", 1)[0] + "#"
    assert all(k.startswith(prefix) for k in keys), "one begins_with reads a key's points"


def test_a_bad_definition_is_refused_and_nothing_else_splits_the_key():
    for bad in ({"event": "Signup#x", "kind": "count", "grain": "day"}, {"event": "a.b", "kind": "avg", "grain": "day"},
                {"event": "a.b", "kind": "count", "grain": "hour"}, {"event": "a.b", "kind": "count_by", "property": "p#q", "value": "v", "grain": "day"}):
        try:
            metrics.public_key(bad, "2026-09-21")
            raise AssertionError(bad)
        except metrics.Invalid:
            pass
    for bad_key in ("revenue#2026-09", "a.b#count#day", "a.b#count_by#day#2026-09-21", "a.b#avg#day#2026-09-21"):
        try:
            metrics.parse_public_key(bad_key)
            raise AssertionError(bad_key)
        except metrics.Invalid:
            pass
    # the api and the lambdas read the key through parse_public_key; no other splitter on a counters key
    roots = [REPO / "prod" / "api_openlyoperated", REPO / "modules" / "metrics" / "lambdas"]
    for root in roots:
        for p in root.rglob("*.py"):
            src = p.read_text()
            for m in re.finditer(r"\.r?split\(\"#\"", src):
                line = src[:m.start()].count("\n") + 1
                assert False, f"{p.relative_to(REPO)}:{line} splits a key by hand"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all public key tests passed")
