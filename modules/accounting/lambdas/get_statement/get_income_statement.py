import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from aws import table as _table


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


def _iso_to_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _month_partitions(start_ms: int, end_ms: int):
    s = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    e = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        yield f"{y:04d}-{m:02d}"
        m = m + 1 if m < 12 else 1
        if m == 1:
            y += 1


def _dims_match(row, dims):
    """Subset match against the row's entry-level dimensions: every requested key must equal the
    row's. An unstamped row matches no filter — an entry nobody dimensioned belongs to no slice.

    Matches the UNION of `dims` (what describes the transaction) and `dims_private` (a person
    reference). post_journal_entry files them into two fields so a PUBLIC reader can publish
    `dims` wholesale without reaching a person; this is an internal statement read, so slicing by
    worker must keep working. `dimensions` is the pre-split field, still read for older rows."""
    if not dims:
        return True
    have = {**(row.get("dimensions") or {}),
            **(row.get("dims") or {}),
            **(row.get("dims_private") or {})}
    return all(str(have.get(k)) == str(v) for k, v in dims.items())


def _rows_from_ddb(time_range, dims=None):
    """Yield (account_id, account_type, side, total) — filtered to REVENUE/EXPENSE, aggregated per account."""
    start_ms = _iso_to_ms(time_range["start"])
    end_ms = _iso_to_ms(time_range["end"])
    sk_start = f"{start_ms:020d}"
    sk_end = f"{end_ms:020d}~"
    agg = defaultdict(float)
    for pk in _month_partitions(start_ms, end_ms):
        kwargs = {
            "KeyConditionExpression": "pk = :pk AND sk BETWEEN :a AND :b",
            "ExpressionAttributeValues": {":pk": pk, ":a": sk_start, ":b": sk_end},
        }
        while True:
            resp = ledger_table().query(**kwargs)
            for r in resp.get("Items", []):
                if not _dims_match(r, dims):
                    continue
                amt = float(r["amount"])
                if (r.get("debit_account_type") or "").upper() in ("REVENUE", "EXPENSE"):
                    agg[(r["debit_account"], (r.get("debit_account_type") or "").upper(), "DEBIT")] += amt
                if (r.get("credit_account_type") or "").upper() in ("REVENUE", "EXPENSE"):
                    agg[(r["credit_account"], (r.get("credit_account_type") or "").upper(), "CREDIT")] += amt
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    for (account_id, account_type, side), total in agg.items():
        yield account_id, account_type, side, total


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    time_range = body["range"]
    dims = body.get("dimensions")

    rows = _rows_from_ddb(time_range, dims)

    accounts = {}
    for account_id, account_type, side, total in rows:
        if account_id not in accounts:
            accounts[account_id] = {"account_type": account_type, "debits": 0.0, "credits": 0.0}
        accounts[account_id]["debits" if side == "DEBIT" else "credits"] = total

    revenue = []
    expenses = []
    total_revenue = 0.0
    total_expenses = 0.0

    for account_id, vals in accounts.items():
        entry = {"accountId": account_id, "debits": vals["debits"], "credits": vals["credits"]}
        if vals["account_type"] == "REVENUE":
            entry["balance"] = entry["credits"] - entry["debits"]
            revenue.append(entry)
            total_revenue += entry["balance"]
        else:
            entry["balance"] = entry["debits"] - entry["credits"]
            expenses.append(entry)
            total_expenses += entry["balance"]

    return {
        "statusCode": 200,
        "body": json.dumps({
            "revenue": revenue,
            "expenses": expenses,
            "netIncome": total_revenue - total_expenses,
            "asOf": time_range.get("end", _now_iso()),
            # a filtered statement covers only entries STAMPED with these dimensions
            **({"dimensions": dims} if dims else {}),
        }),
    }


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
