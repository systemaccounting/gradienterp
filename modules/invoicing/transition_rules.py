"""modules/invoicing — the money rules of the transaction object.

A transition SETS DATA. It moves money only when a rule MATCHES the item for the state it lands on —
matched on `ITEM_TRANSITION#<what the item credits>#<the state>`, so an item's money behaviour is a property of
**what kind of thing it is**, not an `if` in a lambda and not a shape baked into the rule.

**What kind of thing it is** is exactly what it credits:

    REVENUE     you SELL it. Cash arrives before it is delivered, so it lands as a LIABILITY
                (UNEARNED_REVENUE) and only becomes revenue at `earned`.
    LIABILITY   you COLLECT it for someone else — a sales tax, a tip, a deposit. There is nothing to
                earn, ever. Cash goes STRAIGHT to the account it is held under, and no leg of it
                touches UNEARNED_REVENUE.

That distinction is why the key is half the item's KIND. A revenue-shaped `collect` posted for every
item overstates unearned revenue and understates the tax you owe the state.

The canonical rules (below, `CANONICAL`):

    ITEM_TRANSITION#REVENUE#paid       collect     DR CASH             / CR UNEARNED_REVENUE
    ITEM_TRANSITION#REVENUE#earned     recognize   DR UNEARNED_REVENUE / CR <the item's account>
    ITEM_TRANSITION#REVENUE#refunded   refund      DR <whichever holds it> / CR CASH
    ITEM_TRANSITION#LIABILITY#paid     collect     DR CASH             / CR <the item's account>
    ITEM_TRANSITION#LIABILITY#refunded refund      DR <the item's account> / CR CASH

There is no `ITEM_TRANSITION#LIABILITY#earned` — a tax is never earned, so `earned` on a tax item matches
nothing and is pure annotation. Same for any state an owner invents (`check-in`, `cleaned`): no key,
no rule, no posting.

**These are not read from the instance table.** What collecting cash means is not a firm's business
config, so there is no row for one to write and nothing to override — the accounting stands because
there is no place to put a different opinion, not because anything is policed. A firm extends
elsewhere: what it sells, what it owes on that, who it pays, what it announces.

**Where the state lives.** The state being ENTERED is half the KEY
(`ITEM_TRANSITION#<what the item credits>#<state>`), so a rule never asks which state fired. It is NOT in the
ctx. The state being LEFT (`ctx["from"]`) is in the ctx, and has to be: how far this item got is a
fact about its own stream, which a key cannot carry, and it is what tells a refund whether it is
reversing recognised revenue or the unearned liability it never left.

`ctx` is the item's money face: `{amount, account, accountType, from}`. Note it is BUILT, never the
item row itself — `run_instances` refuses a ctx carrying a `rule_key` (that guard is what makes tax-on-tax
impossible at invoice-build time), and a rule-added tax item must absolutely still run its money rules.

**A rule-added item (a tax, a tip) has no catalog key** for an `INVOICE_LINE#` instance to match — it is
computed at build time and carries only its `rule_key`, an amount and an account.
So the money key is `accountType`, which every item has, rule-added or not, and which says the one thing money
rules need to know. The deriving rule already decided it (`multiply_item_value`'s `creditorType`), so
a tax routes to SALES_TAX_PAYABLE for exactly the reason it is a tax: it is money held for someone
else.

Bundled into the `transition_item` lambda alongside `rules.py` + `instances.py`.
"""

from typing import Annotated

import instances
from rules import rule, _D
from rules import debit as _debit, credit as _credit   # aliased: `debit`/`credit` are PARAM names below

MONEY = instances.ITEM_TRANSITION   # the subject namespace for an item's money rules
ITEM = "ITEM"       # the account token meaning "the item's own account" (see _account)
ANY = "*"           # the `held_by` fallback: every from-state not named


def subject(account_type, state):
    """The key an item's posting rules hang off: `ITEM_TRANSITION#REVENUE#paid`, `ITEM_TRANSITION#LIABILITY#refunded`.

    Half of it is WHAT THE ITEM IS (what it credits — the only kind-of-thing every item has, whether a
    rule created it or not) and half is WHICH STATE it is entering. Both halves come from the object,
    so no rule has to decide either one."""
    return instances.key(MONEY, f"{account_type}#{state}")


def money_instances(account_type, state):
    """What posts when an item crediting `account_type` enters `state`.

    A CANONICAL key answers from code and the table is not read for it, so what collecting cash means
    — and that money you hold for someone else is never earned — stays out of a firm's reach: there is
    nowhere to put a different opinion about `paid`.

    Any other state reads the instance table, which is how a firm's own status moves money. A hotel
    collecting at `settled` writes the row canonical writes for `paid`, on `ITEM_TRANSITION#REVENUE#settled`.
    A state with no rule either way (`check-in`, `cleaned`) returns [] and posts nothing."""
    key = subject(account_type, state)
    canonical = [i for i in CANONICAL if i["pk"] == key]
    return canonical or instances.for_key(key)


