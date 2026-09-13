"""tests/seed — populate a DEPLOYED gerp with a coherent business narrative, and reset it cleanly.

Drives the REAL entry-point lambda (accounting's post_journal_entry today) so data is prod-shaped and
cascades to the ledger + the economic counter (→ LIVE GDP) with no bespoke wiring. EVERY seeded record is
tagged `source=seed` so `--teardown` removes exactly the seed and nothing else — build the undo with the do.

  python tests/seed/run.py                    # seed gradienterp (default 90-day window)
  python tests/seed/run.py --teardown         # remove everything the seed created (ledger rows + counters)
  python tests/seed/run.py --window 30 --rng-seed 7 --scale 1.0

Distinct from helpers/seed.py (offline, random, jsonl). This is deployed + coherent. See tests/TODO.md.
"""

import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import boto3

REGION = "us-east-1"
CUSTOMER_PROFILE = "gerp-gradienterp"  # the gerp's own account (post_journal_entry + ledger)
OPERATOR_PROFILE = "operator-org"                   # operator account (the economic counters)
SEED_SOURCE = "seed"                                # the teardown tag, on the ledger row's `source`

_sessions = {}


def _client(profile, svc):
    if profile not in _sessions:
        _sessions[profile] = boto3.Session(profile_name=profile, region_name=REGION)
    return _sessions[profile].client(svc)


def _pje_fn(gerp_id):
    return f"gerp-accounting-{gerp_id.replace('_', '-')}-post_journal_entry"


def _ledger_table(gerp_id):
    return f"gerp-accounting-{gerp_id.replace('_', '-')}-ledger"


def _inv_fn(gerp_id, name):
    return f"gerp-inventory-{gerp_id.replace('_', '-')}-{name}"


def _inv_table(gerp_id):
    return f"gerp-inventory-{gerp_id.replace('_', '-')}-items"


# ── the business narrative (a coffee roaster/cafe) ──────────────────────────────
#
# A believable story, not random: revenue trends up across the window with weekday weighting; COGS + opex
# are sized so gross margin ~55% and net ~20%. Getting the story right is what makes the dashboard read as
# a real business (and, later, the retained-earnings curve fall out for free).

