"""The read: a query is a registry row run by name, its parameters bound by type.

The store is the Parquet the cabinet holds (seeded by `seed_store`, date-partitioned) and the
engine is duckdb locally, the same SQL Athena runs, with macros for the Trino functions the
canonical rows use. What is worth pinning: each canonical row answers from the rows a window
admits, on the firm's days; a name in neither the table nor the canonical file fails naming it;
a canonical row is in the table after first use; a saved row runs back; a parameter renders by
its declared type and a bad one is refused; the usage row names the query.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, query_rows, save_query, scratch_env, seed_store, usage_rows

SEP = "2026-09-"
ROWS = [
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


def _q(tool, name, **body):
    r = tool.handler({"body": json.dumps({"op": "query", "name": name, **body})}, None)
    return r["statusCode"], json.loads(r["body"])


def test_count_bins_on_the_firms_own_days_and_count_by_splits_on_a_property():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "count", params={"event": "member.checked_in"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "n": 1}, {"period": "2026-09-03", "n": 1},
                                {"period": "2026-09-08", "n": 1}, {"period": "2026-09-09", "n": 1},
                                {"period": "2026-09-15", "n": 1}], "06:30Z on the 2nd is the 1st in Los Angeles"
        assert body["window"]["start"] == "2026-09-01T07:00:00.000Z", "the window edge is the firm's midnight, in the store's format"

        code, body = _q(tool, "count_by", params={"event": "lead.captured", "grain": "month", "property": "plan"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "by_value": "annual", "n": 1},
                                {"period": "2026-09-01", "by_value": "monthly", "n": 2},
                                {"period": "2026-09-01", "by_value": None, "n": 1}]


def test_active_is_subjects_per_period():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "active", params={"event": "member.checked_in", "grain": "month"}, **SEPT)
        assert body["rows"] == [{"period": "2026-09-01", "subjects": 3}]
        code, body = _q(tool, "active", params={"event": "member.checked_in", "grain": "week"}, **SEPT)
        assert [r["subjects"] for r in body["rows"]] == [2, 2, 1], "weeks of 08-31, 09-07, 09-14"


def test_funnel_counts_a_subject_only_in_order():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "funnel_3", params={"e1": "lead.captured", "e2": "member.joined", "e3": "member.checked_in"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"step_1": 4, "step_2": 2, "step_3": 2}], "c_4 joined before its lead"


def test_retention_is_cohorts_by_first_period():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "retention", params={"event": "member.checked_in", "grain": "week"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [
            {"cohort": "2026-08-31", "offset_n": 0, "n": 2}, {"cohort": "2026-08-31", "offset_n": 1, "n": 1},
            {"cohort": "2026-08-31", "offset_n": 2, "n": 1}, {"cohort": "2026-09-07", "offset_n": 0, "n": 1},
        ]


def test_a_canonical_row_is_in_the_table_after_first_use_and_a_saved_row_runs_back():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        assert query_rows() == {}, "nothing seeded"
        code, body = _q(tool, "count", params={"event": "member.joined"}, **SEPT)
        assert code == 200
        rows = query_rows()
        assert set(rows) == {"count"} and rows["count"]["origin"]["S"] == "canonical" and rows["count"]["bucket"]["S"] == "athena"

        save_query("joined_by_plan", "SELECT element_at(properties, ?) AS plan, count(*) AS n FROM metrics WHERE event = ? GROUP BY 1 ORDER BY 1",
                   [{"name": "property", "type": "string"}, {"name": "event", "type": "string"}])
        code, body = _q(tool, "joined_by_plan", params={"property": "plan", "event": "member.joined"})
        assert code == 200, body
        assert body["rows"] == [{"plan": "annual", "n": 1}, {"plan": "monthly", "n": 1}, {"plan": None, "n": 1}]
        assert body["engine"] == "athena"

        used = usage_rows()
        assert sorted(u["name"] for u in used) == ["count", "joined_by_plan"] and all(int(u["bytes_scanned"]) > 0 for u in used)


def test_a_canonical_row_follows_the_file_and_a_saved_row_is_left_alone():
    """A canonical fix (the at_timezone one, 2026-09-19) reaches a gerp that already copied the row
    on its next call; the gerp's own rows are its own."""
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        save_query("count", "SELECT 1 AS stale", [], engine="athena")   # a copy from an older file
        from aws import resource
        t = resource("dynamodb").Table(__import__("os").environ["SCHEMA_TABLE"])
        t.update_item(Key={"registry": "metric_queries", "bucket_name": "athena#count"},
                      UpdateExpression="SET origin = :o, pinned = :p", ExpressionAttributeValues={":o": "canonical", ":p": True})
        code, body = _q(tool, "count", params={"event": "member.joined"}, **SEPT)
        assert code == 200 and body["rows"][0]["n"] == 1, "the file's SQL ran, not the stale copy"
        row = query_rows()["count"]
        assert "at_timezone" in row["schema"]["M"]["sql"]["S"] and row["pinned"]["BOOL"] is True, "refreshed, the pin kept"

        save_query("mine", "SELECT 2 AS mine", [])
        code, body = _q(tool, "mine")
        assert body["rows"] == [{"mine": 2}], "an extension row is never touched"


