"""publisher — the operator bus onto the Events API, the stream every consumer reads.

The rule: any bus event with `detail.customer_id` and no `detail.to` (an addressed event is between
two firms). Each lands here and goes out on up to two channels:

- `/oob/counters` — every `detail.counters` entry as `{key, op, magnitude, period, at}`: economic
  data, every gerp, never the gerp id and never the event
- `/oob/<gerp_id>/<kind>` — the event's detail projected through its contract, only when the
  gerp's row reads `published`. The contract (`modules/events/<module>/<kind>.v1.json`, bundled
  as `contracts/`) names what an event carries; a property marked `class: subject` or
  `class: secret` is dropped, and a kind with no contract has no channel. `kind` is the
  detail-type with `.` and `_` as `-`, since a channel segment is letters, digits and dashes.

Publishes over HTTP with SigV4 (the api's publish auth is IAM; a subscriber's is the public key).
The row's `published` bit is read per gerp and cached a minute per container.
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from aws import client as _aws

EVENTS_HTTP = os.environ.get("EVENTS_HTTP", "")          # https://<host>/event
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
CONTRACTS_DIR = Path(os.environ.get("CONTRACTS_DIR", str(Path(__file__).parent / "contracts")))
REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVELOPE = {"schema_version", "openly_operated", "customer_id", "counters"}
_published_cache = {}   # gerp_id -> (at, bool)
_contracts = {}


def channel_slug(s):
    return re.sub(r"[^A-Za-z0-9-]", "-", s).strip("-")[:50]


def _published(gerp_id):
    hit = _published_cache.get(gerp_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    it = _aws("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
                                   ProjectionExpression="published").get("Item") or {}
    val = bool(it.get("published", {}).get("BOOL", False))
    _published_cache[gerp_id] = (time.time(), val)
    return val


def contract(kind):
    """The event's contract, or None for a kind that has none."""
    if kind not in _contracts:
        p = CONTRACTS_DIR / f"{kind}.v1.json"
        _contracts[kind] = json.loads(p.read_text()) if p.exists() else None
    return _contracts[kind]


def project(value, schema):
    """`value` reduced to the properties `schema` names, recursively; subject and secret dropped."""
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        out = {}
        for k, v in value.items():
            prop = schema["properties"].get(k)
            if prop is None or k in ENVELOPE or prop.get("class") in ("subject", "secret"):
                continue
            out[k] = project(v, prop)
        return out
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [project(v, schema["items"]) for v in value]
    return value


def _period(detail):
    ms = detail.get("posted_at_ms")
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def post(channel, events):
    """One signed publish per five events, each event a JSON string."""
    out = []
    host = EVENTS_HTTP.split("/")[2]
    for i in range(0, len(events), 5):
        body = json.dumps({"channel": channel, "events": [json.dumps(e) for e in events[i:i + 5]]}).encode()
        req = AWSRequest(method="POST", url=EVENTS_HTTP, data=body,
                         headers={"content-type": "application/json", "host": host})
        SigV4Auth(boto3.Session().get_credentials(), "appsync", REGION).add_auth(req)
        r = urllib.request.Request(EVENTS_HTTP, data=body, method="POST", headers=dict(req.headers))
        with urllib.request.urlopen(r, timeout=8) as resp:
            out.append(json.loads(resp.read() or b"{}"))
    return out


def route(event):
    """Where one bus event goes: [(channel, [events])]."""
    detail = event.get("detail") or {}
    gerp_id = detail.get("customer_id") or ""
    kind = event.get("detail-type") or ""
    at = event.get("time") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    counters = [c for c in detail.get("counters") or [] if c.get("key")]
    if counters:
        period = _period(detail)
        out.append(("/oob/counters", [{"key": c["key"], "op": c.get("op", "add"), "magnitude": c.get("magnitude", 1),
                                       "period": period, "at": at} for c in counters]))
    schema = contract(kind) if gerp_id and kind else None
    if schema and _published(gerp_id):
        out.append((f"/oob/{channel_slug(gerp_id)}/{channel_slug(kind)}",
                    [{"kind": kind, "gerp_id": gerp_id, "at": at, "detail": project(detail, schema)}]))
    return out


def handler(event, context):
    sent = []
    for channel, events in route(event):
        post(channel, events)
        sent.append({"channel": channel, "events": len(events)})
    return {"published": sent}
