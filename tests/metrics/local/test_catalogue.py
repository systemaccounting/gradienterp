"""The catalogue (issue #54): a definition is a `metric_definitions` registry row in MetricFlow's
words, run by name through `manage_metrics op=query` as its legs' rows joined per period; a leg over
an event the firm never recorded contributes nothing; a name in neither registry is the error the
agent acts on; a pin sets the flag on the definition's row, copied in from canonical on first use."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, seed_store  # noqa: E402

SEP = "2026-09-"
SEPT = {"start": "2026-09-01", "end": "2026-10-01"}
ROWS = [
    {"event": "member.joined",     "subject_id": "m_1", "ts": "2026-08-15T15:00:00.000Z", "properties": {}},
    {"event": "member.joined",     "subject_id": "m_2", "ts": f"{SEP}08T15:00:00.000Z", "properties": {}},
    {"event": "member.cancelled",  "subject_id": "m_1", "ts": f"{SEP}20T15:00:00.000Z", "properties": {}},
    {"event": "member.checked_in", "subject_id": "m_1", "ts": f"{SEP}02T18:00:00.000Z", "properties": {}},
    {"event": "member.checked_in", "subject_id": "m_2", "ts": f"{SEP}09T18:00:00.000Z", "properties": {}},
    {"event": "member.checked_in", "subject_id": "m_2", "ts": f"{SEP}16T18:00:00.000Z", "properties": {}},
    {"event": "revenue.posted",    "subject_id": "je-1", "ts": f"{SEP}03T15:00:00.000Z", "properties": {"account": "SALES_REVENUE", "side": "CREDIT", "amount": "100"}},
    {"event": "expense.posted",    "subject_id": "je-2", "ts": f"{SEP}10T15:00:00.000Z", "properties": {"account": "RENT_EXPENSE", "side": "DEBIT", "amount": "40"}},
]


def _q(tool, name, **body):
    r = tool.handler({"body": json.dumps({"op": "query", "name": name, **body})}, None)
    return r["statusCode"], json.loads(r["body"])


def _definition_rows():
    from boto3.dynamodb.conditions import Key
    from aws import resource
    items = resource("dynamodb").Table(os.environ["SCHEMA_TABLE"]).query(KeyConditionExpression=Key("registry").eq("metric_definitions"))["Items"]
    return {i["name"]: i for i in items}


def test_a_definition_runs_as_its_legs_joined_per_period():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "membership.churn", **SEPT)
        assert code == 200, body
        assert body["type"] == "ratio" and body["unit"] == "ratio" and body["grain"] == "month" and body["bucket"] == "membership"
        assert body["columns"] == ["period", "numerator", "denominator", "ratio"]
        assert body["rows"] == [{"period": "2026-09-01", "numerator": 1.0, "denominator": 1.0, "ratio": 1.0}], \
            "one cancelled over the one member there at the start of September"
        code, body = _q(tool, "membership.margin", **SEPT)
        assert body["rows"] == [{"period": "2026-09-01", "numerator": 60.0, "denominator": 100.0, "ratio": 0.6}], "revenue minus expense, over revenue: signed legs"
        code, body = _q(tool, "membership.engagement", params={"grain": "week"}, **SEPT)
        assert code == 200 and body["grain"] == "week" and [r["period"] for r in body["rows"]][:2] == ["2026-08-31", "2026-09-07"]
        assert body["rows"][1] == {"period": "2026-09-07", "numerator": 1.0, "denominator": 2.0, "ratio": 0.5}, "m_2 checked in; two members at the end of that week"
        assert _definition_rows()["churn"]["origin"] == "canonical", "copied into the table on first use"


def test_an_unrecorded_leg_contributes_nothing_and_an_unknown_name_is_the_error():
    with scratch_env():
        seed_store(ROWS)
        tool = load_lambda("manage_metrics")
        code, body = _q(tool, "membership.conversion", **SEPT)
        assert code == 200, body
        assert body["rows"] == [{"period": "2026-09-01", "numerator": 1.0, "denominator": 0.0, "ratio": None}], "no lead was ever captured: the denominator is zero and the period has no ratio"
        code, body = _q(tool, "nope", **SEPT)
        assert code != 200 and "nope" in json.dumps(body)
        code, body = _q(tool, "churn", **SEPT)
        assert code != 200 and "commerce, membership, saas" in json.dumps(body), "a bare name in three buckets is refused naming them"
        code, body = _q(tool, "membership.churn", params={"plan": "x"}, **SEPT)
        assert code != 200 and "grain" in json.dumps(body), "a definition takes grain alone"


def test_a_pin_on_a_definition_sets_the_flag_on_its_row():
    with scratch_env():
        tool = load_lambda("manage_metrics")
        r = tool.handler({"body": json.dumps({"op": "pin", "name": "saas.unit_cost"})}, None)
        assert r["statusCode"] == 200, r["body"]
        row = _definition_rows()["unit_cost"]
        assert row["pinned"] is True and row["bucket"] == "saas" and row["origin"] == "canonical"
        r = tool.handler({"body": json.dumps({"op": "pin", "name": "saas.unit_cost", "pinned": False})}, None)
        assert _definition_rows()["unit_cost"]["pinned"] is False


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all catalogue tests passed")
