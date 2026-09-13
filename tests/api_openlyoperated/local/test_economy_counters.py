"""GET /economy/counters — the counters in the metric shape, margin derived, the catalog form."""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules"))
from helpers.localaws import unique  # noqa: E402

os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _counters_table():
    """The operator's counters table — not in the per-customer schema snapshot, so made here: one
    hash key, `counter`, `<key>#<YYYY-MM>`."""
    from aws import client
    name = unique("counters")
    client("dynamodb").create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                                    AttributeDefinitions=[{"AttributeName": "counter", "AttributeType": "S"}],
                                    KeySchema=[{"AttributeName": "counter", "KeyType": "HASH"}])
    return name


def _load(rows):
    os.environ["COUNTERS_TABLE"] = _counters_table()
    from aws import client
    for pk, v in rows.items():
        client("dynamodb").put_item(TableName=os.environ["COUNTERS_TABLE"], Item={"counter": {"S": pk}, "value": {"N": str(v)}})
    spec = importlib.util.spec_from_file_location("api_counters", REPO / "prod/api_openlyoperated/api/v1/economy_counters/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get(mod, **q):
    out = mod.handler({"queryStringParameters": q or None}, None)
    return out["statusCode"], json.loads(out["body"])


def test_a_signal_answers_in_the_metric_shape_and_margin_is_derived():
    mod = _load({"revenue#2026-07": 1000, "revenue#2026-08": 2000, "revenue#2026-09": 500,
                 "expense#2026-07": 800, "expense#2026-08": 1500, "other#2026-08": 1})
    code, m = _get(mod, signal="revenue")
    assert code == 200
    assert set(m) == {"key", "label", "unit", "grain", "headline", "points", "definition", "source"}
    assert m["unit"] == "USD" and m["grain"] == "month" and m["definition"]
    assert m["points"] == [{"period": "2026-07", "value": 1000.0}, {"period": "2026-08", "value": 2000.0}, {"period": "2026-09", "value": 500.0}]
    assert m["headline"] == {"period": "2026-09", "value": 500.0}
    assert m["source"] == {"curl": "curl https://api.openlyoperated.biz/v1/economy/counters?signal=revenue"}
    code, mg = _get(mod, signal="margin")
    assert mg["unit"] == "ratio"
    assert mg["points"] == [{"period": "2026-07", "value": 0.2}, {"period": "2026-08", "value": 0.25}, {"period": "2026-09", "value": 1.0}]
    code, w = _get(mod, signal="expense", **{"from": "2026-08", "to": "2026-08"})
    assert w["points"] == [{"period": "2026-08", "value": 1500.0}]


def test_the_catalog_form_names_every_signal_and_an_unknown_one_is_404():
    mod = _load({"revenue#2026-08": 1})
    code, cat = _get(mod)
    assert code == 200
    assert [s["key"] for s in cat["signals"]] == ["revenue", "expense", "margin"]
    rev = cat["signals"][0]
    assert rev["first"] == "2026-08" and rev["last"] == "2026-08" and rev["unit"] == "USD"
    assert cat["signals"][1]["first"] is None, "no expense counted yet"
    assert _get(mod, signal="gerps")[0] == 404
    # no rows: the headline names the current month with no value, so a reader sees which period is empty
    from datetime import datetime, timezone
    assert _get(mod, signal="expense")[1]["headline"] == {"period": datetime.now(timezone.utc).strftime("%Y-%m"), "value": None}


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all economy counters tests passed")
