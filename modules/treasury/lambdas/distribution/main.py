"""treasury distribution — the period-close payout.

When accounting's `compute_balances` closes a period, it invokes this handler with the
period's net income. For each INSTRUMENT, run the rule instances matched to it, post
DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE, and emit `distribution.paid`.

An instrument's terms ARE its matched instance(s) — a row in the modules/rules instance store,
pk `DISTRIBUTION#<instrument_id>`, written by `settlement` when the offer's funds land:

    DISTRIBUTION#biz#investor#seed / 0100#net_income_percent_dividend
      { rule: distribution_share, param: { factor: 0.10, cap: 750000, holder: "investor" } }

There is nothing else to resolve. No rule set, no name list, no params table: an instrument pays
because an instance is matched to it, and an instrument with none matched does not exist. The
instrument list IS the set of `DISTRIBUTION#` subjects in the instance store — the cap table, as a
projection, nothing materialised.

One post + one event PER INSTRUMENT. Its cumulative payout (the cap fold) is summed from the
ledger — DIVIDENDS_PAYABLE credits tagged with the instrument's dimensions — so that too is a
projection, not stored state.

Idempotent per (instrument, period): a deterministic entryId (dist-<instrument>-<period>) + a
period-stable timestamp, so a re-fire no-ops — accounting dedups on (pk, sk) where sk embeds the
timestamp, so BOTH must be stable.

The ledger and the instance store are real tables the test seeds;
post_journal_entry appends to a local journal; distribution.paid appends to the local events jsonl.
The handler runs standalone given {periodEnd, netIncome}.
"""

import datetime
import json
import logging
import os
import time
from decimal import Decimal

import instances
import ledger        # the rule-instance store — modules/rules/instances.py
import rules            # the engine (run_instances/lineitems) — modules/rules/rules.py
import treasury_rules   # the distribution rule — modules/treasury/treasury_rules.py

from boto3.dynamodb.conditions import Key, Attr

import journal
from aws import client as _aws, table as _table, log as alog

INSTRUMENT = instances.DISTRIBUTION                  # the instance-store subject namespace
_PREFIX = f"{INSTRUMENT}#"

LEDGER_INCEPTION = os.environ.get("LEDGER_INCEPTION", "2026-01")  # first ledger month to fold from
CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "test_customer")

log = logging.getLogger()
log.setLevel(logging.INFO)

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")
OP_EVENT_BUS_ARN = os.environ.get("OP_EVENT_BUS_ARN")


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


def _openly_operated() -> bool:
    """Current publication consent, read PER INVOKE, never cached — consent is a reference, and a
    cold-start cache kept stamping `true` after a gerp turned publication off, into a firehose
    archive that cannot be unwritten. Mirrors accounting's post_journal_entry."""
    row = _table(os.environ["SETTINGS_TABLE"]).get_item(
        Key={"gerp_id": CUSTOMER_ID, "sk": "GERP#openly_operated"}
    ).get("Item") or {}
    return bool(row.get("value", False))


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ─── period / timestamp ───

