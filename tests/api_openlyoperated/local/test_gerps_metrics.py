"""GET /gerps/{gerp_id}/metrics[/{slug}]: a firm's product record off the platform's counters, in the
metric shape the site draws; the catalog is the partition; nothing asked of the gerp, no query run."""
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "gradienterp_cloud"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules" / "metrics"))
from _helpers import scratch_env  # noqa: E402
from helpers.localaws import unique  # noqa: E402


def _seed(published=True):
    from aws import client
    ddb = client("dynamodb")
    os.environ["COUNTERS_TABLE"] = unique("counters")
    ddb.create_table(TableName=os.environ["COUNTERS_TABLE"], BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "gerp_id", "AttributeType": "S"}, {"AttributeName": "key", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "gerp_id", "KeyType": "HASH"}, {"AttributeName": "key", "KeyType": "RANGE"}])
    ddb.put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item={"gerp_id": {"S": "cafe"}, "published": {"BOOL": published}})
    for key, val in (("account.signed_up#count#day#2026-09-20", {"value": {"N": "3"}}),
                     ("account.signed_up#count#day#2026-09-21", {"value": {"N": "5"}}),
                     ("account.signed_up#count#month#2026-09", {"value": {"N": "8"}}),
                     ("account.signed_up#active#day#2026-09-21", {"members": {"SS": ["ada", "bo"]}}),
                     ("loaf.baked#count#day#2026-09-21", {"value": {"N": "40"}}),
                     ("revenue#2026-09", {"value": {"N": "9"}})):
        ddb.put_item(TableName=os.environ["COUNTERS_TABLE"], Item={"gerp_id": {"S": "cafe"}, "key": {"S": key}, **val})
    ddb.put_item(TableName=os.environ["COUNTERS_TABLE"], Item={"gerp_id": {"S": "platform"}, "key": {"S": "revenue#2026-09"}, "value": {"N": "99"}})


def _load():
    os.environ["METRIC_EVENTS_FILE"] = str(REPO / "modules" / "schemas" / "data" / "metric_events.json")
    spec = importlib.util.spec_from_file_location("api_gerps_metrics", REPO / "prod/api_openlyoperated/api/v1/gerps_metrics/handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get(mod, gerp, slug=None, grain=None):
    r = mod.handler({"pathParameters": {"gerp_id": gerp, **({"slug": slug} if slug else {})},
                     "queryStringParameters": ({"grain": grain} if grain else None)}, None)
    return r["statusCode"], json.loads(r["body"])


def test_the_partition_is_the_catalog_and_every_metric_answers_in_the_shape():
    with scratch_env():
        _seed()
        mod = _load()
        code, out = _get(mod, "cafe")
        assert code == 200 and out["grain"] == "day"
        by = {m["key"]: m for m in out["metrics"]}
        assert sorted(by) == ["account.signed_up.active", "account.signed_up.count", "loaf.baked.count"], "a card per event and kind, off the keys alone"
        signups = by["account.signed_up.count"]
        assert [(p["period"], p["value"]) for p in signups["points"]] == [("2026-09-20", 3), ("2026-09-21", 5)]
        assert signups["headline"] == {"period": "2026-09-21", "value": 5} and signups["class"] == "saas"
        assert signups["definition"].startswith("a login was created"), "the vocabulary's words"
        assert signups["source"]["curl"].endswith("/gerps/cafe/metrics/account.signed_up.count?grain=day")
        assert by["account.signed_up.active"]["points"] == [{"period": "2026-09-21", "value": 2}], "a set's size"
        assert by["loaf.baked.count"]["class"] == "" and "own event" in by["loaf.baked.count"]["definition"], "a firm's own name, no bucket"
        assert "ada" not in json.dumps(out) and "revenue" not in json.dumps(out), "no member, and nothing outside the layout"
        code, month = _get(mod, "cafe", "account.signed_up.count", grain="month")
        assert code == 200 and month["points"] == [{"period": "2026-09", "value": 8}]
        assert _get(mod, "cafe", grain="hour")[0] == 400


def test_an_unpublished_gerp_or_an_unknown_metric_is_404():
    with scratch_env():
        _seed(published=False)
        mod = _load()
        assert _get(mod, "cafe")[0] == 404
        assert _get(mod, "nobody")[0] == 404
    with scratch_env():
        _seed()
        mod = _load()
        assert _get(mod, "cafe", "loaf.baked.active")[0] == 404


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all gerps metrics tests passed")
