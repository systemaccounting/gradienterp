"""get_holdings — the portfolio: instruments this firm HOLDS in other firms.

The mirror of the cap table. `manage_rules` (op list) on the `DISTRIBUTION#` subjects answers "who holds a claim on
MY margin"; this answers "what do I hold on other people's".

**Nothing is stored to make this work.** A holding is not an object — it is two facts the books
already carry, joined:

    the TERMS      the agreement row where this firm is the buyer and the deal settled
                   (issuer, product, factor, cap — the same row the counterparty has)
    the MONEY      the ledger: `DR INVESTMENTS` dimensioned `instrument_id` is what was paid,
                   and `CR INVESTMENT_INCOME` on the same dimension is what has come back

So `paid_to_date` is a FOLD, not a counter something has to remember to advance, and `remaining` is
arithmetic on it. A stored mirror would be a third copy of facts already recorded twice, and the only
thing a third copy can do that the other two cannot is disagree with them.

That fold is also the holder's INDEPENDENT count. Against an openly-operated issuer it makes the cap
checkable — the same `distribution.paid` events surface on the public feed, so two separately derived
numbers should agree. Against a private issuer it stays trust, which is a property of the
counterparty and not something more bookkeeping here can fix.
"""

import datetime as dt
import json
from decimal import Decimal

from agreements import list_agreements
import ledger
from _helpers import GERP_ID, ok


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    want = body.get("issuer")

    upto = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")   # fold to the current month
    # kind gate first: the shared store holds every agreement kind, and a settled PO where this
    # firm is the buyer is a purchase, not a holding
    mine = [r for r in list_agreements()
            if r.get("kind") == "offer"
            and r.get("buyer") == GERP_ID and r.get("settled_time")
            and (not want or r.get("seller") == want)]

    out = []
    for r in mine:
        spec = ((r.get("terms") or {}).get("items") or [{}])[0]
        thread = r.get("thread")
        received = ledger.fold_credits("INVESTMENT_INCOME", "instrument_id", thread, upto)
        paid = ledger.fold_debits("INVESTMENTS", "instrument_id", thread, upto)
        row = {
            "thread": thread,
            "issuer": r.get("seller"),
            "product": spec.get("product"),
            "factor": spec.get("factor"),
            "cost": paid,                 # what we put in, off the ledger — not the quoted price
            "paid_to_date": received,     # what has come back, folded from the same dimension
            "funded": bool(r.get("funds_receipt_ledger_entry")),
        }
        # A perpetuity carries no cap key at all — there is nothing to count down to, which is the
        # whole difference between the two instrument shapes. Reporting `remaining: null` would
        # invite a caller to treat it as zero.
        if spec.get("cap") is not None:
            row["cap"] = spec["cap"]
            row["remaining"] = Decimal(str(spec["cap"])) - Decimal(str(received))
        out.append(row)

    return ok({"count": len(out), "holdings": out})
