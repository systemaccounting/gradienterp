"""reconcile handler — the bank-feed reconciliation loop (see modules/accounting/AGENTS.md § bank-feed reconciliation).

Pulls the gerp's bank transactions through the operator Plaid gateway (cross-account, carrying
only this gerp's access_token), then for each POSTED bank line decides — via the pure logic in
`reconcile.py` — whether it settles a pending payout (MATCH: re-time CASH_PENDING → CASH) or is a
genuinely-new flow (BOOK: post one classified CASH leg + a provisional counter-leg that queues to
pending for owner classification). Persists Plaid's `next_cursor` so the next run is incremental.

Idempotent: every posted entry keys on `reco-<bank_txn_id>` with the txn's own date as timestamp,
so an overlapping re-pull no-ops in post_journal_entry (dedup on pk+sk).

Not an agent tool (no schema.json) — triggered by the gateway's webhook poke / a cron backstop.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import reconcile as R  # sibling pure logic, bundled into the zip (infra extra_sources)

import logging

from aws import client as _aws, table as _table

log = logging.getLogger()

# Plaid is an external service and the gateway that fronts it lives in the OPERATOR account, so
# there is nothing local to stand it up against. `LOCAL_PLAID_PULL` points at a captured response
# and is the one seam that stays — a fixture path, chosen by config, not by which environment this
# is running in. Everything else below (the ledger, SSM, the cross-lambda post) is a real call.
LOCAL_PLAID_PULL = os.environ.get("LOCAL_PLAID_PULL", "")      # {added, modified, removed, next_cursor}
LOCAL_CASH_PENDING = os.environ.get("LOCAL_CASH_PENDING", "")  # [{entry_id, amount, date}, ...]

PLAID_GATEWAY_ARN = os.environ.get("PLAID_GATEWAY_ARN", "")
ACCESS_TOKEN_PARAM = os.environ.get("PLAID_ACCESS_TOKEN_PARAM", "")
CURSOR_PARAM = os.environ.get("PLAID_CURSOR_PARAM", "")


POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")
OPEN_WINDOW_DAYS = int(os.environ.get("OPEN_CASH_PENDING_WINDOW_DAYS", "60"))


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


# ─── date helpers (ISO ⇄ ms epoch at UTC midnight; deterministic → idempotent entries) ───

def _date_ms(iso_date):
    dt = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))


def _ms_to_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


# ─── open CASH_PENDING (the payout legs still awaiting a bank deposit) ───

def compute_open_cash_pending(rows):
    """Pure: ledger pair-rows → the CASH_PENDING legs still open. A payout initiation debits
    CASH_PENDING; a reconcile settlement (source=reconcile) credits it back. Open = an initiation
    not yet drained by a same-amount settlement. Deposit case only — a withdrawal (CR at init) is
    an outflow the bank shows later and reconcile books; tight matching keeps that safe."""
    candidates, settled = [], []
    for r in rows:
        amt = float(r["amount"])
        d = _ms_to_date(int(r["timestamp_ms"]))
        if r.get("debit_account") == "CASH_PENDING":
            candidates.append({"entry_id": r.get("entry_id"), "amount": amt, "date": d})
        elif r.get("credit_account") == "CASH_PENDING" and r.get("source") == "reconcile":
            settled.append(amt)
    open_legs = []
    for c in candidates:
        hit = next((i for i, s in enumerate(settled) if abs(s - c["amount"]) < 0.005), None)
        if hit is not None:
            settled.pop(hit)  # this initiation is already reconciled — drop it
        else:
            open_legs.append(c)
    return open_legs


def _open_cash_pending():
    if LOCAL_CASH_PENDING:
        return json.loads(open(LOCAL_CASH_PENDING).read())   # injected open set, for the offline walk
    # trailing-window scan of the month partitions the open payouts could live in.
    cur = datetime.now(tz=timezone.utc)
    months = set()
    for _ in range(OPEN_WINDOW_DAYS // 28 + 2):
        months.add(f"{cur.year:04d}-{cur.month:02d}")
        cur = cur.replace(day=1) - timedelta(days=1)
    rows = []
    for pk in months:
        kwargs = {"KeyConditionExpression": "pk = :pk", "ExpressionAttributeValues": {":pk": pk}}
        while True:
            resp = ledger_table().query(**kwargs)
            rows += resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return compute_open_cash_pending(rows)


# ─── pull (through the operator gateway; injected locally) ───

def _pull(access_token, cursor):
    if LOCAL_PLAID_PULL:
        return json.loads(open(LOCAL_PLAID_PULL).read())     # a captured Plaid response
    resp = _aws("lambda").invoke(
        FunctionName=PLAID_GATEWAY_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps({"op": "pull", "access_token": access_token, "cursor": cursor}),
    )
    out = json.loads(resp["Payload"].read())
    if not out.get("ok"):
        raise RuntimeError(f"plaid gateway pull failed: {out}")
    return out


# ─── cursor + token state ───

def _read_access_token():
    if LOCAL_PLAID_PULL:
        return "fixture-token"        # the pull is a fixture; no token is exchanged
    ssm = _aws("ssm")
    try:
        return ssm.get_parameter(Name=ACCESS_TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def _read_cursor():
    ssm = _aws("ssm")
    try:
        return ssm.get_parameter(Name=CURSOR_PARAM)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def _write_cursor(cursor):
    if not cursor:
        return
    _aws("ssm").put_parameter(Name=CURSOR_PARAM, Value=cursor, Type="String", Overwrite=True)


# ─── entry builders ───

def _provisional_account(line):
    """The classification hint becomes the provisional (unclassified) account name; the owner maps
    it via add_classification. pfc primary is already UPPER_SNAKE; else slugify the merchant name."""
    pfc = line.get("pfc")
    if pfc:
        return pfc
    slug = "".join(c if c.isalnum() else "_" for c in (line.get("name") or "").upper()).strip("_")
    return slug or "UNCLASSIFIED"


def _match_entry(line, decision):
    e = decision["entry"]  # {debit: CASH, credit: CASH_PENDING, amount}
    return {
        "entryId": f"reco-{line['external_id']}",
        "timestamp": _date_ms(line["date"]),
        "source": "reconcile",
        "memo": f"bank settlement: {line['name']}"[:200],
        "lineItems": [
            {"account": e["debit"], "accountType": "ASSET", "side": "DEBIT", "amount": e["amount"]},
            {"account": e["credit"], "accountType": "ASSET", "side": "CREDIT", "amount": e["amount"]},
        ],
    }


def _book_entry(line, decision):
    # one classified CASH leg + a provisional counter-leg with NO accountType → post_journal_entry
    # queues the whole entry to pending until the owner classifies the counter account.
    amt = decision["amount"]
    cash = {"account": "CASH", "accountType": "ASSET"}
    other = {"account": _provisional_account(line)}
    if decision["direction"] == "inflow":
        items = [{**cash, "side": "DEBIT", "amount": amt}, {**other, "side": "CREDIT", "amount": amt}]
    else:
        items = [{**other, "side": "DEBIT", "amount": amt}, {**cash, "side": "CREDIT", "amount": amt}]
    return {
        "entryId": f"reco-{line['external_id']}",
        "timestamp": _date_ms(line["date"]),
        "source": "reconcile",
        "memo": f"bank: {line['name']}"[:200],
        "lineItems": items,
    }


def _post(payload):
    resp = _aws("lambda").invoke(
        FunctionName=POST_JOURNAL_ENTRY_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    return json.loads(resp["Payload"].read())


# ─── handler ───

def handler(event, context):
    access_token = _read_access_token()
    if not access_token:
        return {"statusCode": 200, "body": json.dumps({"status": "no bank linked"})}

    cursor = _read_cursor()
    pull = _pull(access_token, cursor)
    open_pending = _open_cash_pending()

    matched = booked = skipped = 0
    for txn in pull.get("added", []):
        line = R.normalize(txn)
        if line["pending"]:
            skipped += 1  # in-flight authorization, not a settled movement — wait for it to post
            continue

        decision = R.reconcile(line, open_pending)
        if decision["kind"] == "match":
            # drain the matched leg so a later same-amount line can't re-match it
            open_pending = [p for p in open_pending if p.get("entry_id") != decision["pending_entry_id"]]
            _post(_match_entry(line, decision))
            matched += 1
        else:
            _post(_book_entry(line, decision))
            booked += 1

    _write_cursor(pull.get("next_cursor") or cursor)

    return {
        "statusCode": 200,
        "body": json.dumps({"matched": matched, "booked": booked, "skipped_pending": skipped,
                            "cursor_advanced": bool(pull.get("next_cursor"))}),
    }
