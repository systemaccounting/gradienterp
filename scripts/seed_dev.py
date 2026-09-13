"""seed_dev — a general toolkit for seeding a tenant's books by invoking the live module lambdas.

The companion to reset-dev: **reset → seed** is the dev-state primitive for BOTH demo reseeds AND
e2e/integration tests. This module is the GENERAL half — composable helpers that map 1:1 to the
tools the agent uses, so seeding exercises the same write paths a real tenant does. Specific
fixtures import it (the demo's coffee shop is `docs/demos/seed.py`).

  # in a test or a fixture:
  import seed_dev as s
  s.contact("acme", "Acme Co", is_customer=True)
  s.sale("acme", 500)                                     # create → issue → pay a revenue invoice
  s.je("adj", "2026-07-01T00:00:00Z", "note", [("CASH","ASSET","DEBIT",10), ("OWNER_EQUITY","EQUITY","CREDIT",10)])
  tb = s.call("accounting", "get_statement", {"statement": "trial_balance", "range": {...}})

  # inspect whatever's there now:
  bash scripts/seed-dev.sh --check

Account targeting is shared with reset-dev (`session_for_account` — hardcoded account today,
reassignable to a dedicated dev account in one place).
"""

import json
import sys

from reset_dev import session_for_account  # shared account assume (hardcoded → reassignable)

_lam = None


def _client():
    global _lam
    if _lam is None:
        _lam = session_for_account().client("lambda")
    return _lam


# ── general invoker ──

def call(module, fn, payload):
    """Invoke gerp-<module>-gradienterp-<fn> with the payload as the event; return the parsed body.
    Raises on a non-2xx tool response (with the payload) so a bad seed fails loudly."""
    name = f"gerp-{module}-gradienterp-{fn}"
    resp = _client().invoke(FunctionName=name, Payload=json.dumps(payload).encode())
    out = json.loads(resp["Payload"].read())
    code = out.get("statusCode")
    body = json.loads(out["body"]) if isinstance(out.get("body"), str) else out
    if code not in (200, 201):
        raise SystemExit(f"FAIL {name} [{code}]: {body}\n  payload={payload}")
    return body


def je(entry_id, ts, memo, lines, source="seed"):
    """Post a balanced journal entry. lines = [(account, type, side, amount), ...]."""
    return call("accounting", "post_journal_entry", {
        "entryId": entry_id, "timestamp": ts, "source": source, "memo": memo,
        "lineItems": [{"account": a, "accountType": t, "side": s, "amount": amt} for a, t, s, amt in lines],
    })


# ── entity helpers (each maps to a tool the agent uses) ──

def contact(cid, name=None, first=None, last=None, **flags):
    """A customer/vendor org (name=) or a person (first=/last=). flags: is_customer/is_vendor/is_employee."""
    body = {"contact_id": cid, **flags, "entity_type": "person" if (first or last) else "organization"}
    if name:
        body["name"] = name
    if first:
        body["first_name"] = first
    if last:
        body["last_name"] = last
    return call("contacts", "manage_contacts", {"op": "put", **body})


def worker(cid, role, rate, classification="W-2"):
    return call("labor", "manage_labor", {"op": "put", "entity": "worker", "contact_id": cid, "role": role,
                                       "rate": rate, "classification": classification})


def item(sku, name, unit, cost, price=0, loc="1"):
    return call("inventory", "manage_stock", {"op": "create_item", "item_id": f"{loc}#{sku}", "name": name, "unit": unit,
                                             "unit_cost": cost, "unit_price": price})


def move(sku, qty, kind="ADJUSTED", entry_id=None, memo="", loc="1"):
    """A stock movement (ADJUSTED/RECEIVED/SOLD/PRODUCED)."""
    p = {"item_id": f"{loc}#{sku}", "quantity": qty, "movement_type": kind, "memo": memo}
    if entry_id:
        p["entry_id"] = entry_id
    return call("inventory", "manage_stock", {"op": "move", **p})


