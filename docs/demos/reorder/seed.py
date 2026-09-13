"""demo 05 fixture — the reorder loop (purchasing · CPO), a clean shelf and no deal in flight.

    .venv/bin/python docs/demos/reorder/seed.py            # reset, seed, check
    .venv/bin/python docs/demos/reorder/seed.py --reset     # teardown only
    .venv/bin/python docs/demos/reorder/seed.py --check     # is this demo recordable right now?

The verbs, the safety properties and the no-side-doors rule are `docs/demos/fixture.py`.

THE TAKE (`record.mjs --step 6`, header Tanners Coffee Co, three turns + the Blue Ridge puppet):

  "heads up, we're down to 5 bags of espresso beans"   → variance booked, par read, order offered
  "yeah, Blue Ridge Roasters"                          → cross-firm create_po, po.proposed crosses
  (puppet: Blue Ridge accepts, then ships with an ETA)
  "did they take it? when's it getting here?"          → the PO answers with its delivery

WHAT THE PREMISE NEEDS, and nothing else:

  - the beans shelf AT PAR (12): the whole story is counting DOWN to 5. A shelf already short
    makes the variance wrong; a shelf below the reorder level makes the agent offer unprompted.
  - the reorder rules attached to `REORDER#1#beans` (required_count + order_required) and the vendor
    link on the item — platform state built once, REQUIRED here, never seeded here.
  - no Blue Ridge deal in flight: no row on the shared agreements store, no open PO, no custody
    row. A pending anything makes the agent negotiate against it instead of opening the loop.

Received POs from prior takes are the books' history and STAY — their receipts are real ledger
entries, and the reorder math ignores them (only OPEN orders count toward on_order).

The shelf is restored through `manage_stock` move (a positive ADJUSTED movement, front door), which is
also roughly the inverse of the take's own write-down — the meter is a movement log, not a field
to overwrite.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # docs/demos — the shared harness
import fixture as fx                            # noqa: E402 — the shared harness (§ no side doors)

VENDOR = "blue-ridge-roasters"
VENDOR_NAME = "Blue Ridge Roasters"
ITEM = "1#beans"
PAR = 12

AGREEMENTS_TABLE = "gerp-agreements-gradienterp"          # the SHARED negotiation store
ORDERS_TABLE = "gerp-purchasing-gradienterp-orders"
SHIPPING_TABLE = "gerp-shipping-gradienterp"
ITEMS_TABLE = "gerp-inventory-gradienterp-items"
RULES_TABLE = "gerp-rules-gradienterp-instances"
CONTACTS_TABLE = "gerp-contacts-gradienterp"


def _stock() -> int:
    r = fx.s.call("inventory", "manage_stock", {"op": "get", "item_id": ITEM})
    items = r.get("items") or []
    return int(items[0]["quantity"]) if items else -1


def _open_vendor_pos():
    rows = fx.table(ORDERS_TABLE).scan().get("Items", [])
    return [r for r in rows if r.get("vendor") == VENDOR and r.get("status") not in ("received", "paid")]


# ── seed ──────────────────────────────────────────────────────────────────────────────────────────

def seed():
    """Restore the shelf to par, through the meter's own front door."""
    q = _stock()
    fx.require(q >= 0, f"item {ITEM} exists (manage_stock get answered)")
    if q != PAR:
        fx.s.call("inventory", "manage_stock", {
            "op": "move",
            "item_id": ITEM, "quantity": PAR - q, "movement_type": "ADJUSTED",
            "memo": "demo fixture: restore shelf to par", "source": "demo-fixture",
        })
        print(f"  adjusted   beans {q} -> {PAR} (ADJUSTED {PAR - q:+d}, through manage_stock (op: move))")
    else:
        print(f"  ok         beans already at par ({PAR})")


# ── reset ─────────────────────────────────────────────────────────────────────────────────────────

def reset():
    """Un-make the deal, every store the loop writes: the negotiation row, the order it opened,
    the custody row the dispatch created. The shelf is seed's job (a movement, not a delete)."""
    fx.purge(AGREEMENTS_TABLE, ("thread", "terms_hash"),
             lambda r: r.get("seller") == VENDOR, "Blue Ridge agreement row(s)")

    fx.purge(ORDERS_TABLE, ("po_id",),
             lambda r: r.get("vendor") == VENDOR and r.get("status") not in ("received", "paid"),
             "open Blue Ridge PO(s)")

    fx.purge(SHIPPING_TABLE, ("thread",),
             lambda r: VENDOR in str(r.get("from_gerp", "")) or VENDOR in str(r.get("carrier_from", "")),
             "Blue Ridge custody row(s)")


# ── check ─────────────────────────────────────────────────────────────────────────────────────────

def check():
    q = _stock()
    fx.require(q == PAR, f"beans at par ({q} of {PAR}) — the take counts DOWN from here")

    items = fx.table(ITEMS_TABLE).scan().get("Items", [])
    beans = next((i for i in items if i.get("item_id") == ITEM), {})
    fx.require(VENDOR in (beans.get("vendors") or []),
               f"{VENDOR} linked on the item — the agent names the vendor without hunting")

    rules = fx.table(RULES_TABLE).query(
        KeyConditionExpression="pk = :p",
        ExpressionAttributeValues={":p": f"INVOICE_LINE#{ITEM}"}).get("Items", [])
    names = {r.get("rule") for r in rules}
    fx.require({"required_count", "order_required"} <= names,
               f"reorder rules attached to REORDER#{ITEM} ({sorted(names)}) — par is a rule, not a guess")

    contact = fx.s.call("contacts", "manage_contacts", {"op": "get", "contact_id": VENDOR})
    contact = contact.get("contact") or contact
    fx.require(contact.get("name") == VENDOR_NAME, f"{VENDOR} is a contact")

    pending = [r for r in fx.table(AGREEMENTS_TABLE).scan().get("Items", [])
               if r.get("seller") == VENDOR]
    fx.require(not pending, f"no Blue Ridge row on the shared store ({len(pending)} found) — "
                            f"a deal in flight makes the agent negotiate against it")

    fx.require(not _open_vendor_pos(), "no open Blue Ridge PO — the loop must open its own")


if __name__ == "__main__":
    fx.run("demo 05 · purchasing — the reorder loop (CPO)", seed, reset, check)
