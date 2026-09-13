"""get_aws_cost: the gerp's AWS bill by service off a faked Cost Explorer, the windows, the fold,
the estimate at the markup — and the markup equal to what bill_customer bills at."""

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, env  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


class FakeCE:
    """One page unless told otherwise; records the windows asked for."""

    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.calls = list(pages or []), error, []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        page = self.pages.pop(0) if self.pages else {"ResultsByTime": []}
        return page


def _page(costs, token=None):
    groups = [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"}}} for k, v in costs.items()]
    return {"ResultsByTime": [{"Groups": groups}], **({"NextPageToken": token} if token else {})}


def _load(ce, today=dt.date(2026, 9, 4)):
    with env(AWS_DEFAULT_REGION="us-east-1"):
        mod = load_lambda("get_aws_cost")
    mod._aws = lambda name: ce
    mod._today = lambda: today
    return mod


def _call(mod, body):
    out = mod.handler(body, None)
    return out["statusCode"], json.loads(out["body"])


def test_month_to_date_by_service_sorted_folded_and_estimated():
    costs = {f"svc{i}": 0.5 for i in range(12)}
    costs.update({"AWS Config": 0.68, "Amazon Bedrock AgentCore": 0.6, "Zero": 0})
    mod = _load(FakeCE([_page(costs)]))
    code, body = _call(mod, {"op": "read"})
    assert code == 200, body
    assert body["period"] == {"start": "2026-09-01", "end": "2026-09-04"}
    assert body["by_service"][0] == {"service": "AWS Config", "amount": 0.68}
    assert body["by_service"][1] == {"service": "Amazon Bedrock AgentCore", "amount": 0.6}
    assert len(body["by_service"]) == 11 and body["by_service"][-1]["service"] == "other"
    assert body["by_service"][-1]["amount"] == 2.0, "the four half-dollar services past the top ten fold"
    assert "Zero" not in [r["service"] for r in body["by_service"]]
    assert body["total"] == 7.28 and body["currency"] == "USD"
    assert body["markup"] == 1.2 and body["invoice_estimate"] == 8.74


def test_last_month_and_an_explicit_range_resolve_to_the_right_windows():
    ce = FakeCE([_page({"a": 1}), _page({"a": 1})])
    mod = _load(ce)
    _call(mod, {"op": "read", "period": "last_month"})
    assert ce.calls[-1]["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-09-01"}
    _call(mod, {"op": "read", "range": {"start": "2026-07-10", "end": "2026-12-31"}})
    assert ce.calls[-1]["TimePeriod"] == {"Start": "2026-07-10", "End": "2026-09-04"}, "a future end is cut at today"
    assert ce.calls[-1]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
    # the first of the month: nothing has posted, and CE would refuse an empty window
    mod = _load(FakeCE(), today=dt.date(2026, 9, 1))
    code, body = _call(mod, {"op": "read"})
    assert code == 200 and body["total"] == 0 and body["by_service"] == []
    assert _call(mod, {"op": "read", "period": "yesterday"})[0] == 400
    assert _call(mod, {"op": "list"})[0] == 400


def test_a_second_page_is_read_and_a_refusal_is_the_tools_error():
    ce = FakeCE([_page({"a": 1, "b": 2}, token="t2"), _page({"a": 0.5, "c": 3})])
    mod = _load(ce)
    code, body = _call(mod, {"op": "read"})
    assert code == 200 and body["total"] == 6.5 and ce.calls[1]["NextPageToken"] == "t2"
    assert [r["service"] for r in body["by_service"]] == ["c", "b", "a"]

    class Refused(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "not authorized: ce:GetCostAndUsage"}}
    mod = _load(FakeCE(error=Refused()))
    code, body = _call(mod, {"op": "read"})
    assert code == 502 and "ce:GetCostAndUsage" in body["error"]


def test_the_markup_is_the_one_bill_customer_bills_at():
    here = re.search(r'^MARKUP = Decimal\("([^"]+)"\)', (REPO / "modules/accounting/lambdas/get_aws_cost/main.py").read_text(), re.M).group(1)
    there = re.search(r'^MARKUP = Decimal\("([^"]+)"\)', (REPO / "prod/tower/lambdas/bill_customer/main.py").read_text(), re.M).group(1)
    assert here == there == "1.2"
    disclosure = (REPO / "prod/gradienterp_cloud/web/purchase-terms.txt").read_text()
    assert "20% operator markup" in disclosure, "the third copy is what the buyer read"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all get_aws_cost tests passed")
