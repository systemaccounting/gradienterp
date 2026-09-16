"""record — the door: POST /metrics, for an app that holds nothing of ours but a bearer.

The gateway cannot check a per-caller secret, so this does: the bearer is compared in constant
time with every `METRICS_TOKEN_<CALLER>` at the firm's metrics env path (`manage_metrics
op=publish_source` writes them, one per caller). A wrong or missing bearer is a 401 that names
nothing. The body is one event or a list; every event is checked before any is sent, so a batch is
accepted whole or refused with the index and the field. 202 with the count.

Tokens are read once per container and kept `TOKEN_CACHE_S` seconds (30), and read again on a
miss, so a new token admits at once and a rotated-out one stops admitting within the cache's life.
"""

import base64
import hmac
import json
import logging
import os
import time

from aws import client as _aws, log as alog
import metrics

log = logging.getLogger()
log.setLevel(logging.INFO)

ENV_PATH = os.environ.get("METRICS_ENV_PATH", "")
TOKEN_PREFIX = "METRICS_TOKEN_"

_tokens: dict[str, str] = {}   # caller → token
_loaded_at = 0.0


def _resp(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _load_tokens() -> dict[str, str]:
    global _tokens, _loaded_at
    out = {}
    if ENV_PATH:
        pages = _aws("ssm").get_paginator("get_parameters_by_path").paginate(
            Path=ENV_PATH, Recursive=False, WithDecryption=True)
        for page in pages:
            for p in page.get("Parameters", []):
                name = p["Name"].rsplit("/", 1)[-1]
                if name.startswith(TOKEN_PREFIX):
                    out[name[len(TOKEN_PREFIX):].lower()] = p["Value"]
    _tokens, _loaded_at = out, time.monotonic()
    return out


def _match(presented: str, tokens: dict) -> str | None:
    for caller, tok in tokens.items():
        if hmac.compare_digest(presented.encode(), tok.encode()):
            return caller
    return None


def _admit(event) -> str | None:
    """The caller the bearer names, or None."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    presented = headers.get("authorization", "")
    presented = presented[7:].strip() if presented.lower().startswith("bearer ") else ""
    if not presented:
        return None
    ttl = float(os.environ.get("TOKEN_CACHE_S", "30"))
    tokens = _tokens if _tokens and time.monotonic() - _loaded_at < ttl else _load_tokens()
    caller = _match(presented, tokens)
    if caller is None and tokens is _tokens:
        caller = _match(presented, _load_tokens())   # a rotation since the last read
    return caller


def _body(event):
    raw = event.get("body")
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def handler(event, context):
    caller = _admit(event)
    if caller is None:
        return _resp(401, {"error": "unauthorized"})
    try:
        body = _body(event)
    except Exception:  # noqa: BLE001
        return _resp(400, {"error": "body: json, an event or a list of events"})
    items = body if isinstance(body, list) else [body] if isinstance(body, dict) else []
    if not items:
        return _resp(400, {"error": "body: an event {event, subject_id, at?, properties?} or a list of them"})
    normalized = []
    for i, raw in enumerate(items):
        try:
            normalized.append(metrics.normalize(raw))
        except metrics.Invalid as e:
            return _resp(400, {"error": f"event {i}: {e}"})
    for d in normalized:
        metrics.record({"event": d["event"], "subject_id": d["subject_id"], "at": d["ts"],
                        "properties": d["properties"]}, via="door", caller=caller)
    alog.info("metrics recorded", name=caller, count=len(normalized))
    return _resp(202, {"accepted": len(normalized), "source": caller})