def add_rule(matches, rule, name, n, param):
    return call("rules", "manage_rules", {"op": "add", "matches": matches, "rule": rule, "name": name, "n": n, "param": param})


def del_rule(matches, name, n):
    return call("rules", "manage_rules", {"op": "delete", "matches": matches, "name": name, "n": n})


def invoice(customer, amount, account="SALES_REVENUE", atype="REVENUE",
            due="2026-07-31", memo="", issue=True, pay=False):
    """Create an invoice (one line); optionally issue + pay it. Returns invoice_id."""
    r = call("invoicing", "manage_invoice", {"op": "create", "customer": customer, "due_date": due, "memo": memo,
             "lines": [{"description": memo or account, "account": account, "accountType": atype, "amount": amount}]})
    iid = r["invoice_id"]
    if issue:
        call("invoicing", "issue_invoice", {"invoice_id": iid})
    if pay:
        call("invoicing", "record_invoice_paid", {"invoice_id": iid})
    return iid


def sale(customer, amount, **kw):
    """A completed B2B sale: create → issue → pay a SALES_REVENUE invoice. Returns invoice_id."""
    return invoice(customer, amount, pay=True, **kw)


def pay_invoice(invoice_id):
    return call("invoicing", "record_invoice_paid", {"invoice_id": invoice_id})


def bill(vendor, amount, account="INVENTORY", atype="ASSET", memo="", receive=True, pay=False):
    """Create a PO (one line); optionally receive + pay it. Returns po_id."""
    r = call("purchasing", "create_po", {"vendor": vendor, "memo": memo, "approved": True,
             "lines": [{"description": memo or account, "account": account, "accountType": atype, "amount": amount}]})
    pid = r["po_id"]
    if receive:
        call("purchasing", "manage_po", {"op": "receive", "po_id": pid})
    if pay:
        call("purchasing", "manage_po", {"op": "pay", "po_id": pid})
    return pid


def asset(name, klass, cost, paid_via="opening"):
    return call("assets", "manage_assets", {"op": "add", "name": name, "class": klass,
                                            "cost": cost, "paid_via": paid_via})


def trial_balance(start="2026-01-01T00:00:00Z", end="2026-12-31T23:59:59Z", materialize=True):
    rng = {"range": {"start": start, "end": end}}
    if materialize:
        call("accounting", "get_statement", {"statement": "balances", **rng})
    return call("accounting", "get_statement", {"statement": "trial_balance", **rng})


def check(start="2026-01-01T00:00:00Z", end="2026-12-31T23:59:59Z", income_month=("2026-07-01T00:00:00Z", "2026-07-31T23:59:59Z")):
    """Print the trial balance + an income statement window. Reads the ledger directly — does NOT
    materialize the balances cache (that's a GAAP-checkpoint write the agent owns; a whole-year
    checkpoint here would poison a later as-of report)."""
    tb = trial_balance(start, end, materialize=False)
    rows = sorted(tb["balances"], key=lambda r: r["accountId"])
    dr = sum(r["debits"] for r in rows)
    cr = sum(r["credits"] for r in rows)
    print("  trial balance:")
    for r in rows:
        print(f"    {r['accountId']:24} {r['balance']:>14,.2f}")
    print(f"    {'-' * 40}\n    debits {dr:,.2f}  credits {cr:,.2f}  "
          f"{'BALANCED' if abs(dr - cr) < 0.01 else 'OUT BY ' + format(dr - cr, ',.2f')}")
    inc = call("accounting", "get_statement", {"statement": "income", "range": {"start": income_month[0], "end": income_month[1]}})
    rev = sum(r["balance"] for r in inc["revenue"])
    exp = sum(r["balance"] for r in inc["expenses"])
    print(f"\n  income statement {income_month[0][:7]}: revenue ${rev:,.2f} - expenses ${exp:,.2f} = net ${inc['netIncome']:,.2f}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        print("seed_dev is a library — import it, or run a fixture (e.g. docs/demos/seed.py).\n"
              "  bash scripts/seed-dev.sh --check   # inspect the current books")
