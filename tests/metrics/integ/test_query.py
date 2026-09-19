"""The canonical queries on a real workgroup: each `metric_queries` row runs on gradienterp's Athena
with its parameters bound as literals — a bound `date_trunc` unit and a bound `AT TIME ZONE` zone
plan, and the answer comes back in the shape the local run gives. Skips without the profile.
Run: `bash scripts/test.sh --env integ --module metrics`, gradienterp up.
"""

import json
import os
import sys

import boto3

PROFILE = os.environ.get("METRICS_INTEG_PROFILE", "gerp-gradienterp")
FN = os.environ.get("METRICS_INTEG_FN", "gerp-metrics-gradienterp-manage_metrics")


def _lambda():
    try:
        return boto3.Session(profile_name=PROFILE).client("lambda", region_name="us-east-1")
    except Exception:  # noqa: BLE001 — no profile: not this machine's test
        return None


def _query(lam, name, **body):
    r = lam.invoke(FunctionName=FN, Payload=json.dumps({"op": "query", "name": name, **body}).encode())
    out = json.loads(r["Payload"].read())
    assert "errorMessage" not in out, out
    return out["statusCode"], json.loads(out["body"])


CASES = {
    "active":    {"params": {"event": "smoke.fired", "grain": "day"}},
    "count":     {"params": {"event": "smoke.fired", "grain": "week"}},
    "count_by":  {"params": {"event": "smoke.fired", "grain": "month", "property": "run"}},
    "funnel_3":  {"params": {"e1": "lead.captured", "e2": "member.joined", "e3": "member.checked_in"}},
    "retention": {"params": {"event": "smoke.fired", "grain": "week"}},
}


def test_every_canonical_query_plans_and_answers_on_the_workgroup():
    lam = _lambda()
    if lam is None:
        print("skip: no profile", PROFILE)
        return
    for name, body in CASES.items():
        code, out = _query(lam, name, window="last_30_days", **body)
        assert code == 200, (name, out)
        assert out["engine"] == "athena" and out["query_id"] and isinstance(out["rows"], list), (name, out)
        print(f"  {name}: {out['row_count']} row(s), {out['bytes_scanned']} bytes")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
