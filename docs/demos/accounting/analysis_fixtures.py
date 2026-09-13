"""demo 01 fixture — four exports a real cafe would actually have, filed in its cabinet.

    .venv/bin/python docs/demos/accounting/analysis_fixtures.py            # reset, generate, file, check
    .venv/bin/python docs/demos/accounting/analysis_fixtures.py --reset    # unfile them
    .venv/bin/python docs/demos/accounting/analysis_fixtures.py --check    # recordable right now?

The verbs, the safety properties and the no-side-doors rule are `docs/demos/fixture.py`.

Deterministic (fixed seed), so the expected answers below hold on every regeneration and the demo's
numbers can be CHECKED rather than trusted. CSVs land in `fixtures/` beside this file, then get
FILED through `manage_storage op=file`.

Filing matters and used to be a manual `aws s3 cp`. That writes the bytes and not the caption
annotation, and `op=find` matches `query`/`tag` against the caption — so these five sat in the bucket
fully readable by exact key and invisible to every search the agent performs. A demo 03 take asked
for a POS export, was told "no POS export on file", and dropped its strongest beat while a 2.9 MB one
with a `served_by` column sat right there.

NOT part of scripts/seed_dev.py, deliberately. That toolkit writes DDB rows through the agent's own
tool shapes and its job is to leave the books COHERENT. These are third-party exports a cafe
RECEIVES — POS vendor, delivery app, payroll — and their job is to disagree with the books in ways
worth finding. Opposite goals, different substrate, no shared code.

Ground truth at seed 20260724 — RECOMPUTED FROM THE FILES 2026-07-24, not copied from a prior run:
    POS            38,385 rows · $191,788.80 revenue · COGS $55,060.99 · 71.3% gross
    AR             $82,835.29 open across 46 of 140 · 90+ bucket $21,910 · worst: Riverside Catering
    GL             $157,832.75 revenue

⚠️ These are NOT the numbers the recorded take (demo-analysis.webm, 19:02) shows. It read an earlier
generation; these fixtures were regenerated and re-uploaded at 19:50 after `served_by` was added to
the POS writer. That column consumes a `random.choices` draw per row, which shifts the whole RNG
stream, so every downstream figure moved (revenue $183,609 → $191,788.80, and the worst AR account
changed from Harbor Law Group to Riverside Catering). The gif is unaffected — it is already cut. But
a RE-RECORD will produce different figures than the take, and the demo README's "agent found" column
quotes the old ones. Recompute before trusting any number here against a new take.

FOUR FINDINGS ARE PLANTED HERE ON PURPOSE — each is a thing a human takes days to see, and the demo
is whether an open question ("where are we leaking money?") surfaces them. All four survive the
regeneration; only their magnitudes moved:

  1. LABOR vs DEMAND — shifts are scheduled flat (6a-2p / 10a-6p) regardless of revenue, so the
     open and the late-morning lull cost more in wages than they earn:
         06:00 labor/rev 96.5% · 11:00 100.0% · versus 08:00 at 22.2%
  2. THE DELIVERY RAKE — the POS records menu price on a delivery order; the app keeps 30% + a 5%
     marketing fee and remits weekly. Nothing in the books reflects the gap:
         gross $15,131.90 · fees $5,296.18 · channel margin falls 71% → 36%
  3. AR CONCENTRATION — chronic late payers carry the aged bucket:
         $21,910 at 90+ days, Riverside Catering the largest single exposure
  4. THE LOSS-LEADER — "oat matcha latte" is popular and priced like a regular latte while costing
     nearly four times as much to make:
         $25,806 revenue (2nd biggest item) · 4,129 units · 22.4% margin vs 78-89% for every other drink

The GL wages line and the labor-hours file also DISAGREE on purpose (GL posts a weekly payroll
figure; the hours file carries every scheduled shift). A good analysis notices and says which it used.

SERVED_BY IS DEMO 03's INPUT. The POS `served_by` weights are a second plant: Ava and Mira turn
materially more revenue per hour on shift than Ben and Deo, invisible until someone joins this log to
the labor hours. That is what lets demo 03's scheduler rank the crew and put the two of them on the
7-9am rush instead of rotating evenly. The two demos share this file on purpose; demo 03's `--check`
fails loudly when it isn't filed.
"""
import csv, random, datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # docs/demos — the shared harness
import fixture as fx                            # noqa: E402 — the shared harness (§ no side doors)

