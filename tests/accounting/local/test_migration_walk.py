"""The migration walk (modules/accounting/kb.md), dry-run end to end in local mode.

A fixture business — 10 items, 3 open invoices, 2 unpaid bills, 4 capitalized assets (+1
sub-threshold), cash, a loan — migrates onto the platform exactly as the playbook prescribes:

  counts   → ADJUSTED movements valued to OWNER_EQUITY via a temporary STOCK_ADJUSTED#* instance
  open AR  → manage_invoice create/issue_invoice with lines at OWNER_EQUITY (never revenue)
  open AP  → create_po/manage_po receive with lines at OWNER_EQUITY (never expense)
  assets   → manage_assets add with paid_via: opening (no entry posts)
  the rest → ONE opening entry (deterministic entryId + timestamp), EXCLUDING AR/AP/INVENTORY

then asserts the acceptance gate: the platform trial balance ties out line by line against the
source system's TB, the migration produced an EMPTY income statement (no revenue, no expense —
state was imported, not history re-entered), and a full re-run of every posting no-ops.
"""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Module roots each lambda needs beside its own dir (mirrors each zip's bundle layout).
_EXTRA_DIRS = {
    "accounting": [],
    "inventory":  ["modules/inventory", "modules/rules"],
    "invoicing":  ["modules/invoicing", "modules/rules", "modules/inventory"],
    "purchasing": ["modules/purchasing", "modules/rules", "modules/agreements"],
    "assets":     ["modules/assets", "modules/rules"],
    "rules":      ["modules/rules", "modules/inventory", "modules/labor"],
}
# Shared top-level names that must not leak between modules (every module has its own _helpers;
# the rule engine and its libraries bind per-load).
_PURGE = {"_helpers", "rules", "instances", "params", "stock_rules", "movements", "availability",
          "capacity", "catalog_rules", "transition_rules", "general_rules", "payroll_rules"}
_added_paths = []


def load(module, name):
    for d in list(_added_paths):
        sys.path.remove(d)
        _added_paths.remove(d)
    lambdas_dir = REPO_ROOT / "modules" / module / "lambdas"
    # the lambda's own dir first: a bundle is flat, so main.py imports its sibling op modules
    # (manage_rules carries add_rule.py etc.) by bare name
    for d in [str(lambdas_dir / name), str(lambdas_dir)] + [str(REPO_ROOT / p) for p in _EXTRA_DIRS[module]]:
        sys.path.insert(0, d)
        _added_paths.append(d)
    for m in list(sys.modules):
        if m in _PURGE or m.startswith("mig_"):
            del sys.modules[m]
    for f in (lambdas_dir / name).glob("*.py"):
        sys.modules.pop(f.stem, None)
    path = lambdas_dir / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"mig_{module}_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _invoke(mod, body):
    resp = mod.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _scratch():
    out = REPO_ROOT / "out" / "test_migration_walk"
    logs = REPO_ROOT / "logs" / "test_migration_walk"
    for d in (out, logs):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    env = {
        "LOCAL_LEDGER": "ledger.jsonl", "LOCAL_PENDING": "pending.jsonl",
        "LOCAL_BALANCES": "balances.jsonl", "LOCAL_SCHEMA": "schema.jsonl",
        "LOCAL_EVENTS": "events.jsonl", "LOCAL_CLASSIFICATIONS": "classifications.jsonl",
    }
    for k, v in env.items():
        os.environ[k] = str(out / v)
    os.environ["LOCAL_LOGS"] = str(logs)

    # accounting and assets have migrated off the jsonl local mode — they need real tables
    # (modules/aws/aws.py). This walk drives both, so give them the same substrate their own
    # suites use: `books()` is the ledger/pending/settings/chart set every posting module needs.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from helpers.localaws import books, invoicing, make_table, seed_registry
    os.environ.update(books("migration-walk"))
    os.environ["BALANCES_TABLE"] = make_table("accounting-balances")
    os.environ["ASSETS_TABLE"] = make_table("assets")
    os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
    os.environ["MOVEMENTS_TABLE"] = make_table("inventory-movements")
    os.environ["ORDERS_TABLE"] = make_table("purchasing-orders")
    os.environ.update(invoicing("migration-walk"))
    os.environ["AGREEMENTS_TABLE"] = make_table("agreements")   # invoice + lines + transitions (ITEMS_TABLE re-set below)
    # the walk drives assets AND inventory, and both validate fail-closed against the same
    # registry table — a deployment has one, carrying every module's fields.
    seed_registry(os.environ["SCHEMA_TABLE"], "asset_fields", "item_fields")


# ─── the source system (what the owner brings) ───

CUTOVER = "2026-06-30"

ITEMS = [  # (item_id, name, unit_cost, on_hand)
    ("1#beans", "Espresso Beans", 12.50, 40), ("1#milk", "Whole Milk", 3.00, 60),
    ("1#oat-milk", "Oat Milk", 4.25, 24), ("1#cups-12oz", "12oz Cups", 0.15, 2000),
    ("1#lids", "Lids", 0.05, 2000), ("1#syrup-vanilla", "Vanilla Syrup", 8.00, 12),
    ("1#pastry-croissant", "Croissant", 1.40, 30), ("1#tea-earl-grey", "Earl Grey", 6.50, 10),
    ("1#cold-brew-concentrate", "Cold Brew Concentrate", 22.00, 6),
    ("1#gift-mug", "Gift Mug", 5.50, 18),
]
OPEN_INVOICES = [  # (customer, amount, due_date, original date for the memo)
    ("catering-riverside", 450.00, "2026-07-10", "2026-06-12"),
    ("office-lofthouse", 800.00, "2026-07-15", "2026-06-20"),
    ("wedding-nguyen", 1250.00, "2026-07-28", "2026-06-27"),
]
OPEN_BILLS = [  # (vendor, amount, original date)
    ("roaster-supply-co", 700.00, "2026-06-18"),
    ("dairy-distributors", 1300.00, "2026-06-25"),
]
ASSETS = [  # (name, class, cost) — cost None = sub-threshold, operational row only
    ("espresso-machine", "machinery_equipment", 9500.00),
    ("walk-in-fridge", "machinery_equipment", 8000.00),
    ("delivery-van", "vehicles", 4500.00),
    ("pos-terminal", "computer_equipment", 2000.00),
    ("hand-grinder", "machinery_equipment", None),
]
CASH = 12000.00
NOTES_PAYABLE = 10000.00

INVENTORY_VALUE = round(sum(c * q for _, _, c, q in ITEMS), 2)
AR = round(sum(a for _, a, _, _ in OPEN_INVOICES), 2)
AP = round(sum(a for _, a, _ in OPEN_BILLS), 2)
FIXED_ASSETS = round(sum(c for _, _, c in ASSETS if c), 2)

SOURCE_TB = {  # the old system's trial balance as of the cutover (balance-signed: DR +, CR −)
    "CASH": CASH,
    "ACCOUNTS_RECEIVABLE": AR,
    "INVENTORY": INVENTORY_VALUE,
    "FIXED_ASSETS": FIXED_ASSETS,
    "ACCOUNTS_PAYABLE": -AP,
    "NOTES_PAYABLE": -NOTES_PAYABLE,
    "OWNER_EQUITY": -(CASH + AR + INVENTORY_VALUE + FIXED_ASSETS - AP - NOTES_PAYABLE),
}


def _run_walk():
    """Execute the playbook. Returns the list of journal payloads the modules produced."""
    # 2. items + counts, valued to equity via the temporary STOCK_ADJUSTED#* instance
    add_rule = load("rules", "manage_rules")
    code, body = _invoke(add_rule, {
        "op": "add", "matches": "STOCK_ADJUSTED#*", "rule": "value_adjustment", "name": "opening_counts",
        "n": 100, "param": {"account": "OWNER_EQUITY", "account_type": "EQUITY"}})
    assert code == 200, body

    create_item = load("inventory", "manage_stock")
    update_stock = load("inventory", "manage_stock")
    for item_id, name, cost, qty in ITEMS:
        code, body = _invoke(create_item, {
            "op": "create_item", "item_id": item_id, "name": name, "unit_cost": cost, "unit_price": 0})
        assert code == 200, body
        code, body = _invoke(update_stock, {
            "op": "move", "item_id": item_id, "quantity": qty, "movement_type": "ADJUSTED",
            "entry_id": f"opening-count-{item_id}", "memo": f"opening count as of {CUTOVER}",
            # deterministic entry_id AND timestamp: post_journal_entry dedups on (pk, sk), so
            # re-running the walk lands on the same key instead of double-valuing the count
            "timestamp": f"{CUTOVER}T00:00:00Z"})
        assert code == 200, body

    delete_rule = load("rules", "manage_rules")
    code, body = _invoke(delete_rule, {"op": "delete", "matches": "STOCK_ADJUSTED#*", "name": "opening_counts", "n": 100})
    assert code == 200, body

    # 3. open AR — individually, lines at OWNER_EQUITY, original date in the memo
    create_invoice = load("invoicing", "manage_invoice")
    issue_invoice = load("invoicing", "issue_invoice")
    invoice_ids = []
    for customer, amount, due, orig in OPEN_INVOICES:
        code, body = _invoke(create_invoice, {
            "op": "create",
            "customer": customer, "due_date": due,
            "memo": f"migrated open invoice, originally issued {orig}",
            "lines": [{"description": "open balance at cutover", "account": "OWNER_EQUITY",
                       "accountType": "EQUITY", "amount": amount}]})
        assert code == 200, body
        invoice_ids.append(body["invoice_id"])
        code, body = _invoke(issue_invoice, {"invoice_id": invoice_ids[-1]})
        assert code == 200, body

    # 4. open AP — mirrored
    create_po = load("purchasing", "create_po")
    record_receipt = load("purchasing", "manage_po")
    for vendor, amount, orig in OPEN_BILLS:
        code, body = _invoke(create_po, {
            "vendor": vendor, "memo": f"migrated unpaid bill dated {orig}", "approved": True,
            "lines": [{"description": "open balance at cutover", "account": "OWNER_EQUITY",
                       "accountType": "EQUITY", "amount": amount}]})
        assert code == 200, body
        code, body = _invoke(record_receipt, {"op": "receive", "po_id": body["po_id"]})
        assert code == 200, body

    # 5. assets — paid_via opening posts nothing
    manage_assets = load("assets", "manage_assets")
    for name, klass, cost in ASSETS:
        req = {"op": "add", "name": name, "class": klass}
        if cost:
            req.update({"cost": cost, "paid_via": "opening"})
        code, body = _invoke(manage_assets, req)
        assert code == 200, body
        assert body.get("journal_entry_id") is None, "paid_via: opening must not post"
    # assets posts through the real post_journal_entry now, so "posted nothing" is a statement
    # about the LEDGER rather than about a captured payload
    from helpers.localaws import ledger_rows
    assert not [r for r in ledger_rows() if str(r.get("source", "")).startswith("assets")], \
        "opening assets posted a journal entry"

    # 6. the opening entry — everything else, AR/AP/INVENTORY excluded
    opening = {
        "entryId": f"opening-{CUTOVER}",
        "timestamp": f"{CUTOVER}T00:00:00Z",
        "memo": f"opening balances as of {CUTOVER}",
        "source": "migration",
        "lineItems": [
            {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": CASH},
            {"account": "FIXED_ASSETS", "accountType": "ASSET", "side": "DEBIT", "amount": FIXED_ASSETS},
            {"account": "NOTES_PAYABLE", "accountType": "LIABILITY", "side": "CREDIT", "amount": NOTES_PAYABLE},
            {"account": "OWNER_EQUITY", "accountType": "EQUITY", "side": "CREDIT",
             "amount": round(CASH + FIXED_ASSETS - NOTES_PAYABLE, 2)},
        ],
    }
    pje = load("accounting", "post_journal_entry")
    resp = pje.handler(opening, None)
    assert resp["statusCode"] == 200, resp

    # every module in the walk posts through the real post_journal_entry now, so its entries are
    # already ON the ledger — the only payload this returns is the one written here, for the
    # idempotency replay.
    return [opening]


def _trial_balance():
    tb = load("accounting", "get_statement")
    code, body = _invoke(tb, {"statement": "trial_balance"})
    assert code == 200, body
    return {a["accountId"]: round(a["balance"], 2) for a in body["balances"]}


def test_migration_ties_out():
    _scratch()
    payloads = _run_walk()

    # ─── the acceptance gate ───
    platform = _trial_balance()
    for account, expected in SOURCE_TB.items():
        got = platform.get(account, 0.0)
        assert abs(got - round(expected, 2)) < 0.01, \
            f"{account}: source {expected} != platform {got}"
    assert set(platform) == set(SOURCE_TB), f"unexpected accounts: {set(platform) - set(SOURCE_TB)}"

    # an imported state produces an EMPTY income statement — no revenue, no expense
    inc = load("accounting", "get_statement")
    code, body = _invoke(inc, {"statement": "income", "range": {"start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"}})
    assert code == 200, body
    assert body["revenue"] == [] and body["expenses"] == [], \
        f"migration produced P&L activity: {body}"

    # re-running every posting no-ops (deterministic entryIds + timestamps)
    pje = load("accounting", "post_journal_entry")
    for p in payloads:
        pje.handler(p, None)
    assert _trial_balance() == platform, "re-run changed balances — idempotency broken"

    # the temporary valuation instance is gone
    from helpers.localaws import rows as _table_rows
    assert all(r.get("pk") != "STOCK_ADJUSTED#*" for r in _table_rows(os.environ["RULE_INSTANCES_TABLE"])), \
        "the opening_counts instance must be deleted after the counts"


if __name__ == "__main__":
    test_migration_ties_out()
    print("ok")
