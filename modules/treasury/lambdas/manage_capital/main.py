"""manage_capital — the capital deal, both sides, one tool.

    record_receipt   the issuer books an investor's funds landing: DR CASH / CR OWNER_EQUITY
    record_outlay    the holder books money going out for what it bought: DR INVESTMENTS / CR CASH
    offers           the deal store by stage (offer / bid / accepted / paid / settled)
    holdings         the portfolio — instruments this firm holds in other firms, folded off the ledger

Each op's body is the tool it absorbed, moved in unchanged as a sibling module; this file routes on
`op` and strips it before delegating. propose_offer / accept_offer stay their own tools — they are
agreements kinds, served by the shared agreements service.
"""

import json

import get_holdings
import get_offers
import record_capital_outlay
import record_capital_receipt

OPS = {
    "record_receipt": record_capital_receipt.handler,
    "record_outlay": record_capital_outlay.handler,
    "offers": get_offers.handler,
    "holdings": get_holdings.handler,
}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    run = OPS.get(op)
    if run is None:
        return {"statusCode": 400, "body": json.dumps(
            {"error": "op is required: " + ", ".join(OPS)})}
    return run(body, context)
