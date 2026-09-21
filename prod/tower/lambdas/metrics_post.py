"""One product event to the metrics door gradienterp published for its own app (modules/metrics).

`METRICS_HOOK_PARAM` names an SSM parameter holding `{url, token}`, what `manage_metrics
op=publish_source` returned. A post follows the durable write it reports and never fails it: a
failure is one log line. A 401 drops the cached parameter so a rotated token is read on the next
post. No parameter configured, no post. Each tower archive lists this file beside main.py.
"""
import json
import logging
import os
import urllib.error
import urllib.request

import boto3

log = logging.getLogger()
_cache: dict = {}   # "hook" -> {url, token}, read once per container


def _hook() -> dict:
    param = os.environ.get("METRICS_HOOK_PARAM", "")
    if not param:
        return {}
    if "hook" not in _cache:
        try:
            raw = boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
            _cache["hook"] = json.loads(raw)
        except Exception:  # noqa: BLE001
            log.exception("metrics hook parameter %s unreadable", param)
            return {}
    return _cache["hook"]


def _send(url: str, token: str, payload: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def post(event: str, subject_id: str, properties: dict | None = None) -> int | None:
    """`{event, subject_id, properties}` to the door. None when nothing is configured, the subject
    is empty or the post itself failed; otherwise the status (202 accepted)."""
    hook = _hook()
    if not hook.get("url") or not hook.get("token") or not subject_id:
        return None
    payload = {"event": event, "subject_id": subject_id,
               "properties": {k: str(v) for k, v in (properties or {}).items() if v not in (None, "")}}
    try:
        status = _send(hook["url"], hook["token"], payload)
    except Exception:  # noqa: BLE001
        log.exception("metric %s not posted", event)
        return None
    if status == 401:
        _cache.pop("hook", None)
    if status >= 300:
        log.warning("metric %s post returned %s", event, status)
    return status
