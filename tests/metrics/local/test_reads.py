"""The reads: count, distinct, funnel, retention and SQL over the store, on the firm's calendar.

The store is the Parquet the cabinet holds (seeded by `seed_store`, date-partitioned) and the
engine is duckdb locally, the same SQL Athena runs. What is worth pinning: each read answers from
the rows a window admits; bins fall on the firm's own days (a 6am UTC event is the day before in
Los Angeles); a funnel counts a subject only in order; retention folds cohorts by first period;
every read leaves a usage row naming the payer and the bytes; a bad argument names itself.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, seed_store, usage_rows

SEP = "2026-09-"
ROWS = [
    # c_1 joins in week 1 and checks in every week; c_2 joins week 2 and checks in once; c_3 leads only
    {"event": "lead.captured",     "subject_id": "c_1", "ts": f"{SEP}01T15:00:00.000Z", "properties": {"plan": "monthly"}},
    {"event": "member.joined",     "subject_id": "c_1", "ts": f"{SEP}01T16:00:00.000Z", "properties": {"plan": "monthly"}},
    {"event": "member.checked_in", "subject_id": "c_1", "ts": f"{SEP}02T06:30:00.000Z", "properties": {"location": "pier"}},   # 09-01 in LA
    {"event": "member.checked_in", "subject_id": "c_1", "ts": f"{SEP}08T18:00:00.000Z", "properties": {"location": "pier"}},
    {"event": "member.checked_in", "subject_id": "c_1", "ts": f"{SEP}15T18:00:00.000Z", "properties": {"location": "main"}},
    {"event": "lead.captured",     "subject_id": "c_2", "ts": f"{SEP}07T15:00:00.000Z", "properties": {"plan": "annual"}},
    {"event": "member.joined",     "subject_id": "c_2", "ts": f"{SEP}08T15:00:00.000Z", "properties": {"plan": "annual"}},
    {"event": "member.checked_in", "subject_id": "c_2", "ts": f"{SEP}09T18:00:00.000Z", "properties": {"location": "main"}},
    {"event": "lead.captured",     "subject_id": "c_3", "ts": f"{SEP}10T15:00:00.000Z", "properties": {"plan": "monthly"}},
    # c_4 checks in before joining: out of order, so the funnel must not count the join step
    {"event": "member.checked_in", "subject_id": "c_4", "ts": f"{SEP}03T18:00:00.000Z"},
    {"event": "member.joined",     "subject_id": "c_4", "ts": f"{SEP}04T18:00:00.000Z"},
    {"event": "lead.captured",     "subject_id": "c_4", "ts": f"{SEP}05T18:00:00.000Z"},
]
SEPT = {"start": "2026-09-01", "end": "2026-10-01"}


def _call(tool, op, **body):
    r = tool.handler({"body": json.dumps({"op": op, **body})}, None)
    return r["statusCode"], json.loads(r["body"])


def test_count_bins_on_the_firms_own_days_and_groups_by_a_property():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _call(tool, "count", event="member.checked_in", grain="day", **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "n": 1}, {"period": "2026-09-03", "n": 1},
                                {"period": "2026-09-08", "n": 1}, {"period": "2026-09-09", "n": 1},
                                {"period": "2026-09-15", "n": 1}], "06:30Z on the 2nd is the 1st in Los Angeles"
        assert body["window"]["start"] == "2026-09-01T07:00:00.000Z", "the window edge is the firm's midnight"

        code, body = _call(tool, "count", event="lead.captured", grain="month", by="plan", **SEPT)
        assert body["rows"] == [{"period": "2026-09-01", "by_value": "annual", "n": 1},
                                {"period": "2026-09-01", "by_value": "monthly", "n": 2},
                                {"period": "2026-09-01", "by_value": None, "n": 1}]


def test_distinct_is_subjects_per_period():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _call(tool, "distinct", event="member.checked_in", grain="month", **SEPT)
        assert body["rows"] == [{"period": "2026-09-01", "n": 3}]
        code, body = _call(tool, "distinct", event="member.checked_in", grain="week", **SEPT)
        assert [r["n"] for r in body["rows"]] == [2, 2, 1], "weeks of 08-31, 09-07, 09-14"


def test_funnel_counts_a_subject_only_in_order():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _call(tool, "funnel", events=["lead.captured", "member.joined", "member.checked_in"], **SEPT)
        assert code == 200, body
        assert body["steps"] == [
            {"event": "lead.captured", "subjects": 4, "rate": 1.0},
            {"event": "member.joined", "subjects": 2, "rate": 0.5},      # c_4 joined before its lead
            {"event": "member.checked_in", "subjects": 2, "rate": 0.5},
        ]


def test_retention_folds_cohorts_by_first_period():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _call(tool, "retention", event="member.checked_in", grain="week", **SEPT)
        assert code == 200, body
        assert body["cohorts"] == [
            {"cohort": "2026-08-31", "size": 2, "periods": [2, 1, 1]},   # c_1 and c_4; c_1 returns twice
            {"cohort": "2026-09-07", "size": 1, "periods": [1]},         # c_2
        ]


def test_sql_is_the_general_read_and_every_read_leaves_a_usage_row():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _call(tool, "query", sql="SELECT event, count(*) AS n FROM metrics GROUP BY 1 ORDER BY 1")
        assert code == 200, body
        assert body["rows"] == [{"event": "lead.captured", "n": 4}, {"event": "member.checked_in", "n": 5},
                                {"event": "member.joined", "n": 3}]
        assert body["bytes_scanned"] > 0
        code, body = _call(tool, "query", sql="SELECT nope FROM metrics")
        assert code == 422 and "nope" in body["error"]

        rows = usage_rows()
        assert len(rows) == 1, "the failed query left no row"
        assert rows[0]["payer"] == "gerp" and rows[0]["op"] == "query" and int(rows[0]["bytes_scanned"]) > 0


def test_an_empty_store_answers_empty_and_a_bad_argument_names_itself():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        code, body = _call(tool, "count", event="member.checked_in", window="this_month")
        assert code == 200 and body["rows"] == []
        for kwargs, expected in (
            ({"event": "Bad Name"}, "event:"),
            ({"event": "a.b", "grain": "hour"}, "grain:"),
            ({"event": "a.b", "window": "yesterday"}, "window:"),
            ({"event": "a.b", "start": "2026-09-01"}, "start and end"),
            ({"event": "a.b", "by": "Plan-Type"}, "by:"),
        ):
            code, body = _call(tool, "count", **kwargs)
            assert code == 400 and body["error"].startswith(expected), (kwargs, body)
        code, body = _call(tool, "funnel", events=["a.b"])
        assert code == 400 and body["error"].startswith("events:")


if __name__ == "__main__":
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
