"""Shared helpers for tests/openlyoperated_biz/ — the deployed oob platform surfaces (operator account).

integ/ tests run against LIVE infra and need creds: `operator-org` (operator account — the counters, the
gerp-events bus, the read lambdas). Cross-account reads use a customer profile. Profiles come from
~/.aws/config, overridable via env — same shape as tests/e2e. Run: `bash scripts/test.sh --env integ`
(or `pytest tests/openlyoperated_biz/integ`). local/ tests (offline lambda logic) need no creds.
"""

import json
import os
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = json.loads((REPO_ROOT / "config.json").read_text())
STACK_PREFIX = _CONFIG.get("STACK_PREFIX", "gerp")
REGION = os.environ.get("AWS_REGION", "us-east-1")
OPERATOR_PROFILE = os.environ.get("OOB_OPERATOR_PROFILE", "operator-org")
CUSTOMER_PROFILE = os.environ.get("OOB_CUSTOMER_PROFILE", "gerp-gradienterp")

COUNTERS_TABLE = f"{STACK_PREFIX}-counters"
OP_EVENT_BUS = f"{STACK_PREFIX}-events"

_sessions = {}


def _session(profile):
    import boto3

    if profile not in _sessions:
        _sessions[profile] = boto3.Session(profile_name=profile, region_name=REGION)
    return _sessions[profile]


def operator(service):
    return _session(OPERATOR_PROFILE).client(service)


def creds_available():
    """True if the operator profile resolves — integ tests skip otherwise."""
    try:
        operator("sts").get_caller_identity()
        return True
    except Exception:  # noqa: BLE001
        return False


def put_bus_event(detail, source="accounting", detail_type="journal_entry.posted"):
    return operator("events").put_events(Entries=[{
        "EventBusName": OP_EVENT_BUS, "Source": source, "DetailType": detail_type, "Detail": json.dumps(detail),
    }])


API_URL = "https://api.openlyoperated.biz/v1"


def get_counter(counter_key):
    item = operator("dynamodb").get_item(
        TableName=COUNTERS_TABLE, Key={"counter": {"S": counter_key}}
    ).get("Item")
    return float(item["value"]["N"]) if item and "value" in item else None


def del_counter(counter_key):
    operator("dynamodb").delete_item(TableName=COUNTERS_TABLE, Key={"counter": {"S": counter_key}})


def poll(fn, want, timeout=20, interval=1):
    """Poll fn() until it == want or timeout; return the last value seen."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last == want:
            return last
        time.sleep(interval)
    return last