OUT = Path(__file__).parent / "fixtures"          # beside the take + the cut spec
START = dt.date(2026, 1, 1)
DAYS = 205                                     # jan 1 → jul 24
PREFIX = "uploads/"

MENU = [  # item, price, cost, base popularity
    ("espresso", 3.25, 0.62, 14), ("latte", 5.50, 1.18, 34), ("cappuccino", 5.00, 1.05, 18),
    ("cold brew", 5.75, 1.02, 16), ("drip coffee", 2.95, 0.44, 22), ("croissant", 4.00, 1.20, 12),
    ("muffin", 3.75, 1.05, 9), ("bagel + cream cheese", 4.50, 1.35, 8), ("tea", 3.50, 0.38, 6),
    # the planted loss-leader: popular, feels premium, and the almond/oat upcharge never covered the
    # ingredient cost. Only visible once someone computes contribution per item.
    ("oat matcha latte", 6.25, 4.85, 15),
]
# Who rings the sale. FOH only — Cora bakes, she never serves. The weights are the PLANT: Ava and
# Mira turn materially more revenue per hour on shift than Ben and Deo, which is invisible until
# someone joins the POS log to the labor hours. A scheduler that measures before assigning puts the
# two of them on the 7-9am rush; one that doesn't spreads everyone evenly.
SERVERS = [("Ava Reyes", 32), ("Mira Kwon", 30), ("Ben Osei", 21), ("Deo Park", 17)]

HOUR_WEIGHT = {6:3, 7:11, 8:18, 9:14, 10:8, 11:7, 12:9, 13:8, 14:6, 15:5, 16:4, 17:3, 18:2}

# What each file IS, for the cabinet. The caption is not decoration — it is the entire index: `find`
# lists by prefix and matches these fields, so a file whose caption doesn't say "POS" cannot be found
# by an agent looking for a POS export, however obvious the filename looks to a human.
CAPTIONS = {
    "pos-transactions-2026-ytd.csv": (
        "POS transactions export — 2026 YTD",
        "Every ticket line from the POS vendor: timestamp, item, qty, unit price, unit cost, payment "
        "method, channel, and served_by (which barista rang it). The per-transaction sales log.",
        ["pos", "sales", "export", "transactions", "served_by"]),
    "ar-aging-2026-07.csv": (
        "AR aging report — July 2026",
        "Open and settled invoices with issue date, due date, amount and payment date. Aging buckets "
        "and DSO come off this.", ["ar", "aging", "receivables", "invoices", "export"]),
    "gl-extract-2026-ytd.csv": (
        "GL extract — 2026 YTD",
        "General-ledger lines by date and account with debit/credit. The bookkeeping view, which does "
        "not agree with the operational exports.", ["gl", "ledger", "accounting", "export"]),
    "labor-hours-2026-ytd.csv": (
        "Labor hours export — 2026 YTD",
        "Every scheduled shift: worker, role, start, end, hours, hourly rate. A third-party payroll "
        "export, NOT the ERP's own time entries.", ["labor", "hours", "payroll", "shifts", "export"]),
    "delivery-app-settlements-2026-ytd.csv": (
        "Delivery app settlements — 2026 YTD",
        "Weekly remittances from the delivery platform: gross sales, commission, marketing fee, net "
        "deposit. The fee the POS never records.", ["delivery", "settlements", "fees", "export"]),
}


