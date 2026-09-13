import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from aws import table as _table


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


# The first month the ledger can hold anything. A read reaches back to here and no further: the
# ledger is partitioned by `YYYY-MM` and a fold QUERIES each partition, so flooring an absent start
# at epoch 0 meant walking every month since 1970 — 680 queries for one unranged trial balance,
# growing by one every month. Same constant treasury's folds use.
LEDGER_INCEPTION = os.environ.get("LEDGER_INCEPTION", "2026-01")


def _iso_to_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _inception_ms() -> int:
    y, m = (int(x) for x in LEDGER_INCEPTION.split("-"))
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _month_partitions(start_ms: int, end_ms: int):
    """Yield 'YYYY-MM' strings for every month-partition touched by [start_ms, end_ms]."""
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
    """Yield (debit_account, debit_account_type, credit_account, credit_account_type, amount) for each pair row in range."""
    start_ms = _iso_to_ms(time_range["start"]) if time_range.get("start") else _inception_ms()
    end_ms = _iso_to_ms(time_range["end"]) if time_range.get("end") else int(time.time() * 1000)
    sk_start = f"{start_ms:020d}"
    sk_end = f"{end_ms:020d}~"  # '~' > digits, bounds the range
    for pk in _month_partitions(start_ms, end_ms):
        kwargs = {
            "KeyConditionExpression": "pk = :pk AND sk BETWEEN :a AND :b",
            "ExpressionAttributeValues": {":pk": pk, ":a": sk_start, ":b": sk_end},
        }
        while True:
            resp = ledger_table().query(**kwargs)
            for item in resp.get("Items", []):
                if not _dims_match(item, dims):
                    continue
                yield (
                    item["debit_account"], item["debit_account_type"],
                    item["credit_account"], item["credit_account_type"],
                    float(item["amount"]),
                )
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _rows_from_ddb_tb(time_range, dims=None):
    """Adapt _rows_from_ddb to the (account_id, side, total) tuples the trial-balance aggregator expects."""
    agg = defaultdict(float)
    for d_acct, _d_t, c_acct, _c_t, amt in _rows_from_ddb(time_range, dims):
        agg[(d_acct, "DEBIT")] += amt
        agg[(c_acct, "CREDIT")] += amt
    for (account_id, side), total in agg.items():
        yield account_id, side, total


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    time_range = body.get("range", {})
    dims = body.get("dimensions")

    rows = _rows_from_ddb_tb(time_range, dims)

    accounts = {}
    for account_id, side, total in rows:
        if account_id not in accounts:
            accounts[account_id] = {"accountId": account_id, "debits": 0.0, "credits": 0.0}
        accounts[account_id]["debits" if side == "DEBIT" else "credits"] = total

    for acct in accounts.values():
        acct["balance"] = acct["debits"] - acct["credits"]

    return {
        "statusCode": 200,
        "body": json.dumps({
            "balances": list(accounts.values()),
            "asOf": time_range.get("end", _now_iso()),
            # a filtered trial balance covers only entries STAMPED with these dimensions
            **({"dimensions": dims} if dims else {}),
        }),
    }


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
