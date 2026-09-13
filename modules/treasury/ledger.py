"""modules/treasury — reading the ledger as a projection.

Treasury asks the books two questions and both are folds over the same rows, sliced by the same
dimension:

    how much has this instrument PAID OUT           DIVIDENDS_PAYABLE credits, dimension instrument_id
    how much has this holding PAID BACK to us       INVESTMENT_INCOME credits, same dimension

Nothing stores either number. That is deliberate and it is the module's whole shape — an instrument
is a rule instance and a holding is not an object at all, so a cap that "has 50k left" and a
portfolio line that "has taken 320k" are both arithmetic over entries, not counters somebody has to
remember to advance. A stored total is a second thing that can disagree with the ledger, and the
ledger is the one that is right.

Partitioned by `YYYY-MM` (accounting's ledger `pk`), so a fold walks months from `LEDGER_INCEPTION`
forward rather than scanning.
"""

import decimal as _decimal
import os as _os

from boto3.dynamodb.conditions import Key as _Key

from aws import table as _ddb_table

LEDGER_INCEPTION = _os.environ.get("LEDGER_INCEPTION", "2026-01")   # first month a fold walks from


def _table():
    return _ddb_table(_os.environ["LEDGER_TABLE"])


def months(start_month: str, end_month: str):
    """`YYYY-MM` partition keys, start..end inclusive."""
    y, m = (int(x) for x in start_month.split("-"))
    ey, em = (int(x) for x in end_month.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m = m + 1 if m < 12 else 1
        if m == 1:
            y += 1


def partition(month: str):
    """Every ledger pair-row in one `YYYY-MM` partition."""
    kwargs = {"KeyConditionExpression": _Key("pk").eq(month)}
    while True:
        resp = _table().query(**kwargs)
        yield from resp.get("Items", [])
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def _dims(row) -> dict:
    """A ledger row's dimensions, whichever field they landed in.

    post_journal_entry SPLITS a caller's one `dimensions` map at the write: keys that describe the
    transaction go to `dims`, person references to `dims_private`. Treasury's keys (instrument_id,
    holder, rule, period) all describe the transaction, so they land in `dims` — reading only
    `dimensions` matched nothing on a real posted row, and every fold returned zero. `dimensions`
    stays in the union for rows written before the split.
    """
    return {**(row.get("dimensions") or {}),
            **(row.get("dims") or {}),
            **(row.get("dims_private") or {})}


def fold_credits(account: str, dim: str, value: str, upto_month: str, before=None) -> _decimal.Decimal:
    """Σ of `account` CREDITS whose `dimensions[dim] == value`, walking to `upto_month`.

    Credits only, and that asymmetry is load-bearing on both callers: a distribution DECLARES with
    `CR DIVIDENDS_PAYABLE` and SETTLES with `DR DIVIDENDS_PAYABLE / CR CASH`, so counting debits
    would net the declaration back out and a cap would never be reached. The same holds on the buy
    side — income is credited, its later cash settlement is not.

    `before` optionally excludes entries at or after a period (the cap fold needs strictly-prior
    payouts, or an instrument's own current-period entry would count against its own cap)."""
    total = _decimal.Decimal(0)
    for month in months(LEDGER_INCEPTION, upto_month):
        for row in partition(month):
            if row.get("credit_account") != account:
                continue
            dims = _dims(row)
            if dims.get(dim) != value:
                continue
            if before is not None and str(dims.get("period") or "") >= before:
                continue
            total += _decimal.Decimal(str(row.get("amount", 0)))
    return total


def fold_debits(account: str, dim: str, value: str, upto_month: str) -> _decimal.Decimal:
    """Σ of `account` DEBITS on a dimension — what a holding COST, since the buy-side money leg is
    `DR INVESTMENTS / CR CASH`."""
    total = _decimal.Decimal(0)
    for month in months(LEDGER_INCEPTION, upto_month):
        for row in partition(month):
            if row.get("debit_account") != account:
                continue
            dims = _dims(row)
            if dims.get(dim) != value:
                continue
            total += _decimal.Decimal(str(row.get("amount", 0)))
    return total