def generate():
    """Write the five CSVs to `fixtures/`. Pure and offline — no AWS, no network.

    The RNG draw ORDER is load-bearing: every ground-truth number in the docstring is a consequence
    of it, and inserting a single `random` call anywhere shifts the whole stream. Adding a column
    already cost one set of numbers. Recompute before changing anything here."""
    random.seed(20260724)
    OUT.mkdir(parents=True, exist_ok=True)

    # ── 1. POS transactions (operational: hours, item mix, channel) ──
    with open(OUT / "pos-transactions-2026-ytd.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["timestamp","ticket_id","item","qty","unit_price","unit_cost","payment_method","channel","served_by"])
        tid = 100000
        for d in range(DAYS):
            day = START + dt.timedelta(days=d)
            dow = day.weekday()
            volume = int(random.gauss(118 if dow < 5 else 165, 18))
            for _ in range(max(30, volume)):
                hour = random.choices(list(HOUR_WEIGHT), weights=list(HOUR_WEIGHT.values()))[0]
                ts = dt.datetime.combine(day, dt.time(hour, random.randint(0,59), random.randint(0,59)))
                tid += 1
                server = random.choices([s[0] for s in SERVERS], weights=[s[1] for s in SERVERS])[0]
                # the stronger servers also upsell — more items per ticket, not just more tickets
                spread = [58,32,10] if server in ("Ava Reyes", "Mira Kwon") else [78,19,3]
                for _ in range(random.choices([1,2,3], spread)[0]):
                    item, price, cost, pop = random.choices(MENU, weights=[m[3] for m in MENU])[0]
                    w.writerow([ts.isoformat(), f"T{tid}", item, random.choices([1,2],[92,8])[0], f"{price:.2f}",
                                f"{cost:.2f}", random.choices(["card","cash","mobile"],[68,14,18])[0],
                                random.choices(["in_store","drive_thru","delivery_app"],[70,22,8])[0], server])

    # ── 2. AR aging (cash & liquidity: DSO, buckets, collections) ──
    CUSTOMERS = ["Riverside Catering","Delta Films Craft Services","Lofthouse Office","Summit Coworking",
                 "Nguyen Events","Maple Street Daycare","Harbor Law Group","Vector Studios"]
    with open(OUT / "ar-aging-2026-07.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["invoice_id","customer","issued","due","amount","paid_amount","paid_date"])
        for i in range(140):
            issued = START + dt.timedelta(days=random.randint(0, DAYS-5))
            terms = random.choice([15, 30, 30, 30, 45, 60])
            due = issued + dt.timedelta(days=terms)
            amt = round(random.uniform(180, 3400), 2)
            cust = random.choice(CUSTOMERS)
            slow = cust in ("Vector Studios", "Harbor Law Group")     # two chronic late payers, on purpose
            if random.random() < (0.62 if slow else 0.86):
                lag = random.randint(-3, 34 if slow else 11)
                paid = due + dt.timedelta(days=lag)
                if paid <= dt.date(2026, 7, 24):
                    w.writerow([f"INV-{2000+i}", cust, issued, due, f"{amt:.2f}", f"{amt:.2f}", paid]); continue
            w.writerow([f"INV-{2000+i}", cust, issued, due, f"{amt:.2f}", "0.00", ""])

    # ── 3. GL extract (statement analysis: margins, common-size, trend) ──
    ACCT = [("SALES_REVENUE","REVENUE"),("COST_OF_GOODS_SOLD","EXPENSE"),("WAGES_EXPENSE","EXPENSE"),
            ("RENT_EXPENSE","EXPENSE"),("UTILITIES_EXPENSE","EXPENSE"),("SUPPLIES_EXPENSE","EXPENSE"),
            ("INSURANCE_EXPENSE","EXPENSE"),("REPAIRS_EXPENSE","EXPENSE"),("MARKETING_EXPENSE","EXPENSE")]
    with open(OUT / "gl-extract-2026-ytd.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date","entry_id","account","account_type","debit","credit","memo"])
        n = 0
        for d in range(DAYS):
            day = START + dt.timedelta(days=d)
            rev = round(random.gauss(760, 130), 2)
            n += 1; w.writerow([day, f"JE{n:05d}", "SALES_REVENUE", "REVENUE", "0.00", f"{rev:.2f}", "daily sales"])
            n += 1; w.writerow([day, f"JE{n:05d}", "COST_OF_GOODS_SOLD", "EXPENSE", f"{rev*random.uniform(.26,.32):.2f}", "0.00", "cogs"])
            if day.weekday() == 4:
                n += 1; w.writerow([day, f"JE{n:05d}", "WAGES_EXPENSE", "EXPENSE", f"{random.gauss(1180,90):.2f}", "0.00", "weekly payroll"])
            if day.day == 1:
                for acct, amt in (("RENT_EXPENSE",3500),("INSURANCE_EXPENSE",300),("UTILITIES_EXPENSE",random.gauss(610,70))):
                    n += 1; w.writerow([day, f"JE{n:05d}", acct, "EXPENSE", f"{amt:.2f}", "0.00", "monthly"])
            if random.random() < .22:
                acct = random.choice(["SUPPLIES_EXPENSE","REPAIRS_EXPENSE","MARKETING_EXPENSE"])
                n += 1; w.writerow([day, f"JE{n:05d}", acct, "EXPENSE", f"{random.uniform(40,480):.2f}", "0.00", "misc"])

    # ── 4. labor hours (unit economics: cost per cup, productivity) ──
    CREW = [("Ava Reyes","barista",19.0),("Ben Osei","barista",18.0),("Deo Park","shift_lead",24.0),
            ("Cora Vance","baker",22.0),("Mira Kwon","barista",18.5)]

    with open(OUT / "labor-hours-2026-ytd.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date","worker","role","shift_start","shift_end","hours","hourly_rate"])
        for d in range(DAYS):
            day = START + dt.timedelta(days=d)
            for name, role, rate in CREW:
                if random.random() < (0.62 if role != "shift_lead" else 0.78):
                    # shifts are scheduled flat 6a-2p / 10a-6p regardless of demand — the planted
                    # overstaffing: afternoons carry nearly the morning crew on a fifth of the revenue
                    start_h = random.choice([6, 6, 7, 10, 11])
                    hrs = random.choice([6, 7, 8])
                    w.writerow([day, name, role, f"{start_h:02d}:00", f"{min(start_h+hrs,19):02d}:00",
                                float(hrs), f"{rate:.2f}"])

    # ── 5. delivery app settlements (the fee nobody nets out) ──
    # The POS records menu price on a delivery order; the app keeps 30% and remits the rest weekly.
    # Nothing in the books reflects that gap until someone reads this file.
    with open(OUT / "delivery-app-settlements-2026-ytd.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["payout_date","period_start","period_end","gross_sales","commission_pct","commission","marketing_fee","net_deposit"])
        pos_rows = list(csv.DictReader(open(OUT / "pos-transactions-2026-ytd.csv")))
        weekly = defaultdict(float)
        for r in pos_rows:
            if r["channel"] == "delivery_app":
                wk = dt.date.fromisoformat(r["timestamp"][:10])
                wk -= dt.timedelta(days=wk.weekday())
                weekly[wk] += float(r["unit_price"]) * int(r["qty"])
        for wk in sorted(weekly):
            gross = round(weekly[wk], 2)
            comm = round(gross * 0.30, 2)
            mktg = round(gross * 0.05, 2)
            w.writerow([wk + dt.timedelta(days=9), wk, wk + dt.timedelta(days=6),
                        f"{gross:.2f}", "0.30", f"{comm:.2f}", f"{mktg:.2f}", f"{gross-comm-mktg:.2f}"])

    for p in sorted(OUT.glob("*.csv")):
        print(f"  {p.name:38} {sum(1 for _ in open(p))-1:>6} rows  {p.stat().st_size/1024:>7.1f} KB")


# ── the three verbs ───────────────────────────────────────────────────────────────────────────────

def seed():
    generate()
    for name, (title, note, tags) in CAPTIONS.items():
        fx.file_document(OUT / name, key=f"{PREFIX}{name}", title=title, note=note, tags=tags,
                         occurred_at="2026-07-24")
    print(f"  wrote   {len(CAPTIONS):>4} filed document(s) under {PREFIX}")


def reset():
    """Empty the cabinet under `uploads/`.

    Demo 01 had no teardown at all, which is the contamination the doctrine warns about pointing the
    other way: these exports outlive their demo and the NEXT demo's agent finds them and reasons off
    them. That is exactly how a labor-scheduling take built a roster around a worker who did not
    exist in the ERP. An export nobody removes is an export every later demo inherits."""
    fx.unfile(PREFIX, owns=CAPTIONS)


def check():
    for name, (title, _note, _tags) in CAPTIONS.items():
        local = OUT / name
        fx.require(local.exists(), f"{name} generated locally")

    docs = {d["key"]: d for d in fx.s.call(
        "storage", "manage_storage", {"op": "find", "prefix": PREFIX}).get("documents", [])}
    for name in CAPTIONS:
        d = docs.get(f"{PREFIX}{name}")
        fx.require(d and (d.get("caption") or {}).get("title"),
                   f"{name:38} filed WITH a caption ({d['size'] if d else 0} bytes)")

    # The captions are the index, so prove the searches an agent actually runs come back non-empty
    # rather than only that the objects exist.
    for query, want in (("POS", "pos-transactions-2026-ytd.csv"),
                        ("labor hours", "labor-hours-2026-ytd.csv"),
                        ("delivery settlements", "delivery-app-settlements-2026-ytd.csv")):
        hits = fx.s.call("storage", "manage_storage",
                         {"op": "find", "prefix": PREFIX, "query": query}).get("documents", [])
        fx.require(any(h["key"].endswith(want) for h in hits),
                   f"find(query={query!r}) returns {want}")


if __name__ == "__main__":
    fx.run("demo 01 · analysis — the exports a cafe receives", seed, reset, check)
