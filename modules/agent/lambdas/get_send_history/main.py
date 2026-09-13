"""get_send_history — what has been sent, after the fact.

`send_email` returns per-recipient results to whoever called it, which answers the question in
the moment. This answers it later: "did the invoices go out last night?" is an ordinary question
about a send nobody was watching.

The log group IS the record — one structured line per invocation — so there is no second table
holding what is already being written. The limit that comes with that is retention: a group that
keeps 30 days cannot answer about March.
"""

import json
import os
import time

from aws import client, log

LOG_GROUP = os.environ.get("SEND_LOG_GROUP", "")
DEFAULT_HOURS = 24
MAX_EVENTS = 200


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    hours = int(body.get("hours") or DEFAULT_HOURS)
    sender = (body.get("from") or "").strip()
    failures_only = bool(body.get("failures_only"))

    if not LOG_GROUP:
        return {"statusCode": 503, "body": json.dumps({"error": "no send log configured"})}

    pattern = '{ $.event = "send_email"' + (' && $.failed_count > 0' if failures_only else "") + " }"
    args = {
        "logGroupName": LOG_GROUP,
        "startTime": int((time.time() - hours * 3600) * 1000),
        "filterPattern": pattern,
        "limit": MAX_EVENTS,
    }

    # Pagination is not optional here, and not for the usual reason. FilterLogEvents scans log
    # STREAMS, so a page can come back with zero events and a nextToken — the matches are simply
    # further in. Reading only the first page reports "nothing sent" for a send that did happen.
    events, token = [], None
    try:
        while len(events) < MAX_EVENTS:
            resp = client("logs").filter_log_events(**({**args, "nextToken": token} if token else args))
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:
        log.error("send log read failed", log_group=LOG_GROUP, hours=hours, sender=sender, error=str(e))
        return {"statusCode": 502, "body": json.dumps({"error": f"could not read the send log: {e}"})}

    sends = []
    for ev in events:
        try:
            row = json.loads(ev["message"])
        except json.JSONDecodeError:
            continue
        if sender and row.get("from") != sender:
            continue
        row.pop("event", None)
        sends.append(row)

    return {"statusCode": 200, "body": json.dumps({
        "hours": hours,
        "count": len(sends),
        "sends": sends,
        "truncated": bool(token),
    })}