def _period_end_ms(period_end: str) -> int:
    """ms-epoch of the period-close timestamp. period_end is accounting's ISO periodEnd
    ('2026-03-31' or '...T00:00:00Z'). Timestamps the entry deterministically so a re-fire
    no-ops, and lands it in the right month partition."""
    dt = datetime.datetime.fromisoformat(period_end.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _month_partitions(start_month: str, end_month: str):
    """YYYY-MM partition keys from start_month..end_month inclusive."""
    y, m = (int(x) for x in start_month.split("-"))
    ey, em = (int(x) for x in end_month.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m = m + 1 if m < 12 else 1
        if m == 1:
            y += 1


# ─── the instruments (a projection of the instance store) ───

def _instrument_ids():
    """Every instrument with an instance matched — the cap table, read straight off the instance
    store. An instrument exists because settlement matched a row to it; there is no instruments
    table and nothing else to consult."""
    pks, kwargs = set(), {"ProjectionExpression": "pk",
                          "FilterExpression": Attr("pk").begins_with(_PREFIX)}
    while True:
        resp = instances._table().scan(**kwargs)
        pks.update(r["pk"] for r in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(pk[len(_PREFIX):] for pk in pks)


# ─── ledger reads ───

def _ledger_partition(month: str):
    """Yield ledger pair-rows in one YYYY-MM partition."""
    kwargs = {"KeyConditionExpression": Key("pk").eq(month)}
    while True:
        resp = ledger_table().query(**kwargs)
        yield from resp.get("Items", [])
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def _cumulative_distributed(instrument_id: str, period_end: str) -> Decimal:
    """Lifetime payout for this instrument *before* this period — DIVIDENDS_PAYABLE credits tagged
    with its instrument_id whose period is strictly < period_end. A fold over the ledger (a
    projection of prior distributions); drives the cap. The settle leg (DR DIVIDENDS_PAYABLE / CR
    CASH) is a debit, excluded — only declarations count.

    The fold itself lives in `treasury/ledger.py`, shared with the buy side's `get_holdings`, which
    asks the same question of INVESTMENT_INCOME on the same dimension."""
    return ledger.fold_credits("DIVIDENDS_PAYABLE", "instrument_id", instrument_id,
                               upto_month=period_end[:7], before=period_end)


# ─── post + emit ───

def post_journal_entry(payload: dict):
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


def emit_distribution_paid(detail: dict):
    """Announcement after the durable post. The holder's inbox books INVESTMENT income off this
    event, so a lost publish is a lost receivable on their books: a failure raises and the async
    retry re-runs the period close (the post is idempotent, so the entry does not double).
    Contract: modules/events/treasury/distribution.paid.v1.json."""
    if not OP_EVENT_BUS_ARN:
        return
    try:
        _aws("events").put_events(Entries=[{
            "Source": "treasury",
            "DetailType": "distribution.paid",
            "Detail": json.dumps(detail, cls=_DecimalEncoder),
            "EventBusName": OP_EVENT_BUS_ARN,
        }])
    except Exception:
        alog.exception("distribution.paid not published", instrument_id=detail.get("instrument_id"),
                       period=detail.get("period_end"))
        raise


# ─── the run ───

def _run_instrument(instrument_id, period_end, net_income):
    """One instrument: run what's matched, post the entry, announce it."""
    matched = instances.for_key(instances.key(INSTRUMENT, instrument_id))
    if not matched:
        return None                                   # nothing matched — not an instrument

    cumulative = _cumulative_distributed(instrument_id, period_end)
    ctx = {"net_income": net_income, "cumulative_paid": cumulative}
    effects = rules.run_instances(ctx, matched, modules=[treasury_rules])
    if not effects:
        return None                                   # a loss, or the cap is reached — nothing to post

    amount = sum(e["amount"] for e in effects
                 if e["side"] == "CREDIT" and e["account"] == "DIVIDENDS_PAYABLE")
    terms = matched[0].get("param") or {}
    holder = terms.get("holder", instrument_id)
    name = matched[0]["name"]                         # the product the parties agreed on
    entry_id = f"dist-{instrument_id}-{period_end}"
    post_journal_entry({
        "lineItems": effects,
        "memo": f"distribution {name} to {holder} on net income {net_income} ({period_end})",
        "source": entry_id,
        "dimensions": {"holder": holder, "rule": name,
                       "period": period_end, "instrument_id": instrument_id},
        "entryId": entry_id,                          # deterministic — pairs with the stable
        "timestamp": str(_period_end_ms(period_end)),  # timestamp to make a re-fire a no-op
    })

    new_cumulative = cumulative + amount
    detail = {
        "schema_version": 1,
        "openly_operated": _openly_operated(),
        "customer_id": CUSTOMER_ID,
        "instrument_id": instrument_id,
        "holder": holder,
        "rule": name,
        "amount": amount,
        "period_end": period_end,
        "cumulative_paid": new_cumulative,
        "entry_id": entry_id,                         # the verification join to journal_entry.posted
    }
    cap = terms.get("cap")
    if cap not in (None, ""):
        detail["cap_remaining"] = Decimal(str(cap)) - new_cumulative
    emit_distribution_paid(detail)

    return {"instrument_id": instrument_id, "amount": amount, "entry_id": entry_id,
            "cumulative_paid": new_cumulative}


def handler(event, context):
    """Period-close entrypoint (invoked by compute_balances on completion, or directly).
    event: {periodEnd, netIncome, instrument?}. With `instrument`, runs just that one;
    otherwise every instrument with an instance matched."""
    body = event.get("body")
    if isinstance(body, str):
        event = json.loads(body)
    elif isinstance(body, dict):
        event = body

    period_end = event.get("periodEnd") or event.get("period_end")
    net_income = event.get("netIncome", event.get("net_income"))
    if not period_end:
        return {"statusCode": 400, "body": json.dumps({"error": "periodEnd is required"})}
    if net_income is None:
        return {"statusCode": 400, "body": json.dumps({"error": "netIncome is required"})}
    net_income = Decimal(str(net_income))

    ids = [event["instrument"]] if event.get("instrument") else _instrument_ids()
    paid = [r for r in (_run_instrument(i, period_end, net_income) for i in ids) if r]
    return {"statusCode": 200, "body": json.dumps(
        {"periodEnd": period_end, "instruments": len(ids), "paid": paid},
        cls=_DecimalEncoder)}
