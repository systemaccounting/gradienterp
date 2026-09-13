"""escalate — the agent flags a platform bug or missing capability, fast.

The description is a TEMPLATE: `$1`/`$2` stand where a firm-specific value would go, and
`private` carries those values in order. The template is public by construction — a customer
name cannot be in it — so the operator can file it as a GitHub issue verbatim, placeholders
and all, while the values stay on the private record. That asks the reporting agent an easy
question (which spans are specific to my business?) rather than a hard one (is this prose
safe to publish?).

It also makes dedup work. Two firms hitting one bug used to write "the Henderson wedding" and
"the Diaz catering job" — different strings for the same defect. Templated, both are
`"... for $1"`, so the same defect from different firms is now an exact match instead of a
judgment call.

Emits platform/escalation.raised on the shared bus; the collector lands it as one inc task on
the operator gerp's books.
"""

import json
import logging
import os
import time

import template
from aws import client as _aws

log = logging.getLogger()
log.setLevel(logging.INFO)


CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
OP_EVENT_BUS_ARN = os.environ.get("OP_EVENT_BUS_ARN", "")

TYPES = ("bug", "feature")
DESCRIPTION_CAP = 4000


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    esc_type = body.get("type")
    description = (body.get("description") or "").strip()
    private = body.get("private") or []
    if esc_type not in TYPES:
        return _err(f"type must be one of {TYPES}")
    if not description:
        return _err("description is required — a sentence or two: what you were doing, what broke or was missing")
    if len(description) > DESCRIPTION_CAP:
        return _err(f"description exceeds {DESCRIPTION_CAP} chars")

    bad = template.check(description, private)
    if bad:
        return _err(bad)

    detail = {
        "schema_version": 1,
        "gerp_id": CUSTOMER_ID,
        "type": esc_type,
        "description": description,
        **({"private": private} if private else {}),
        "at": int(time.time() * 1000),
    }

    _aws("events").put_events(Entries=[{
        "EventBusName": OP_EVENT_BUS_ARN,
        "Source": "platform",
        "DetailType": "escalation.raised",
        "Detail": json.dumps(detail),
    }])

    log.info("escalated %s (gerp=%s)", esc_type, CUSTOMER_ID)
    return {"statusCode": 200, "body": json.dumps({"escalated": esc_type})}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}
