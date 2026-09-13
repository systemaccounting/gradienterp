import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from aws import table as _table


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


# The first month the ledger can hold anything, and what "inception" below actually means. The
# ledger is partitioned by `YYYY-MM` and a fold QUERIES each partition, so starting at epoch 0 meant
# walking every month since 1970 — hundreds of queries per balance sheet, one more every month.
# Per-gerp config: a firm migrating older history sets its own. An entry dated before it is only
# reachable by a read that passes an explicit start.
LEDGER_INCEPTION = os.environ.get("LEDGER_INCEPTION", "2026-01")


def _iso_to_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _inception_ms() -> int:
    y, m = (int(x) for x in LEDGER_INCEPTION.split("-"))
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def _scan_range(start_ms: int, end_ms: int):
    """Low-level: yield raw ledger items with time in [start_ms, end_ms]."""
    sk_start = f"{start_ms:020d}"
    sk_end = f"{end_ms:020d}~"
    for pk in _month_partitions(start_ms, end_ms):
        kwargs = {
            "KeyConditionExpression": "pk = :pk AND sk BETWEEN :a AND :b",
            "ExpressionAttributeValues": {":pk": pk, ":a": sk_start, ":b": sk_end},
        }
        while True:
            resp = ledger_table().query(**kwargs)
            for item in resp.get("Items", []):
                yield item
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _permanent_rows_ddb(as_of, dims=None):
    """Yield (account_id, account_type, side, total) for ASSET/LIABILITY/EQUITY, inception → as_of."""
    end_ms = _iso_to_ms(as_of)
    agg = defaultdict(float)
    for r in _scan_range(_inception_ms(), end_ms):
        if not _dims_match(r, dims):
            continue
        amt = float(r["amount"])
        if (r.get("debit_account_type") or "").upper() in ("ASSET", "LIABILITY", "EQUITY"):
            agg[(r["debit_account"], (r.get("debit_account_type") or "").upper(), "DEBIT")] += amt
        if (r.get("credit_account_type") or "").upper() in ("ASSET", "LIABILITY", "EQUITY"):
            agg[(r["credit_account"], (r.get("credit_account_type") or "").upper(), "CREDIT")] += amt
    for (account_id, account_type, side), total in agg.items():
        yield account_id, account_type, side, total


def _net_income_ddb(start, end, dims=None):
    start_ms = _iso_to_ms(start)
    end_ms = _iso_to_ms(end)
    revenue = 0.0
    expenses = 0.0
    for r in _scan_range(start_ms, end_ms):
        if not _dims_match(r, dims):
            continue
        amt = float(r["amount"])
        if (r.get("debit_account_type") or "").upper() == "REVENUE":
            revenue -= amt
        elif (r.get("credit_account_type") or "").upper() == "REVENUE":
            revenue += amt
        if (r.get("debit_account_type") or "").upper() == "EXPENSE":
            expenses += amt
        elif (r.get("credit_account_type") or "").upper() == "EXPENSE":
            expenses -= amt
    return revenue - expenses


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    as_of = body.get("asOf", _now_iso())
    period_start = body.get("periodStart")
    dims = body.get("dimensions")

    rows = _permanent_rows_ddb(as_of, dims)

    accounts = {}
    for account_id, account_type, side, total in rows:
        if account_id not in accounts:
            accounts[account_id] = {"account_type": account_type, "debits": 0.0, "credits": 0.0}
        accounts[account_id]["debits" if side == "DEBIT" else "credits"] = total

    assets = []
    liabilities = []
    equity = []

    for account_id, vals in accounts.items():
        entry = {"accountId": account_id, "debits": vals["debits"], "credits": vals["credits"]}
        if vals["account_type"] == "ASSET":
            entry["balance"] = entry["debits"] - entry["credits"]
            assets.append(entry)
        elif vals["account_type"] == "LIABILITY":
            entry["balance"] = entry["credits"] - entry["debits"]
            liabilities.append(entry)
        else:
            entry["balance"] = entry["credits"] - entry["debits"]
            equity.append(entry)

    if period_start:
        net_income = _net_income_ddb(period_start, as_of, dims)
        equity.append({
            "accountId": "RETAINED_EARNINGS_CURRENT_PERIOD",
            "debits": 0.0,
            "credits": net_income if net_income >= 0 else 0.0,
            "balance": net_income,
        })

    return {
        "statusCode": 200,
        "body": json.dumps({
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "asOf": as_of,
            # a filtered sheet covers only STAMPED entries — it is a slice view and need not balance
            **({"dimensions": dims} if dims else {}),
        }),
    }
