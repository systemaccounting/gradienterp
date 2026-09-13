import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from aws import client as _aws, table as _table

GENERATE_REPORT_FN = os.environ.get("GENERATE_REPORT_FN", "")
DISTRIBUTION_FN = os.environ.get("DISTRIBUTION_FN", "")


def balances_table():
    return _table(os.environ["BALANCES_TABLE"])


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


REPORTING_STANDARD = os.environ.get("REPORTING_STANDARD", "true").lower() == "true"
DEBIT_NORMAL = {"ASSET", "EXPENSE"}


# The first month the ledger can hold anything. A read reaches back to here and no further: the
# ledger is partitioned by `YYYY-MM` and a fold QUERIES each partition, so flooring an absent start
# at epoch 0 meant walking every month since 1970 — hundreds of queries for one inception-to-date
# compute, growing by one every month. Same constant treasury's folds use.
LEDGER_INCEPTION = os.environ.get("LEDGER_INCEPTION", "2026-01")


def _iso_to_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _inception_ms() -> int:
    y, m = (int(x) for x in LEDGER_INCEPTION.split("-"))
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _get_last_checkpoint():
    resp = balances_table().scan(
        FilterExpression="account_id = :re AND #s = :true",
        ExpressionAttributeNames={"#s": "standard"},
        ExpressionAttributeValues={":re": "RETAINED_EARNINGS_CUMULATIVE", ":true": True},
    )
    items = resp["Items"]
    if not items:
        return None
    latest = max(items, key=lambda x: x["period_end"])
    return {"period_end": latest["period_end"], "retained_earnings": float(latest.get("balance", 0))}


def _get_checkpoint_balances(period_end):
    resp = balances_table().query(
        KeyConditionExpression="period_end = :pe",
        ExpressionAttributeValues={":pe": period_end},
    )
    items = resp["Items"]
    balances = {}
    for item in items:
        aid = item["account_id"]
        if aid == "RETAINED_EARNINGS_CUMULATIVE":
            continue
        balances[aid] = {
            "account_type": item["account_type"],
            "debits": float(item["debits"]),
            "credits": float(item["credits"]),
            "balance": float(item["balance"]),
        }
    return balances


def _month_partitions(start_ms: int, end_ms: int):
    s = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    e = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        yield f"{y:04d}-{m:02d}"
        m = m + 1 if m < 12 else 1
        if m == 1:
            y += 1


def _query_ddb(start, end, accounts):
    """Aggregate ledger pair rows over (start, end], filtered to `accounts` set if non-empty.

    Note: inclusive-right, exclusive-left — matches the prior Timestream
    semantic so checkpoint-to-now aggregation doesn't double-count the
    checkpoint boundary row.
    """
    start_ms = _iso_to_ms(start) if start else _inception_ms()
    end_ms = _iso_to_ms(end) if end else int(time.time() * 1000)
    sk_start = f"{start_ms:020d}~"   # '~' > '#' ensures strictly-greater-than start
    sk_end = f"{end_ms:020d}~"
    accts_set = set(accounts) if accounts else None
    results = {}
    for pk in _month_partitions(start_ms, end_ms):
        kwargs = {
            "KeyConditionExpression": "pk = :pk AND sk BETWEEN :a AND :b",
            "ExpressionAttributeValues": {":pk": pk, ":a": sk_start, ":b": sk_end},
        }
        while True:
            resp = ledger_table().query(**kwargs)
            for r in resp.get("Items", []):
                amt = float(r["amount"])
                for acct_key, type_key, side in (
                    ("debit_account", "debit_account_type", "DEBIT"),
                    ("credit_account", "credit_account_type", "CREDIT"),
                ):
                    account_id = r[acct_key]
                    if accts_set and account_id not in accts_set:
                        continue
                    if account_id not in results:
                        results[account_id] = {"account_type": r[type_key], "debits": 0.0, "credits": 0.0}
                    if side == "DEBIT":
                        results[account_id]["debits"] += amt
                    else:
                        results[account_id]["credits"] += amt
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return results