def _account(ref, ctx, account_type):
    """Resolve an account reference. `ITEM` means the item's OWN account — which is what keeps
    anything industry-flavoured out of here: a room-night credits SERVICE_REVENUE, a coke
    SALES_REVENUE and a CA sales tax SALES_TAX_PAYABLE, all through the same instance."""
    if ref == ITEM:
        return ctx["account"], ctx.get("accountType", "REVENUE")
    return ref, account_type


# ── the general rules ───────────────────────────────────────────────────────

@rule
def post_item_value(
    ctx,
    debit:      Annotated[str, "string", "The account debited. `ITEM` means the item's own account."],
    credit:     Annotated[str, "string", "The account credited. `ITEM` means the item's own account."],
    debitType:  Annotated[str, "string", "ASSET | LIABILITY | EXPENSE — ignored when the account is `ITEM`."] = "ASSET",
    creditType: Annotated[str, "string", "REVENUE | LIABILITY | ASSET — ignored when the account is `ITEM`."] = "LIABILITY",
):
    """The item's value, moved between two accounts. That is every money event except a refund.

    Collecting a sale (`DR CASH / CR UNEARNED_REVENUE`), collecting a tax (`DR CASH / CR ITEM` —
    straight to the liability), and recognising revenue (`DR UNEARNED_REVENUE / CR ITEM`) are all this
    one function; they differ only in which two accounts the instance names."""
    amount = _D(ctx["amount"])
    dr, dr_type = _account(debit, ctx, debitType)
    cr, cr_type = _account(credit, ctx, creditType)
    return [_debit(dr, amount, dr_type), _credit(cr, amount, cr_type)]


@rule
def reverse_item_value(
    ctx,
    held_by:    Annotated[dict, "object", "{from-state: the account holding the item's value there}. `ITEM` means the item's own account; `*` is the fallback for every other from-state."],
    heldType:   Annotated[str,  "string", "The held account's type — ignored where `held_by` names `ITEM`."] = "LIABILITY",
    credit:     Annotated[str,  "string", "Where the value goes back out — CASH."] = "CASH",
    creditType: Annotated[str,  "string", "ASSET."] = "ASSET",
):
    """Money back out, reversing whichever account currently HOLDS the item's value.

    Which one that is depends on how far the item got, and that is the one thing an instance cannot
    know — so it reads `ctx["from"]` against the instance's `held_by` map. A seat refunded after it
    was flown reverses REVENUE; a seat refunded before it flew reverses the UNEARNED liability it
    never left. A collected tax only ever sat in its own payable, so its map is `{*: ITEM}` and it can
    never reverse revenue it never earned."""
    amount = _D(ctx["amount"])
    ref = (held_by or {}).get(ctx.get("from")) or (held_by or {}).get(ANY)
    if not ref:
        return []
    dr, dr_type = _account(ref, ctx, heldType)
    cr, cr_type = _account(credit, ctx, creditType)
    return [_debit(dr, amount, dr_type), _credit(cr, amount, cr_type)]


# ── the canonical rules ─────────────────────────────────────────────────────
#
# Double-entry's own defaults, not a firm's business config — so unlike a tax (which exists only
# because a firm wrote a row for it), these ship, and only these run. They carry a real `sk`, so a
# posting one produces lands in the ledger stamped with its rule_key exactly like any other.

CASH = "CASH"
UNEARNED = "UNEARNED_REVENUE"

def _canonical(pk, n, name, rule, param):
    """A canonical row in the same shape a written one has — pk + sk included, so a posting it
    produces carries a real `rule_key` and reads back exactly like a firm-written instance."""
    return {"pk": pk, "sk": instances.sort_key(n, name), "n": n,
            "name": name, "rule": rule, "param": param}


CANONICAL = [
    # a REVENUE item — you sell it, so cash is held unearned until it is delivered
    _canonical(subject("REVENUE", "paid"), 100, "collect", "post_item_value",
               {"debit": CASH, "debitType": "ASSET", "credit": UNEARNED, "creditType": "LIABILITY"}),
    _canonical(subject("REVENUE", "earned"), 100, "recognize", "post_item_value",
               {"debit": UNEARNED, "debitType": "LIABILITY", "credit": ITEM}),
    _canonical(subject("REVENUE", "refunded"), 100, "refund", "reverse_item_value",
               {"held_by": {"earned": ITEM, ANY: UNEARNED}, "heldType": "LIABILITY",
                "credit": CASH, "creditType": "ASSET"}),

    # a LIABILITY item — a tax, a tip, a deposit. You are holding the state's (or the server's) money.
    # It goes straight to the payable and is never earned, so there is no `earned` key at all.
    _canonical(subject("LIABILITY", "paid"), 100, "collect", "post_item_value",
               {"debit": CASH, "debitType": "ASSET", "credit": ITEM}),
    _canonical(subject("LIABILITY", "refunded"), 100, "refund", "reverse_item_value",
               {"held_by": {ANY: ITEM}, "credit": CASH, "creditType": "ASSET"}),
]