def _entries(window_days, scale, rng):
    """Yield (dt, memo, line_items) for each posting across the window ending today."""
    today = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=window_days - 1)

    for i in range(window_days):
        d = start + timedelta(days=i)
        growth = 1.0 + 0.7 * (i / max(1, window_days - 1))          # ~+70% across the window
        weekday = 1.1 if d.weekday() < 5 else 0.65                  # heavier Mon–Fri (office coffee)
        rev = round(820 * scale * growth * weekday * (0.9 + 0.2 * rng.random()), 2)
        cogs = round(rev * 0.45, 2)                                 # ~55% gross margin
        yield d, "daily sales", [
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": rev},
            {"account": "SALES_REVENUE", "accountType": "REVENUE", "side": "CREDIT", "amount": rev},
        ]
        yield d, "cogs recognized", [
            {"account": "COST_OF_GOODS_SOLD", "accountType": "EXPENSE", "side": "DEBIT", "amount": cogs},
            {"account": "INVENTORY", "accountType": "ASSET", "side": "CREDIT", "amount": cogs},
        ]
        # rent + utilities on the 1st; wages every other Friday
        if d.day == 1:
            yield d, "rent", [
                {"account": "RENT_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": 3500.0},
                {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": 3500.0},
            ]
            yield d, "utilities", [
                {"account": "UTILITIES_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": round(540 + 120 * rng.random(), 2)},
                {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": None},  # filled below
            ]
        if d.weekday() == 4 and (i // 7) % 2 == 0:
            wage = round(2900 * scale, 2)
            yield d, "payroll run", [
                {"account": "WAGES_EXPENSE", "accountType": "EXPENSE", "side": "DEBIT", "amount": wage},
                {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": wage},
            ]


def _balance(line_items):
    """Fill any None credit amount to balance debits (used for the utilities pair)."""
    dr = sum(li["amount"] for li in line_items if li["side"] == "DEBIT" and li["amount"] is not None)
    for li in line_items:
        if li["amount"] is None:
            li["amount"] = dr
    return line_items


# ── the operational side (inventory on hand) ────────────────────────────────────
#
# The same coffee roaster/cafe, seen from the ops surface. manage_stock create_item then a RECEIVED movement to set
# opening stock — the RECEIVED posts DR INVENTORY / CR ACCOUNTS_PAYABLE tagged source=seed, so those ledger
# rows are removed by the ledger teardown; teardown_inventory only has to drop the item rows themselves.
# (item_id, name, unit, unit_cost, unit_price, opening_stock)

CATALOG = [
    ("green_ethiopia", "Green Coffee — Ethiopia Yirgacheffe", "lb",   4.20,  0.00, 640),
    ("green_colombia", "Green Coffee — Colombia Huila",       "lb",   3.80,  0.00, 520),
    ("roast_house",    "House Roast · 12oz bag",              "each", 6.50, 16.00, 180),
    ("roast_decaf",    "Decaf Roast · 12oz bag",              "each", 6.80, 16.00,  90),
    ("oat_milk",       "Oat Milk · 32oz",                     "each", 1.90,  0.00, 120),
    ("whole_milk",     "Whole Milk",                          "gal",  3.20,  0.00,  45),
    ("cups_12oz",      "To-go Cups · 12oz",                   "each", 0.08,  0.00, 4200),
    ("croissant",      "Butter Croissant",                    "each", 0.90,  4.50,  60),
    ("mug_ceramic",    "Ceramic Mug · logo",                  "each", 3.50, 14.00,  48),
    ("filters",        "Paper Filters · #4",                  "each", 0.03,  0.00, 3000),
]


def seed_inventory(gerp_id):
    lam = _client(CUSTOMER_PROFILE, "lambda")
    created = 0
    for item_id, name, unit, cost, price, stock in CATALOG:
        payload = {"op": "create_item", "item_id": item_id, "name": name, "unit": unit, "unit_cost": cost, "unit_price": price}
        lam.invoke(FunctionName=_inv_fn(gerp_id, "manage_stock"), Payload=json.dumps(payload).encode())
        # opening stock as a RECEIVED movement (posts DR INVENTORY / CR AP, tagged source=seed)
        rcv = {"item_id": item_id, "quantity": stock, "movement_type": "RECEIVED",
               "source": SEED_SOURCE, "memo": "opening stock"}
        r = lam.invoke(FunctionName=_inv_fn(gerp_id, "manage_stock"), Payload=json.dumps({"op": "move", **rcv}).encode())
        body = json.loads(r["Payload"].read() or b"{}")
        if r.get("FunctionError") or (isinstance(body, dict) and body.get("statusCode", 200) >= 400):
            print(f"  ! inventory failed ({item_id}): {str(body)[:200]}")
            continue
        created += 1
    print(f"seeded {created} inventory SKUs across {gerp_id} (opening stock → ops dashboard)")


def teardown_inventory(gerp_id):
    ddb = _client(CUSTOMER_PROFILE, "dynamodb")
    table = _inv_table(gerp_id)
    dropped = 0
    for item_id, *_ in CATALOG:
        ddb.delete_item(TableName=table, Key={"item_id": {"S": item_id}})
        dropped += 1
    return dropped


# ── seed / reset ────────────────────────────────────────────────────────────────

def seed(gerp_id, window_days, scale, rng_seed):
    rng = random.Random(rng_seed)
    lam = _client(CUSTOMER_PROFILE, "lambda")
    fn = _pje_fn(gerp_id)
    posted = 0
    months = set()
    for dt, memo, line_items in _entries(window_days, scale, rng):
        _balance(line_items)
        months.add(dt.strftime("%Y-%m"))
        payload = {
            "lineItems": line_items,
            "memo": memo,
            "source": SEED_SOURCE,
            "timestamp": str(int(dt.timestamp() * 1000)),
            "entryId": f"seed-{uuid.uuid4().hex[:12]}",
        }
        r = lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
        body = json.loads(r["Payload"].read() or b"{}")
        if r.get("FunctionError") or (isinstance(body, dict) and body.get("statusCode", 200) >= 400):
            print(f"  ! post failed ({memo} {dt.date()}): {str(body)[:200]}")
            continue
        posted += 1
    print(f"seeded {posted} entries across {gerp_id} — months {sorted(months)} (LIVE GDP ticks via the bus)")
    seed_inventory(gerp_id)


def teardown(gerp_id, window_days):
    ddb = _client(CUSTOMER_PROFILE, "dynamodb")
    table = _ledger_table(gerp_id)
    # 1) delete every ledger row tagged source=seed
    deleted, start_key = 0, None
    while True:
        kw = {"TableName": table, "FilterExpression": "#s = :seed",
              "ExpressionAttributeNames": {"#s": "source"}, "ExpressionAttributeValues": {":seed": {"S": SEED_SOURCE}}}
        if start_key:
            kw["ExclusiveStartKey"] = start_key
        page = ddb.scan(**kw)
        for it in page.get("Items", []):
            ddb.delete_item(TableName=table, Key={"pk": it["pk"], "sk": it["sk"]})
            deleted += 1
        start_key = page.get("LastEvalKey") or page.get("LastEvaluatedKey")
        if not start_key:
            break
    # 2) reset the economic counters the seed touched (revenue#<month>) — gradienterp is the sole contributor
    counters = _client(OPERATOR_PROFILE, "dynamodb")
    today = datetime.now(timezone.utc)
    reset = 0
    for k in {(today - timedelta(days=n)).strftime("%Y-%m") for n in range(window_days)}:
        counters.delete_item(TableName="gerp-counters", Key={"counter": {"S": f"revenue#{k}"}})
        reset += 1
    # 3) drop the seeded inventory items (their RECEIVED journal rows were already caught in step 1)
    dropped = teardown_inventory(gerp_id)
    print(f"teardown: deleted {deleted} seeded ledger rows, reset {reset} revenue counters, "
          f"dropped {dropped} inventory SKUs — gerp back to pre-seed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gerp-id", default="gradienterp")
    ap.add_argument("--window", type=int, default=90, help="days of history ending today")
    ap.add_argument("--scale", type=float, default=1.0, help="transaction volume multiplier")
    ap.add_argument("--rng-seed", type=int, default=7)
    ap.add_argument("--teardown", action="store_true", help="remove everything the seed created")
    a = ap.parse_args()
    if a.teardown:
        teardown(a.gerp_id, a.window)
    else:
        seed(a.gerp_id, a.window, a.scale, a.rng_seed)


if __name__ == "__main__":
    main()
