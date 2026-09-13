"""
Synthesize random balanced journal entries for accounting module query testing.

All randomness, scenario selection, and chart-of-accounts loading live here.
The canonical event→entry shape comes from transform.py — seed.py just calls
those transforms with random inputs.

Entries are enriched with `accountType` so they skip the pending queue and go
straight to Timestream. Output is JSON Lines on stdout by default.

Usage:
  python seed.py --count 200 --days 30
  python seed.py --count 50 --out entries.jsonl --seed 42
"""

import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "modules" / "accounting" / "lambdas" / "ingest"))

from transform import (  # noqa: E402
    transform_sale,
    transform_refund,
    transform_payout,
    transform_expense,
    transform_wage,
)

# Test fixture's own copy of the chart of accounts. The platform-wide canonical
# registry lives at modules/schemas/data/chart_of_accounts.json. This helper
# runs offline against jsonl stores and doesn't need the live registry — just
# a working set wide enough to generate realistic seed transactions.
ACCOUNTS = {
    "asset":     ["CASH", "CASH_IN_TRANSIT_STRIPE", "CASH_IN_TRANSIT_SQUARE", "CASH_IN_TRANSIT_PAYPAL",
                  "ACCOUNTS_RECEIVABLE", "ACCOUNTS_RECEIVABLE_STRIPE", "ACCOUNTS_RECEIVABLE_SQUARE",
                  "ACCOUNTS_RECEIVABLE_PAYPAL", "INVENTORY", "PREPAID_EXPENSES", "FIXED_ASSETS"],
    "liability": ["ACCOUNTS_PAYABLE", "WAGES_PAYABLE", "DIVIDENDS_PAYABLE", "ACCRUED_LIABILITIES",
                  "UNEARNED_REVENUE", "NOTES_PAYABLE"],
    "equity":    ["OWNER_EQUITY", "RETAINED_EARNINGS"],
    "revenue":   ["SALES_REVENUE", "SERVICE_REVENUE", "OTHER_INCOME"],
    "expense":   ["COST_OF_GOODS_SOLD", "WAGES_EXPENSE", "RENT_EXPENSE", "UTILITIES_EXPENSE",
                  "SUPPLIES_EXPENSE", "DEPRECIATION_EXPENSE", "INSURANCE_EXPENSE", "OTHER_EXPENSE"],
}

PROCESSORS = ["CASH_IN_TRANSIT_STRIPE", "CASH_IN_TRANSIT_SQUARE", "CASH_IN_TRANSIT_PAYPAL"]

SALE_MEMOS    = ["drip coffee", "latte", "cortado", "americano", "cold brew",
                 "pastry", "sandwich", "cookie", "muffin", "bagel"]
REFUND_MEMOS  = ["wrong order", "customer complaint", "duplicate charge"]
WAGE_NAMES    = ["maria", "jose", "chris", "pat", "alex"]

EXPENSE_ACCOUNTS = {
    "SUPPLIES_EXPENSE":   {"amount": (2000, 15000),   "memos": ["cups", "lids", "napkins", "cleaning supplies"]},
    "UTILITIES_EXPENSE":  {"amount": (5000, 40000),   "memos": ["electric", "water", "gas", "internet"]},
    "RENT_EXPENSE":       {"amount": (200000, 500000), "memos": ["monthly rent"]},
    "COST_OF_GOODS_SOLD": {"amount": (10000, 80000),  "memos": ["coffee beans", "milk", "syrups", "pastries"]},
}

WEIGHTS = [("sale", 60), ("refund", 3), ("payout", 5), ("expense", 20), ("wage", 12)]


def load_accounts():
    return {name: group.upper() for group, names in ACCOUNTS.items() for name in names}


def rand_amount(lo_cents, hi_cents):
    return round(random.randint(lo_cents, hi_cents) / 100.0, 2)


def rand_timestamp_millis(start_dt, end_dt):
    delta_s = (end_dt - start_dt).total_seconds()
    dt = start_dt + timedelta(seconds=random.uniform(0, delta_s))
    return str(int(dt.timestamp() * 1000))


def weighted_pick():
    total = sum(w for _, w in WEIGHTS)
    r = random.uniform(0, total)
    upto = 0
    for name, w in WEIGHTS:
        upto += w
        if r <= upto:
            return name
    return WEIGHTS[-1][0]


def new_id(name):
    return f"seed_{name}_{uuid.uuid4().hex[:12]}"


def build_random_entry(timestamp_millis):
    name = weighted_pick()
    if name == "sale":
        entry = transform_sale(
            entry_id=new_id(name), timestamp=timestamp_millis, source=f"seed.{name}",
            memo=random.choice(SALE_MEMOS),
            amount=rand_amount(200, 5000),
            processor=random.choice(PROCESSORS),
            revenue=random.choice(["SALES_REVENUE", "SERVICE_REVENUE"]),
        )
    elif name == "refund":
        entry = transform_refund(
            entry_id=new_id(name), timestamp=timestamp_millis, source=f"seed.{name}",
            memo="refund: " + random.choice(REFUND_MEMOS),
            amount=rand_amount(200, 3000),
            processor=random.choice(PROCESSORS),
            revenue="SALES_REVENUE",
        )
    elif name == "payout":
        processor = random.choice(PROCESSORS)
        entry = transform_payout(
            entry_id=new_id(name), timestamp=timestamp_millis, source=f"seed.{name}",
            memo=f"payout from {processor.replace('CASH_IN_TRANSIT_', '').lower()}",
            amount=rand_amount(10000, 200000),
            processor=processor, cash="CASH",
        )
    elif name == "expense":
        expense_account = random.choice(list(EXPENSE_ACCOUNTS))
        cfg = EXPENSE_ACCOUNTS[expense_account]
        entry = transform_expense(
            entry_id=new_id(name), timestamp=timestamp_millis, source=f"seed.{name}",
            memo=random.choice(cfg["memos"]),
            amount=rand_amount(*cfg["amount"]),
            expense_account=expense_account, cash="CASH",
        )
    elif name == "wage":
        entry = transform_wage(
            entry_id=new_id(name), timestamp=timestamp_millis, source=f"seed.{name}",
            memo="wages - " + random.choice(WAGE_NAMES),
            amount=rand_amount(40000, 200000),
            wages_expense="WAGES_EXPENSE", cash="CASH",
        )
    return entry


def classify(entry, accounts):
    for li in entry["lineItems"]:
        li["accountType"] = accounts[li["account"]]
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--days",  type=int, default=30, help="spread entries over last N days")
    parser.add_argument("--out",   default="-", help="output file path or '-' for stdout")
    parser.add_argument("--seed",  type=int, default=None, help="PRNG seed for reproducibility")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    accounts = load_accounts()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=args.days)

    entries = [classify(build_random_entry(rand_timestamp_millis(start_dt, end_dt)), accounts)
               for _ in range(args.count)]
    entries.sort(key=lambda e: e["timestamp"])

    fh = sys.stdout if args.out == "-" else open(args.out, "w")
    try:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    finally:
        if fh is not sys.stdout:
            fh.close()


if __name__ == "__main__":
    main()