def test_a_pin_sets_the_flag_the_prompt_reads_and_a_canonical_name_pins_on_first_use():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        r = tool.handler({"body": json.dumps({"op": "pin", "name": "retention"})}, None)
        assert r["statusCode"] == 200 and json.loads(r["body"]) == {"name": "retention", "engine": "athena", "pinned": True}
        assert query_rows()["retention"]["pinned"]["BOOL"] is True, "copied in and pinned in one call"
        r = tool.handler({"body": json.dumps({"op": "pin", "name": "retention", "pinned": False})}, None)
        assert json.loads(r["body"])["pinned"] is False and query_rows()["retention"]["pinned"]["BOOL"] is False
        r = tool.handler({"body": json.dumps({"op": "pin", "name": "nope"})}, None)
        assert r["statusCode"] == 404
        r = tool.handler({"body": json.dumps({"op": "pin", "name": "retention", "pinned": "yes"})}, None)
        assert r["statusCode"] == 400 and "pinned: true or false" in json.loads(r["body"])["error"]


def test_a_missing_name_fails_naming_it_and_there_is_no_inline_sql():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "members_lost")
        assert code == 404 and "members_lost" in body["error"] and "write_schema" in body["error"]
        r = tool.handler({"body": json.dumps({"op": "query", "sql": "SELECT 1"})}, None)
        assert r["statusCode"] == 400 and "name" in json.loads(r["body"])["error"]


def test_a_parameter_renders_by_its_type_and_a_bad_one_is_refused():
    with scratch_env():
        seed_store([{"event": "note.left", "subject_id": "o'brien", "ts": f"{SEP}02T00:00:00.000Z", "properties": {"n": "1"}}])
        tool = load_lambda("manage_metrics")
        save_query("by_subject", "SELECT count(*) AS n FROM metrics WHERE subject_id = ? AND ts >= ? AND ts < ? AND CAST(element_at(properties, 'n') AS INTEGER) = ?",
                   [{"name": "subject", "type": "string"}, {"name": "start", "type": "timestamp"},
                    {"name": "end", "type": "timestamp"}, {"name": "n", "type": "number"}])
        code, body = _q(tool, "by_subject", params={"subject": "o'brien", "n": 1}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"n": 1}], "a quote in a string is escaped, a number is a number"
        code, body = _q(tool, "by_subject", params={"subject": "o'brien", "n": "one"}, **SEPT)
        assert code == 400 and "n: a number" in body["error"]
        code, body = _q(tool, "by_subject", params={"subject": "o'brien", "n": 1, "start": "2026-09-02T00:00:00.000Z", "end": "2026-09-02T00:00:01.000Z"})
        assert code == 200 and body["rows"] == [{"n": 1}], "a store-format timestamp is taken as it is; the event at 00:00:00.000Z is inside its own second"
        code, body = _q(tool, "by_subject", params={"subject": "o'brien", "n": 1, "start": "2026-09-02T00:00:00.001Z", "end": "2026-09-02T00:00:01.000Z"})
        assert code == 200 and body["rows"] == [{"n": 0}], "one millisecond later and the window's edge excludes it"


def test_a_bad_argument_names_itself():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        for kwargs, expected in (
            (dict(params={"event": "a.b", "grain": "hour"}), "grain:"),
            (dict(params={"event": "a.b"}, window="yesterday"), "window:"),
            (dict(params={"event": "a.b"}, start="2026-09-01"), "start and end"),
            (dict(params={"event": "a.b", "colour": "red"}), "params ['colour'] are not the query's"),
            (dict(params={}), "event is required"),
        ):
            code, body = _q(tool, "count", **kwargs)
            assert code == 400 and body["error"].startswith(expected), (kwargs, body)


MONEY = [
    {"event": "member.joined",    "subject_id": "m_1", "ts": "2026-08-15T15:00:00.000Z", "properties": {}},
    {"event": "member.joined",    "subject_id": "m_2", "ts": f"{SEP}08T15:00:00.000Z", "properties": {}},
    {"event": "member.cancelled", "subject_id": "m_1", "ts": f"{SEP}20T15:00:00.000Z", "properties": {}},
    {"event": "revenue.posted",   "subject_id": "je-1", "ts": f"{SEP}03T15:00:00.000Z", "properties": {"account": "SALES_REVENUE", "side": "CREDIT", "amount": "120.50"}},
    {"event": "revenue.posted",   "subject_id": "je-2", "ts": f"{SEP}10T15:00:00.000Z", "properties": {"account": "SALES_REVENUE", "side": "DEBIT", "amount": "-20.50"}},
    {"event": "expense.posted",   "subject_id": "je-3", "ts": f"{SEP}10T15:00:00.000Z", "properties": {"account": "RENT_EXPENSE", "side": "DEBIT", "amount": "40"}},
]


def test_sum_adds_the_amount_per_period_and_cumulative_is_the_stock_at_start_and_end():
    """The money measure and the stock: `sum` adds the signed `amount` per period; `cumulative`
    runs in minus out over the whole history to the window's end, `at_start` before the period and
    `at_end` after it, a balance when the measure is sum."""
    with scratch_env():
        seed_store(MONEY)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "sum", params={"event": "revenue.posted", "grain": "month"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "total": 100.0}], "signed amounts added"
        code, body = _q(tool, "cumulative", params={"in_event": "member.joined", "out_event": "member.cancelled", "measure": "count", "grain": "month"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "at_start": 1.0, "at_end": 1.0}], "August's member at the start; one joined, one left"
        code, body = _q(tool, "cumulative", params={"in_event": "revenue.posted", "out_event": "", "measure": "sum", "grain": "month"}, **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "at_start": 0.0, "at_end": 100.0}], "a balance: the posted sum to date"


if __name__ == "__main__":
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
