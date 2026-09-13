"""payment_links — the hosted pages a processor serves for this firm, one tool.

Three kinds, one capability: the money-page is the processor's, we only mint the url.
`kind: payment` is a link that settles one invoice (fresh each send — links expire and the
amount is re-read). `kind: setup` is a page where a payer saves a card without the number ever
touching us. `kind: test` is a real test-mode payment, so the processor signs and delivers its
own webhook and the whole seam is proven. Each kind's body is the old tool's, verbatim.
"""

import json

import payment
import setup_link
import test_payment

KINDS = {"payment": payment, "setup": setup_link, "test": test_payment}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    kind = (body.pop("kind", "") or "").strip()
    mod = KINDS.get(kind)
    if not mod:
        return {"statusCode": 400, "body": json.dumps(
            {"error": "kind must be one of: payment, setup, test"})}
    return mod.handler(body, context)
