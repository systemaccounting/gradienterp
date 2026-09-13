"""The demo fixture — a small coffee shop, seeded via the migration walk + two months of
operating activity so the login-page demos record against lively, coherent books.

A consumer of the GENERAL toolkit `scripts/seed_dev.py` (the reusable half; e2e/integration tests
import the same helpers to build their own state). Run AFTER `bash scripts/reset-dev.sh --yes`:

  .venv/bin/python docs/demos/seed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import seed_dev as s  # noqa: E402  the general seeding toolkit

CUTOVER = "2026-05-31"
JUN, JUL = "2026-06-15T12:00:00Z", "2026-07-15T12:00:00Z"

CUSTOMERS = [("riverside-catering", "Riverside Catering", 2650), ("delta-films", "Delta Films Craft Services", 2000),
             ("lofthouse-office", "Lofthouse Office", 1500), ("summit-coworking", "Summit Coworking", 1250),
             ("nguyen-events", "Nguyen Events", 1000), ("maple-daycare", "Maple Street Daycare", 750)]
VENDORS = [("blue-ridge-roasters", "Blue Ridge Roasters"), ("valley-dairy", "Valley Dairy"),
           ("restaurant-depot", "Restaurant Depot")]
WORKERS = [("ava-reyes", "Ava", "Reyes", "barista", 19), ("ben-osei", "Ben", "Osei", "barista", 18),
           ("cora-vance", "Cora", "Vance", "baker", 22), ("deo-park", "Deo", "Park", "shift_lead", 24)]
ITEMS = [("beans", "Espresso Beans", "bag", 60.00, 0, 12), ("milk", "Whole Milk", "gal", 4.00, 0, 40),
         ("oat-milk", "Oat Milk", "half-gal", 4.50, 0, 20), ("cups-12oz", "12oz Cups", "ea", 0.12, 0, 3000),
         ("lids", "Lids", "ea", 0.05, 0, 3000), ("croissant", "Croissant", "ea", 1.20, 4.00, 24),
         ("syrup-vanilla", "Vanilla Syrup", "btl", 8.00, 0, 12), ("cold-brew", "Cold Brew", "gal", 18.00, 0, 8),
         ("retail-beans", "Retail Beans 12oz", "bag", 6.00, 16.00, 30), ("tea", "Loose Tea", "box", 6.50, 0, 10)]
ASSETS = [("espresso machine", "machinery_equipment", 9500), ("burr grinder", "machinery_equipment", 2200),
          ("cafe furniture", "furniture_fixtures", 4000)]
CASH_OPEN, LOAN_OPEN = 25000, 12000


def main():
    print("-- Phase A: the migration walk (opening state) --")
    for cid, name, _ in CUSTOMERS:
        s.contact(cid, name, is_customer=True)
    for cid, name in VENDORS:
        s.contact(cid, name, is_vendor=True)
    for cid, first, last, role, rate in WORKERS:
        s.contact(cid, first=first, last=last, is_employee=True)
        s.worker(cid, role, rate)
    print(f"  contacts: {len(CUSTOMERS)} customers, {len(VENDORS)} vendors, {len(WORKERS)} workers")

    # items + opening counts valued to equity via a temporary STOCK_ADJUSTED#* rule (the walk)
    s.add_rule("STOCK_ADJUSTED#*", "value_adjustment", "opening_counts", 100,
               {"account": "OWNER_EQUITY", "account_type": "EQUITY"})
    inv_value = 0
    for sku, name, unit, cost, price, qty in ITEMS:
        s.item(sku, name, unit, cost, price)
        s.move(sku, qty, entry_id=f"opening-count-1#{sku}", memo=f"opening count {CUTOVER}")
        inv_value += cost * qty
    s.del_rule("STOCK_ADJUSTED#*", "opening_counts", 100)
    print(f"  items: {len(ITEMS)}, opening inventory ${inv_value:,.2f}")

    # open AR / AP — lines to OWNER_EQUITY (the walk's double-count rule)
    riv_open = s.invoice("riverside-catering", 850, "OWNER_EQUITY", "EQUITY", due="2026-06-15",
                         memo="migrated open invoice orig 2026-05-20")
    s.invoice("lofthouse-office", 600, "OWNER_EQUITY", "EQUITY", due="2026-06-20",
              memo="migrated open invoice orig 2026-05-25")
    s.bill("blue-ridge-roasters", 700, "OWNER_EQUITY", "EQUITY", memo="migrated unpaid bill 2026-05-22")
    print("  open AR: 2 invoices ($1,450) · open AP: 1 bill ($700)")

    fixed = sum(c for _, _, c in ASSETS)
    for name, klass, cost in ASSETS:
        s.asset(name, klass, cost)
    equity = CASH_OPEN + fixed - LOAN_OPEN
    s.je(f"opening-{CUTOVER}", f"{CUTOVER}T00:00:00Z", f"opening balances {CUTOVER}", [
        ("CASH", "ASSET", "DEBIT", CASH_OPEN), ("FIXED_ASSETS", "ASSET", "DEBIT", fixed),
        ("NOTES_PAYABLE", "LIABILITY", "CREDIT", LOAN_OPEN), ("OWNER_EQUITY", "EQUITY", "CREDIT", equity)])
    print(f"  assets: {len(ASSETS)} (${fixed:,}) · opening entry → equity ${equity:,}")

    print("\n-- Phase B: operating activity (June + July) --")
    for cid, _, spend in CUSTOMERS:
        s.sale(cid, spend, memo="coffee service")
    s.pay_invoice(riv_open)  # collect a migrated AR → cash
    print(f"  B2B sales: {len(CUSTOMERS)} customers, ${sum(c[2] for c in CUSTOMERS):,} collected")

    ops = [
        ("retail-jun", JUN, "June retail sales", [("CASH", "ASSET", "DEBIT", 18000), ("SALES_REVENUE", "REVENUE", "CREDIT", 18000)]),
        ("retail-jul", JUL, "July retail sales", [("CASH", "ASSET", "DEBIT", 11000), ("SALES_REVENUE", "REVENUE", "CREDIT", 11000)]),
        ("restock-jun", JUN, "June restock", [("INVENTORY", "ASSET", "DEBIT", 7000), ("CASH", "ASSET", "CREDIT", 7000)]),
        ("restock-jul", JUL, "July restock", [("INVENTORY", "ASSET", "DEBIT", 5000), ("CASH", "ASSET", "CREDIT", 5000)]),
        ("cogs-jun", JUN, "June COGS", [("COST_OF_GOODS_SOLD", "EXPENSE", "DEBIT", 6600), ("INVENTORY", "ASSET", "CREDIT", 6600)]),
        ("cogs-jul", JUL, "July COGS", [("COST_OF_GOODS_SOLD", "EXPENSE", "DEBIT", 4900), ("INVENTORY", "ASSET", "CREDIT", 4900)]),
        ("wages-jun", JUN, "June wages", [("WAGES_EXPENSE", "EXPENSE", "DEBIT", 5200), ("CASH", "ASSET", "CREDIT", 5200)]),
        ("wages-jul", JUL, "July wages", [("WAGES_EXPENSE", "EXPENSE", "DEBIT", 5000), ("CASH", "ASSET", "CREDIT", 5000)]),
        ("rent-jun", JUN, "June rent", [("RENT_EXPENSE", "EXPENSE", "DEBIT", 3500), ("CASH", "ASSET", "CREDIT", 3500)]),
        ("rent-jul", JUL, "July rent", [("RENT_EXPENSE", "EXPENSE", "DEBIT", 3500), ("CASH", "ASSET", "CREDIT", 3500)]),
        ("util-jun", JUN, "June utilities", [("UTILITIES_EXPENSE", "EXPENSE", "DEBIT", 600), ("CASH", "ASSET", "CREDIT", 600)]),
        ("util-jul", JUL, "July utilities", [("UTILITIES_EXPENSE", "EXPENSE", "DEBIT", 600), ("CASH", "ASSET", "CREDIT", 600)]),
        ("ins-jun", JUN, "June insurance", [("INSURANCE_EXPENSE", "EXPENSE", "DEBIT", 300), ("CASH", "ASSET", "CREDIT", 300)]),
        ("ins-jul", JUL, "July insurance", [("INSURANCE_EXPENSE", "EXPENSE", "DEBIT", 300), ("CASH", "ASSET", "CREDIT", 300)]),
        ("supp-jun", JUN, "June supplies", [("SUPPLIES_EXPENSE", "EXPENSE", "DEBIT", 400), ("CASH", "ASSET", "CREDIT", 400)]),
        ("supp-jul", JUL, "July supplies", [("SUPPLIES_EXPENSE", "EXPENSE", "DEBIT", 400), ("CASH", "ASSET", "CREDIT", 400)]),
    ]
    for entry_id, ts, memo, lines in ops:
        s.je(entry_id, ts, memo, lines)
    print(f"  operating entries: {len(ops)}")

    print("\n-- verify --")
    s.check()


if __name__ == "__main__":
    main()
