"""oob_metrics: the statement folded into four metrics in the one public metric shape, off a
seeded ledger; gated like every public read; equal to what oob_financials computes for the
same rows."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, env, seed_ledger  # noqa: E402

NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
GERP = "cafe"


def _ms(days_ago):
    return int((NOW - timedelta(days=days_ago)).timestamp() * 1000)


def _row(days_ago, amount, debit, debit_type, credit, credit_type):
    return {"time_ms": _ms(days_ago), "amount": amount, "debit_account": debit, "debit_account_type": debit_type,
            "credit_account": credit, "credit_account_type": credit_type}


def _seed():
    seed_ledger([
        _row(1, 100, "CASH", "ASSET", "SALES_REVENUE", "REVENUE"),
        _row(2, 40, "COST_OF_GOODS_SOLD", "EXPENSE", "CASH", "ASSET"),
        _row(3, 10, "RENT_EXPENSE", "EXPENSE", "CASH", "ASSET"),
        _row(200, 999, "CASH", "ASSET", "SALES_REVENUE", "REVENUE"),   # outside the window
    ])


def _publish(on=True):
    from aws import table
    table(os.environ["SETTINGS_TABLE"]).put_item(Item={"gerp_id": GERP, "sk": "GERP#openly_operated", "value": on})


def _load(name):
    with env(GERP_ID=GERP, WINDOW_DAYS="90"):
        return load_lambda(name)


def test_the_four_metrics_fold_the_ledger_in_the_metric_shape():
    with scratch_env() as (tmp, _):
        _seed(); _publish()
        mod = _load("oob_metrics")
        metrics = {m["key"]: m for m in mod._metrics(NOW, "curl https://x/oob/metrics")}
        assert list(metrics) == ["revenue", "expense", "gross_margin", "net_margin"]
        for m in metrics.values():
            assert set(m) == {"key", "label", "unit", "grain", "headline", "points", "definition", "source"}
            assert m["source"] == {"curl": "curl https://x/oob/metrics"} and m["grain"] == "day" and m["definition"]
            assert len(m["points"]) == 30, "90 days downsampled to the sparkline"
        assert metrics["revenue"]["headline"] == {"period": "2026-09", "value": 100.0}
        assert metrics["expense"]["headline"] == {"period": "2026-09", "value": 50.0}
        assert metrics["revenue"]["unit"] == "USD" and metrics["gross_margin"]["unit"] == "ratio"
        assert metrics["gross_margin"]["headline"] == {"period": "trailing 90d", "value": 0.6}
        assert metrics["net_margin"]["headline"] == {"period": "trailing 90d", "value": 0.5}
        # the ratio is window-to-date: undefined before the first revenue, then cumulative
        gross = metrics["gross_margin"]["points"]
        assert gross[0]["value"] is None and gross[-1]["value"] == 0.6
        assert sum(p["value"] for p in metrics["revenue"]["points"] if p["value"]) <= 100.0, "downsampling drops days, never invents"


def test_a_broken_balances_read_is_a_502_never_a_zero():
    """The public statement folds a standing balance off the balances cache. A read that THROWS
    (the table gone, a throttle) is a failure the reader must see: a 502 with the line, not a 0
    served as fact on the public feed. An empty cache is still 0."""
    with scratch_env() as (tmp, _):
        _seed(); _publish()
        with env(GERP_ID=GERP, WINDOW_DAYS="90", BALANCES_TABLE="no-such-table"):
            fin = load_lambda("oob_financials")
        out = fin.handler({"requestContext": {"domainName": "abc.execute-api.us-east-1.amazonaws.com"}}, None)
        assert out["statusCode"] == 502, out
        assert "financials read failed" in json.loads(out["body"])["error"]


def test_the_metrics_agree_with_the_statement_and_the_read_is_gated():
    with scratch_env() as (tmp, _):
        _seed(); _publish()
        mod = _load("oob_metrics")
        fin = _load("oob_financials")
        statement = fin._financials()
        metrics = {m["key"]: m for m in mod._metrics(NOW, "c")}
        assert statement["revenue"] == 100.0 and statement["expenses"] == 50.0
        assert metrics["net_margin"]["headline"]["value"] == (statement["revenue"] - statement["expenses"]) / statement["revenue"]
        out = mod.handler({"requestContext": {"domainName": "abc.execute-api.us-east-1.amazonaws.com"}}, None)
        assert out["statusCode"] == 200
        body = json.loads(out["body"])
        assert body["gerp_id"] == GERP and body["metrics"][0]["source"]["curl"] == "curl https://abc.execute-api.us-east-1.amazonaws.com/oob/metrics"
        via = mod.handler({"requestContext": {"domainName": "abc.execute-api.us-east-1.amazonaws.com"},
                           "headers": {"X-Public-Url": "https://api.openlyoperated.biz/v1/gerps/cafe/sources/metrics"}}, None)
        assert json.loads(via["body"])["metrics"][0]["source"]["curl"] == "curl https://api.openlyoperated.biz/v1/gerps/cafe/sources/metrics", "through the api, the curl is the api call"
        _publish(False)
        assert mod.handler({}, None)["statusCode"] == 404


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all oob_metrics tests passed")
