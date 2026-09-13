"""settlement — the issuer's settle EFFECT: an agreed, funded capital deal creates its instrument.

Invoked by the shared agreements settle with `{"agreement": row}` when an offer-kind row carries
both stamps and this firm is its SELLER. The dispatcher already ran the money step (purchase.pay,
declared by the AGREEMENT#offer config row) and owns every write to the agreement row, so this
does the one domain thing: ATTACH the rule instance that IS the instrument (pk
`DISTRIBUTION#<thread>`). The holder's side produces nothing at all — its claim is the agreement
row plus the `DR INVESTMENTS` entry, and what has come back is a ledger fold (`get_holdings`).

The terms ARE the row: the product the parties agreed (`net_income_percent_dividend`), the
factor, the cap (a perpetuity has none), the holder (the buyer). `distribution` pays it because
the instance exists — nothing else is written anywhere. Transfer = rewrite `holder`; retire =
delete the instance.

The funds gate holds here too — a row dispatched unpaid issues nothing. A `pay_present_value`
buyout retires a target instead of issuing — deferred; skipped.
"""

from aws import log
import instances

INSTRUMENT = instances.DISTRIBUTION         # the instance-store subject namespace
DISTRIBUTION_RULE = "distribution_share"    # treasury's one general rule — see treasury_rules.py
N = 100                                     # cascade slot; a second instance on the same instrument sorts after it


def _issue(row):
    if not row.get("funds_receipt_ledger_entry"):
        log.info("dispatched unpaid; not issued", thread=row.get("thread"))
        return None
    terms = row.get("terms") or {}
    spec = (terms.get("items") or [{}])[0]       # the instrument spec is the one item being sold
    product = spec.get("product")
    if product == "pay_present_value":
        log.info("pay_present_value buyout is deferred; skipped", thread=row.get("thread"))
        return None
    if not product or spec.get("factor") is None:
        log.info("terms carry no product/factor; skipped", thread=row.get("thread"))
        return None
    instrument_id = row["thread"]                # the negotiation IS the deal, so it names the instrument
    param = {"factor": spec["factor"], "holder": row["buyer"]}
    if spec.get("cap") is not None:
        param["cap"] = spec["cap"]               # a perpetuity carries no cap key — the whole difference
    instances.add(matches=instances.key(INSTRUMENT, instrument_id), n=N,
                  name=product, rule=DISTRIBUTION_RULE, param=param)
    log.info("instrument issued", product=product, instrument_id=instrument_id, holder=row["buyer"])
    return {"instrument_id": instrument_id, "name": product, "holder": row["buyer"], "side": "issuer"}


def handler(event, context):
    result = _issue(event["agreement"])
    return {"ok": True, "issued": [result] if result else []}
