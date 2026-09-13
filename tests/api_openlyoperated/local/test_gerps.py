"""GET /gerps — the directory off the operator's rows: published, active gerps with the profile's
name and place and each gerp's sources; the source asked when the row carries no `published`."""

import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "gradienterp_cloud"))
from _helpers import scratch_env  # noqa: E402  — the operator tables, fresh per case


def _load():
    path = REPO / "prod" / "api_openlyoperated" / "api" / "v1" / "gerps" / "handler.py"
    spec = importlib.util.spec_from_file_location("api_gerps", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(gerp_id, status="active", gateway="https://gw.example", label="", published=None):
    from aws import client
    item = {"gerp_id": {"S": gerp_id}, "status": {"S": status}, "label": {"S": label or gerp_id}}
    if gateway:
        item["gateway_url"] = {"S": gateway}
    if published is not None:
        item["published"] = {"BOOL": published}
    client("dynamodb").put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item=item)


def _profile(gerp_id, label, city, state, naics=()):
    from aws import client
    client("dynamodb").put_item(TableName=os.environ["PROFILES_TABLE"], Item={
        "gerp_profile_id": {"S": gerp_id}, "kind": {"S": "business"}, "label": {"S": label},
        "city": {"S": city}, "state": {"S": state}, "naics_intended": {"L": [{"S": n} for n in naics]}})


def _with_catalogs(mod, catalogs):
    """The gerps' own /oob catalogs: a dict of gateway url → sources list, None for a 404."""
    def fake(url, timeout=0):
        base = url[: -len("/oob")]
        if base not in catalogs or catalogs[base] is None:
            raise urllib.error.HTTPError(url, 404, "not published", {}, None)
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"sources": catalogs[base]}).encode()
        return R()
    mod.urllib.request.urlopen = fake


def test_the_directory_lists_published_active_gerps_with_their_sources():
    with scratch_env():
        mod = _load()
        _row("cafe", published=True, label="Cafe Inc")
        _profile("cafe", "Coffee by Cafe", "Oakland", "CA", ["722515"])
        _row("shut", published=False, label="Shut Co")
        _row("old", status="closed", label="Old Co")
        _row("building", gateway="", label="Building Co")
        _with_catalogs(mod, {"https://gw.example": [{"kind": "ledger", "label": "financials", "path": "/oob/financials"},
                                                     {"kind": "metrics", "label": "metrics", "path": "/oob/metrics"}]})
        out = json.loads(mod.handler({"queryStringParameters": None}, None)["body"])
        assert [g["gerp_id"] for g in out["gerps"]] == ["cafe"]
        g = out["gerps"][0]
        assert g["label"] == "Coffee by Cafe" and g["city"] == "Oakland" and g["naics_intended"] == ["722515"]
        assert g["sources"] == [{"key": "financials", "kind": "ledger", "label": "financials"},
                                {"key": "metrics", "kind": "metrics", "label": "metrics"}]
        assert g["api"] == "/v1/gerps/cafe" and "gateway_url" not in json.dumps(out)
        # the filters
        assert json.loads(mod.handler({"queryStringParameters": {"q": "cof"}}, None)["body"])["gerps"]
        assert not json.loads(mod.handler({"queryStringParameters": {"q": "tea"}}, None)["body"])["gerps"]
        assert json.loads(mod.handler({"queryStringParameters": {"sector": "722515", "state": "ca"}}, None)["body"])["gerps"]
        assert not json.loads(mod.handler({"queryStringParameters": {"city": "Denver"}}, None)["body"])["gerps"]


def test_a_row_without_the_published_stamp_is_asked_at_the_source():
    with scratch_env():
        mod = _load()
        _row("yes", gateway="https://yes.example", label="Yes Co")     # no stamp, catalog answers
        _row("no", gateway="https://no.example", label="No Co")        # no stamp, catalog is 404
        _row("mute", gateway="https://mute.example", label="Mute Co")  # no stamp, no answer
        _with_catalogs(mod, {"https://yes.example": [{"kind": "ledger", "label": "financials", "path": "/oob/financials"}],
                             "https://no.example": None})
        out = json.loads(mod.handler({}, None)["body"])
        assert [g["gerp_id"] for g in out["gerps"]] == ["yes"]
        assert out["gerps"][0]["label"] == "Yes Co", "no profile row: the instance label stands in"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all gerps tests passed")
