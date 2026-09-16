"""transition_item — move ONE item of a transaction to a new state.

The spine of the transaction object (`../../TODO.md`). The 80s invoice put one `status` box on the
*invoice*, so anything that didn't fit — one seat flown and one refunded, a partial delivery, a
partial payment — became a credit memo or a split invoice. Here state lives on the **item**, and
those stop being special cases: seat A `earned`, seat B `refunded`, seat C still `paid`, one
invoice, no gymnastics.

What a transition does, in order:

  1. append a row to the item's append-only stream (the row IS the record — nothing is edited);
  2. run whatever money rules are ATTACHED to this kind of item for the state it is entering, and
     post their legs as a journal entry — **dimensioned by the item**, so cost/revenue aggregate per
     unit (the payoff: `../../TODO.md` § why). Nothing matched → nothing posted → pure annotation.

The invoice's own `status` is left alone: it is a snapshot, and the item states are the
truth. `get_invoices` folds the streams back into per-item current state.

Money is decided by attachment, not by this handler — so a hotel's `check-in` / `check-out` vocab or
a carrier's `flown` needs no code here, and a sales tax collected on the same invoice as a room-night
credits the state's payable instead of unearned revenue without this file knowing what a tax is
(`../transition_rules.py`).
"""

import json

import rules
import transition_rules
import metric_rules   # modules/metrics: record_metric — a moment as a product event, by a row
from _helpers import (
    get_invoice, append_transition, read_transitions, fold_items,
    post_journal_entry, new_transition_id, now_iso, ok, err, INVOICE_LEVEL,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    invoice_id = body.get("invoice_id")
    item_id = body.get("item_id")
    state = body.get("state")
    if not invoice_id:
        return err("invoice_id is required")
    if not state:
        return err("state is required (the state to move the item to)")

    invoice = get_invoice(invoice_id)
    if not invoice:
        return err(f"invoice not found: {invoice_id}", status=404)

    # No item_id means the entry is about the INVOICE, not one of its lines — a chase that went out,
    # a teardown that ran. A timer that fires and deletes itself leaves no other evidence, and this
    # is where that belongs: the same stream, so the history reads in one order, and `get_invoices`
    # already returns it. It posts nothing to the journal; an invoice-level event is a record, not a
    # money movement, and the money states are per line.
    item = None
    if not item_id:
        item_id = INVOICE_LEVEL
    else:
        item = next((ln for ln in invoice.get("lines", []) if ln.get("item_id") == item_id), None)
        if not item:
            return err(
                f"item not found on invoice {invoice_id}: {item_id}", status=404,
                item_ids=[ln.get("item_id") for ln in invoice.get("lines", [])],
            )

    history = read_transitions(invoice_id)

    # Idempotent retry: the caller's transition already landed. Check the rows we read for the
    # fold anyway — a conditional put can't do it, since `at` is generated fresh on every call.
    transition_id = body.get("transition_id")
    if transition_id and any(r.get("transition_id") == transition_id for r in history):
        return ok({
            "invoice_id": invoice_id, "item_id": item_id, "state": state,
            "transition_id": transition_id, "duplicate": True,
            "items": _snapshot(invoice, fold_items(history)),
        })

    current = fold_items(history).get(item_id)
    from_state = current["state"] if current else invoice.get("status", "draft")
    if from_state == state:
        return err(f"item {item_id} is already {state}", status=409)

    # ─── the money leg: whatever matches this kind of item entering this state ───
    #
    # The match IS the dispatch. `accountType` is what kind of thing the item is — REVENUE is what you
    # sell, LIABILITY is what you hold for someone else (a sales tax, a tip) — and a rule-added tax
    # item carries it just like a catalogued one, which is why a tax's money rules match it despite it
    # having no catalog key.
    #
    # ctx is BUILT, never the item row: `run_instances` refuses a ctx carrying a `rule_key` (the guard
    # that makes tax-on-tax impossible at build time), and a tax item must still move its own money
    # here. It carries `from` so a refund knows WHAT it is reversing.
    # An invoice-level entry moves no money: the money states belong to LINES, and "we chased them"
    # is a record rather than a transaction. So the rules are not consulted and nothing posts.
    if item is None:
        ctx, effects, legs = {}, [], []
    else:
        ctx = {
            "amount": item.get("amount", 0),
            "account": item.get("account"),
            "accountType": item.get("accountType", "REVENUE"),
            "from": from_state,
        }
        effects = rules.run_instances(
            ctx,
            transition_rules.money_instances(ctx["accountType"], state),
            modules=[transition_rules, metric_rules],
        )
        legs = effects

    # A non-billable item (an operational task from a template — a `clean` at rate 0) has no money
    # states. Say so, rather than letting post_journal_entry reject a zero-amount leg downstream.
    if legs and (not ctx["account"] or ctx["amount"] <= 0):
        return err(
            f"item {item_id} is not billable (no amount / revenue account), so '{state}' can't move "
            f"money. Use a non-money state for it (e.g. 'done').",
            status=409,
        )

    entry_id = None
    if legs:
        # Dimensioned BY THE ITEM — this is what makes per-unit cost/revenue a query instead of an
        # estimate. accounting carries `dimensions` verbatim onto every ledger row (labor's
        # worker_id precedent). Note it is entry-LEVEL, so one entry per item transition.
        #
        # `catalog_item_id` is the load-bearing one: item_id is only unique WITHIN an invoice, so
        # aggregating "what does a clean & restock actually cost" across every invoice needs the SKU.
        # location: the line's catalog key carries its ordinal prefix (<n>#<sku>); a rule-added
        # item (a tax/tip — no catalog key by design) falls back to the invoice row's location.
        cat = item.get("catalog_item_id") or ""
        loc = cat.split("#", 1)[0] if "#" in cat and cat.split("#", 1)[0].isdigit() \
            else str(invoice.get("location") or "1")
        dimensions = {
            "invoice_id": invoice_id,
            "item_id": item_id,
            "location": loc,
            **({"job": str(invoice["job"])} if invoice.get("job") else {}),
            **({"catalog_item_id": item["catalog_item_id"]} if item.get("catalog_item_id") else {}),
            **({"unit": item["unit"]} if item.get("unit") else {}),
            **(body.get("dims") or {}),
        }
        entry_id = post_journal_entry({
            "lineItems": legs,
            "memo": body.get("memo") or f"{item.get('description', item_id)} → {state}",
            "source": f"invoicing.transition_item:{invoice_id}:{item_id}",
            "dimensions": dimensions,
        })

    tid = transition_id or new_transition_id()
    at = now_iso()
    row = {
        "invoice_id": invoice_id,
        "tx_sk": f"{item_id}#{at}#{tid}",
        "transition_id": tid,
        "item_id": item_id,
        "state": state,
        "from": from_state,
        "at": at,
        **({"memo": body["memo"]} if body.get("memo") else {}),
        **({"dims": body["dims"]} if body.get("dims") else {}),
        **({"entry_id": entry_id} if entry_id else {}),
    }
    append_transition(row)

    items = _snapshot(invoice, fold_items(read_transitions(invoice_id)))
    return ok({
        "invoice_id": invoice_id,
        "item_id": item_id,
        "from": from_state,
        "state": state,
        "transition_id": tid,
        "posted": entry_id is not None,
        "journal_entry_id": entry_id,
        "items": items,
        "settled": all(_is_terminal(i) for i in items),
    })


# When an item needs nothing more — and that depends on WHAT KIND of thing it is, for the same reason
# there is no `ITEM_TRANSITION#LIABILITY#earned` rule:
#
#   REVENUE     you sold it. It is done when you have DELIVERED it (`earned`) — being paid for a room
#               you haven't given anyone yet is a liability, not the end of anything.
#   LIABILITY   you collected it for someone else — a sales tax, a tip, a deposit. There is nothing to
#               deliver and nothing to earn, ever. Collecting it (`paid`) IS the end of it; remitting
#               it to the state is a separate transaction against the payable, not this item's stream.
#
# Equilibrium (the whole object settled) is: every item is terminal. Getting this wrong is not
# cosmetic — with one shared set, a collected tax never reported terminal, so ANY invoice carrying tax
# never read settled.
#
# An owner's custom states (`check-in`, `cleaned`) are not here and so are never terminal; that needs
# the state vocabulary in modules/schemas (see TODO § the state vocabulary), which is what would let a
# hotel say a `clean` is finished at `done`.
_TERMINAL = {
    "REVENUE": {"earned", "refunded"},
    "LIABILITY": {"paid", "refunded"},
}
_DEFAULT_TERMINAL = _TERMINAL["REVENUE"]


def _is_terminal(item):
    return item["state"] in _TERMINAL.get(item.get("accountType"), _DEFAULT_TERMINAL)


def _snapshot(invoice, latest):
    """Per-item current state — the fold. An item with no transitions yet reports the invoice's
    own status, so a freshly-created invoice reads as all-`draft` rather than blank."""
    return [
        {
            "item_id": ln.get("item_id"),
            "description": ln.get("description", ""),
            "amount": ln.get("amount"),
            # what KIND of thing it is — what says when it is finished (see _TERMINAL)
            "accountType": ln.get("accountType"),
            "state": latest[ln["item_id"]]["state"] if ln.get("item_id") in latest
            else invoice.get("status", "draft"),
        }
        for ln in invoice.get("lines", [])
    ]
