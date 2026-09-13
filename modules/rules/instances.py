"""modules/rules — the rule-instance store.

A module does not "have" a rule. It writes a row here — a rule INSTANCE — and that row is the whole
configuration: which general rule, what params, and the key it runs on. This is how a firm configures
its business without gradienterp deploying code for it.

    pk = INVOICE_LINE#room_deluxe    sk = 300#ca_sales_tax
      { rule: "multiply_item_value",
        param: { factor: 0.0725, creditor: "SALES_TAX_PAYABLE", name: "CA sales tax" } }

    pk = INVOICE_LINE#room_deluxe    sk = 310#sf_district_tax     ← a second one: BOTH run, in n order
    pk = INVOICE_LINE#*              sk = 320#processing_fee      ← runs on every item

**The match is the dispatch.** A rule never asks whether it applies; it runs because a row matches the
thing in hand. A firm that owes no sales tax has no row, so no rule runs — there is no "rate 0 means
off", and no `if taxable` inside any rule.

`pk` is the KEY a rule runs on, and that key is also **when it runs**: code looks a rule up by the key
the thing already carries. An inventory item (`INVOICE_LINE#<key>`, `INVOICE_LINE#*` for all of them) is looked up on
a sale; `PAY_RUN#<contact_id>` on that worker's pay run; `CLOSE_SHIFT#<contact_id>` on a closed shift. A rule
never asks which event it is — `run_instances` only ever sees the rows that matched.

`sk = <n>#<name>` — `n` orders the cascade when several rows match, and `name` makes the row
addressable. Sorting the sk sorts by n (zero-padded).

**An instance is current config: active while the row exists, gone when it is deleted.** It is not a
time machine and does not need to be — the LEDGER is the historical record. A pay run freezes its
entry (deterministic entryId + timestamp, dedup on `(pk, sk)`), so once a period is posted, what was
withheld is fixed in the books and re-running no-ops; the W-2 sums those frozen entries, it never
recomputes. So a rate change is a delete-and-replace, not a new dated version. The one thing that DOES
need as-of dating — a platform tax table, where a new year is appended and both years must coexist —
lives in `params.py` (the `GENERAL` rows), a separate store a rule reads layered UNDER this row's
params at run time.

Bundled into consumers alongside `rules.py` (no infra of its own beyond its table).
"""

import json
import os
import time
from decimal import Decimal

from boto3.dynamodb.conditions import Key as _Key

from aws import table as _ddb_table

ANY = "*"                       # the catch-all value: matches every one of that callsite's subjects

# A key is `<CALLSITE>#<subject>`, and the callsite says WHEN it fires. Named for the moment rather
# than the object because the moment is what an owner is choosing: an item is read at three different
# ones (a line being priced, stock moving on a sale, the reorder loop) and those are three different
# automations that used to share `INVOICE_LINE#` and be told apart by hand-filtering in each caller.
#
# `modules/rules/callsites.py` declares them all with the libraries resolvable at each.

PAY_RUN = "PAY_RUN"                     # a worker's pay run — `PAY_RUN#<contact_id>`
CLOSE_SHIFT = "CLOSE_SHIFT"             # a shift closing — `CLOSE_SHIFT#<contact_id>`
INVOICE_LINE = "INVOICE_LINE"           # a line being added to an invoice — `INVOICE_LINE#<catalog_key>`
INVOICE_STATUS = "INVOICE_STATUS"       # an invoice entering a status — `INVOICE_STATUS#issued`
INVOICE_TAG = "INVOICE_TAG"             # a tag applied or removed — `INVOICE_TAG#disputed`
ITEM_TRANSITION = "ITEM_TRANSITION"     # an item entering a state — `ITEM_TRANSITION#REVENUE#paid`.
                                        # Canonical: answered from code, nothing to attach.
