"""Integration test — the read api, against DEPLOYED infra.

The directory lists gradienterp with its sources; the read-through serves its catalog and its
financials from its own account (net = revenue − expenses); the economy's counters answer in the
metric shape; every answer lets a browser on any origin read it; the stream door refuses an
anonymous reader. Read-only — no teardown. Needs no creds: the api is public.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _helpers as H  # noqa: E402


def _get(path, origin=None):
    req = urllib.request.Request(H.API_URL + path, headers={"Origin": origin} if origin else {})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()), r


def test_directory_lists_gradienterp_with_its_sources():
    r, _ = _get("/gerps?q=grad")
    gerps = {g["gerp_id"]: g for g in r["gerps"]}
    assert "gradienterp" in gerps
    assert {"financials", "metrics"} <= {s["key"] for s in gerps["gradienterp"]["sources"]}
    assert gerps["gradienterp"]["api"] == "/v1/gerps/gradienterp"


def test_read_through_serves_the_catalog_and_the_financials():
    cat, _ = _get("/gerps/gradienterp/sources")
    assert "financials" in {s["key"] for s in cat["sources"]}
    fin, _ = _get("/gerps/gradienterp/sources/financials")
    assert {"revenue", "expenses", "net_income", "period"} <= set(fin)
    assert abs(fin["net_income"] - (fin["revenue"] - fin["expenses"])) < 0.01
    metrics, _ = _get("/gerps/gradienterp/sources/metrics")
    rev = next(m for m in metrics["metrics"] if m["key"] == "revenue")
    assert rev["source"]["curl"].startswith("curl " + H.API_URL), "the gerp's own read names the api's url, not its host"


def test_economy_counters_answer_in_the_metric_shape():
    cat, _ = _get("/economy/counters")
    assert {"revenue", "expense", "margin"} <= {s["key"] for s in cat["signals"]}
    m, _ = _get("/economy/counters?signal=revenue")
    assert {"key", "label", "unit", "grain", "headline", "points", "definition", "source"} <= set(m)
    assert m["unit"] == "USD" and m["headline"]["period"]


def test_any_origin_may_read_and_the_stream_needs_a_key():
    for path in ("/gerps", "/gerps/gradienterp/sources/financials", "/economy/counters"):
        _, resp = _get(path, origin="https://openlyoperated.biz")
        assert resp.headers.get_all("Access-Control-Allow-Origin") == ["*"], path
    try:
        _get("/events?channel=/oob/counters")
        raise AssertionError("an anonymous stream was served")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_a_published_firms_product_record_answers_off_the_platform():
    """GET /gerps/gradienterp/metrics: the catalog is the partition. A card per event and kind the
    firm has recorded, points at the day grain, a set's size and never a member, one slug read back."""
    r, _ = _get("/gerps/gradienterp/metrics")
    assert r["gerp_id"] == "gradienterp" and r["grain"] == "day"
    for m in r["metrics"]:
        assert m["key"] == f"{m['event']}.{m['kind']}" and m["kind"] in ("count", "active")
        assert "members" not in m and all(p["period"] and p["value"] is not None for p in m["points"])
        assert m["source"]["curl"].endswith(f"/metrics/{m['key']}?grain=day")
    if r["metrics"]:
        one, _ = _get(f"/gerps/gradienterp/metrics/{r['metrics'][0]['key']}?grain=month")
        assert one["grain"] == "month" and one["key"] == r["metrics"][0]["key"]


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all api integ tests passed")