def _merge_balances(prior, delta):
    merged = {}
    for aid, vals in prior.items():
        if vals["account_type"] in {"ASSET", "LIABILITY", "EQUITY"}:
            merged[aid] = dict(vals)
    for aid, vals in delta.items():
        if aid in merged:
            merged[aid]["debits"] += vals["debits"]
            merged[aid]["credits"] += vals["credits"]
        else:
            merged[aid] = dict(vals)
        if vals["account_type"] in DEBIT_NORMAL:
            merged[aid]["balance"] = merged[aid]["debits"] - merged[aid]["credits"]
        else:
            merged[aid]["balance"] = merged[aid]["credits"] - merged[aid]["debits"]
    return merged


def _compute_net_income(delta):
    revenue = 0.0
    expenses = 0.0
    for vals in delta.values():
        if vals["account_type"] == "REVENUE":
            revenue += vals["credits"] - vals["debits"]
        elif vals["account_type"] == "EXPENSE":
            expenses += vals["debits"] - vals["credits"]
    return revenue - expenses


def _write_balances(period_end, period_start, balances, cumulative_re, standard):
    now = _now_iso()
    rows = []
    for aid, vals in balances.items():
        rows.append({
            "period_end": period_end,
            "account_id": aid,
            "account_type": vals["account_type"],
            "debits": round(vals["debits"], 2),
            "credits": round(vals["credits"], 2),
            "balance": round(vals["balance"], 2),
            "period_start": period_start,
            "standard": standard,
            "computed_at": now,
        })
    rows.append({
        "period_end": period_end,
        "account_id": "RETAINED_EARNINGS_CUMULATIVE",
        "account_type": "EQUITY",
        "debits": 0.0,
        "credits": round(cumulative_re, 2),
        "balance": round(cumulative_re, 2),
        "period_start": period_start,
        "standard": standard,
        "computed_at": now,
    })

    with balances_table().batch_writer() as batch:
        for r in rows:
            batch.put_item(Item={
                **{k: v for k, v in r.items() if k not in ("debits", "credits", "balance")},
                "debits": Decimal(str(r["debits"])),
                "credits": Decimal(str(r["credits"])),
                "balance": Decimal(str(r["balance"])),
            })


def _invoke_generate_report(payload):
    if GENERATE_REPORT_FN:
        _aws("lambda").invoke(
            FunctionName=GENERATE_REPORT_FN,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )


def _invoke_distribution(payload):
    """Fire treasury's distribution handler with the period's net income — the durable
    period-close trigger (async `Event`, retried by Lambda). Guarded by DISTRIBUTION_FN, which
    a harness leaves unset: treasury is exercised by its own suite, so a compute_balances test
    stays decoupled from it."""
    if DISTRIBUTION_FN:
        _aws("lambda").invoke(
            FunctionName=DISTRIBUTION_FN,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    accounts = body.get("accounts", [])
    time_range = body.get("range", {})
    standard = body.get("standard", REPORTING_STANDARD)

    period_end = time_range.get("end", _now_iso())

    last_checkpoint = _get_last_checkpoint()

    if last_checkpoint and not time_range.get("start"):
        checkpoint_end = last_checkpoint["period_end"]
        query_start = checkpoint_end
        prior_balances = _get_checkpoint_balances(checkpoint_end)
        prior_re = float(last_checkpoint.get("retained_earnings", 0))
    else:
        query_start = time_range.get("start")
        prior_balances = {}
        prior_re = 0.0

    delta = _query_ddb(query_start, period_end, accounts)
    balances = _merge_balances(prior_balances, delta)
    net_income = _compute_net_income(delta)
    cumulative_re = prior_re + net_income
    period_start = query_start or "INCEPTION"

    _write_balances(period_end, period_start, balances, cumulative_re, standard)
    _invoke_generate_report({"periodEnd": period_end, "standard": standard})
    _invoke_distribution({"periodEnd": period_end, "netIncome": net_income})

    return {
        "statusCode": 200,
        "body": json.dumps({
            "periodEnd": period_end,
            "accountsComputed": len(balances),
            "standard": standard,
            "retainedEarnings": cumulative_re,
        }),
    }