INVOICE_TEMPLATE = "INVOICE_TEMPLATE"   # a template expanding — `INVOICE_TEMPLATE#<name>`
ITEM_CREATED = "ITEM_CREATED"           # an inventory item being created — `ITEM_CREATED#*`
STOCK_SOLD = "STOCK_SOLD"               # stock moving on a sale — `STOCK_SOLD#<catalog_key>`
STOCK_ADJUSTED = "STOCK_ADJUSTED"       # a count adjustment landing — `STOCK_ADJUSTED#*`
REORDER = "REORDER"                     # the reorder loop — `REORDER#<catalog_key>`
DISTRIBUTION = "DISTRIBUTION"           # an instrument's period — `DISTRIBUTION#<instrument_id>`
NEXT_VALUES = "NEXT_VALUES"             # what a value may become — `NEXT_VALUES#invoice_status#issued`
AUTOMATION = "AUTOMATION"               # a firm's own script asking — `AUTOMATION#<the script's own subject>`
PROPOSAL = "PROPOSAL"                   # a counterparty's proposal landing — `PROPOSAL#<kind>`


# Resolved at call time so a harness can point at a scratch table between cases.
def _table():
    return _ddb_table(os.environ["RULE_INSTANCES_TABLE"])


def key(kind, value):
    """The key a rule runs on: `INVOICE_LINE#room_deluxe`, `INVOICE_LINE#*`, `PAY_RUN#<contact_id>`. Code looks a rule
    up by the key the thing in hand already carries — an inventory key, a worker, a template name."""
    return f"{kind}#{value}"


def sort_key(n, name):
    """`sk = <n>#<name>`, n zero-padded so a lexical sk sort IS the order they run in."""
    return f"{int(n):04d}#{name}"


def for_key(pk):
    """Every instance that runs on one key, in `n` order."""
    rows, kwargs = [], {"KeyConditionExpression": _Key("pk").eq(pk)}
    while True:
        resp = _table().query(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(rows, key=lambda r: r["sk"])


def at(callsite: str, subject: str):
    """Every instance for one callsite on one subject: the rows keyed on it, plus that callsite's
    catch-all (`<CALLSITE>#*`). Merged and ordered by `n`, so a state tax and a city tax written at
    different `n` stack in the right order.

    The callsite is part of the key, so an item's three moments — a line being priced, stock moving
    on a sale, the reorder loop — return only their own rows. They used to share `REORDER#` and each
    hand-filter what came back.

    **No key, no instances.** An object with no catalog key was not sold — it was COMPUTED (a tax, a
    tip, a fee a rule added). Nothing bought it, so nothing taxes it, and `INVOICE_LINE#*` does not reach it
    either: the catch-all means "every subject this callsite sees", and a tax is not one. That is the whole of
    no-tax-on-tax — a property of what the object is, not a flag anyone has to set.

    An object whose key matches nothing returns [] — which is how "this is not taxable" is said. Not a
    flag on the item, not a rate of zero: simply no row."""
    if not subject:
        return []
    rows = for_key(key(callsite, ANY)) + for_key(key(callsite, subject))
    return sorted(rows, key=lambda r: r["sk"])


def all_rows():
    """Every instance row, `(pk, sk)`-ordered — the firm's whole automation config. The table is
    config, not data: a scan is a handful of rows."""
    rows, kwargs = [], {}
    while True:
        resp = _table().scan(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(rows, key=lambda r: (r.get("pk", ""), r.get("sk", "")))


def delete(matches, n, name):
    """Delete an instance row — the OFF switch: an instance is active while the row exists, and this
    is what removes it. Returns the deleted row, or None if no such row. On a key that carries a
    canonical default, deleting the firm row restores the canonical."""
    sk = sort_key(n, name)
    resp = _table().delete_item(Key={"pk": matches, "sk": sk}, ReturnValues="ALL_OLD")
    return resp.get("Attributes") or None


def add(matches, n, name, rule, param):
    """Write an instance: `rule`, with these `param`s, runs on whatever `matches` — at order `n`,
    under the `name` this firm calls it. That row is the entire act of "using" a rule; the agent
    writes it at onboarding once it knows the business.

    It is current config: writing the same (matches, n, name) replaces the row, deleting it turns the
    rule off. No versioning — a period already run is frozen in the ledger, so the config store has no
    history to keep (see the module docstring). Idempotent on (pk, sk)."""
    row = {
        "pk": matches,
        "sk": sort_key(n, name),
        "name": name,
        "rule": rule,
        "n": int(n),
        "param": param,
        "created_at": int(time.time() * 1000),
    }
    _table().put_item(Item=_to_ddb(row))
    return row


def _to_ddb(v):
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_ddb(x) for x in v]
    return v
