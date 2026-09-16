import json
import logging
import os
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP

from boto3.dynamodb.conditions import Key as _Key

import collection_rules
import dispatch_rules
import metric_rules   # modules/metrics: record_metric — a moment as a product event, by a row
import status_rules
from status_rules import STATUSES, next_statuses
import instances
import journal
import rules
import events

from aws import client as _aws, table as _ddb_table, log as alog

log = logging.getLogger()

# this gerp's id (the `from` on addressed events) + the shared bus
GERP_ID = os.environ.get("GERP_ID", "")
OP_EVENT_BUS_ARN = os.environ.get("OP_EVENT_BUS_ARN")


# Resolved at call time so a harness can point at a scratch table between cases.
def table():
    return _ddb_table(os.environ["INVOICES_TABLE"])


def items_line_table():
    """The invoice's LINES, as range rows."""
    return _ddb_table(os.environ["INVOICE_LINES_TABLE"])


def transitions_table():
    return _ddb_table(os.environ["TRANSITIONS_TABLE"])


def items_table():
    """inventory's catalog — create_from_template resolves emitted keys against it."""
    return _ddb_table(os.environ["ITEMS_TABLE"])


POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def to_ddb(v):
    """Recursively coerce floats to Decimal for DDB."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


def now_ms() -> int:
    return int(time.time() * 1000)


def new_invoice_id() -> str:
    return uuid.uuid4().hex


# ─── invoice store (DDB; local = one jsonl line per invoice snapshot, read=scan / put=rewrite-all) ───

# Lines live in their OWN table as range rows (`pk = invoice_id`, `sk = item#<item>#range#<n>`), not
# nested on the invoice. Two reasons, and neither is tidiness:
#
#   1. a range states what a per-unit list enumerates — fifty units with a modifier on the first ten
#      is three rows, not fifty entries;
#   2. it can carry a GSI, so "every sale of the cappuccino" is a Query instead of the full-table
#      scan `query_invoices` still does for everything else.
#
# `get_invoice` hydrates them back into `inv["lines"]`, so callers keep the shape they had. The
# invoice row keeps customer, status, totals and authorship.

def _line_rows(invoice_id: str) -> list[dict]:
    rows = items_line_table().query(
        KeyConditionExpression=_Key("invoice_id").eq(invoice_id) & _Key("sk").begins_with("item#")
    ).get("Items", [])
    return sorted(rows, key=lambda r: r.get("seq", 0))


def get_invoice(invoice_id: str) -> dict | None:
    inv = table().get_item(Key={"invoice_id": invoice_id}).get("Item")
    if inv is None:
        return None
    tags = read_tags(invoice_id)
    if tags:
        inv["tags"] = [t["tag"] for t in tags]
    lines = _line_rows(invoice_id)
    if lines:  # an invoice written before the split still carries its own lines
        inv["lines"] = [{k: v for k, v in r.items() if k not in ("invoice_id", "sk", "seq", "gsi_item", "gsi_sk")}
                        for r in lines]
    return inv


def put_invoice(inv: dict):
    """The invoice to the invoices table, its lines to the range table. A line's `item_id` IS its sk."""
    lines = inv.get("lines") or []
    row = {k: v for k, v in inv.items() if k != "lines"}
    created = int(inv.get("created_at") or now_ms())
    rows = []
    for n, ln in enumerate(lines):
        sk = ln.get("item_id") or range_sk(ln.get("catalog_item_id") or ln.get("description"), n + 1)
        item = sk.split("#")[1] if sk.startswith("item#") else "misc"
        rows.append({**ln, "invoice_id": inv["invoice_id"], "sk": sk, "seq": n,
                     # the GSI: one product across time, so a loss-leader or a per-server ranking is
                     # a Query rather than a scan of every invoice ever written
                     "gsi_item": f"item#{item}", "gsi_sk": f"{created:013d}#{inv['invoice_id']}"})
    table().put_item(Item=to_ddb(row))
    lines = items_line_table()
    for r in rows:
        lines.put_item(Item=to_ddb(r))


def rollup(inv: dict) -> list[dict]:
    """The invoice's lines with modifiers folded into what they modify.

    A cappuccino's real margin is not the cappuccino's — it is the cappuccino plus the extra foam
    plus the two sugars, priced and costed together. Read the lines flat and every modifier looks
    like a separate 50-cent sale; read them rolled and you can see which menu item actually earns.

    Cost comes from the line's OWN `unit_cost`, copied at build from the catalog as of that sale.
    It is deliberately not re-read from the catalog here: an invoice records what was charged AND
    what it cost THEN, so raising a catalog price today must not move last year's margin. A line
    with no copied cost contributes revenue and no cost, and says so (`costed: False`) rather than
    reporting a margin that silently treats it as free — which is also what a pre-copy legacy line
    now does, honestly, instead of being re-costed at today's prices."""
    by_id = {ln.get("item_id"): ln for ln in inv.get("lines") or []}
    kids = {}
    for ln in inv.get("lines") or []:
        parent = ln.get("modifies")
        if parent and parent in by_id:
            kids.setdefault(parent, []).append(ln)

    def cost_of(ln):
        if ln.get("unit_cost") is None:
            return None
        return round(float(ln["unit_cost"]) * int(ln.get("quantity") or 1), 2)

    out = []
    for ln in inv.get("lines") or []:
        if ln.get("modifies") in by_id:
            continue                                   # folded into its parent below
        mods = kids.get(ln.get("item_id"), [])
        rev = money(line_total(ln) + sum((line_total(m) for m in mods), Decimal(0)))
        costs = [cost_of(x) for x in [ln] + mods]
        costed = all(c is not None for c in costs)
        cost = money(sum((money(c) for c in costs if c is not None), Decimal(0)))
        out.append({
            **ln,
            "modifiers": mods,
            "rolled_revenue": rev,
            "rolled_cost": cost,
            "costed": costed,
            **({"rolled_margin": round((rev - cost) / rev, 4)} if costed and rev else {}),
        })
    return out


def invoices_for_item(item: str, start_ms=None, end_ms=None) -> list[dict]:
    """Every line that sold `item`, oldest first — the query the ledger cannot answer cheaply.
    "Which menu item is a loss-leader", "who sells the most per hour" are both item-across-invoices,
    and without this they mean reading a CSV export instead of the books."""
    lo = f"{int(start_ms or 0):013d}#"
    hi = f"{int(end_ms):013d}#~" if end_ms else "9999999999999#~"
    # the item-index GSI is the read path. The local path scanned and filtered in Python, so a
    # query the index cannot actually serve still passed.
    return items_line_table().query(
        IndexName="item-index",
        KeyConditionExpression=_Key("gsi_item").eq(f"item#{_item_key(item)}") & _Key("gsi_sk").between(lo, hi),
    ).get("Items", [])


def query_invoices(status=None, customer=None) -> list[dict]:
    rows = table().scan().get("Items", [])
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if customer:
        rows = [r for r in rows if r.get("customer") == customer]
    return rows


# ─── rules-params + the inventory catalog (what create_from_template resolves against) ───


def catalog_item(item_id: str) -> dict | None:
    """One item from `modules/inventory`'s catalog — the definition a template's emitted KEY
    resolves to (name, unit, unit_price, unit_cost, revenue_account). Read straight off inventory's
    items table (the same direct cross-module read inventory does against settings)."""
    return items_table().get_item(Key={"item_id": item_id}).get("Item")


# ─── the item pass: rule INSTANCES attached to what's being sold ───
#
# A tax is not a special journal leg and not a special rule — it is another ITEM, produced by a general
# generic rule that was ATTACHED to the thing being sold. Building an invoice, we take the inventory
# key of every item we're adding, look up the instances attached to it, and run them.
#
# The lookup IS the decision. Nothing asks "is this taxable": a bag of beans is taxed because someone
# attached a tax instance to `beans`, and an hour of consulting isn't because nobody attached one to
# it. Two instances on the same item (a state tax and a city tax) both run, in `n` order — a stacked
# tax needs no code. Derived items are never re-matched, so tax-on-tax cannot happen.

def rule_added_items(lines):
    """The items the rule instances ADD to these lines — a tax, a district tax, a tip, a fee. Each
    line's inventory key is looked up in the instance table; whatever matches runs. Each added item
    carries a `rule_key` back to the instance that added it (stamped by `run_instances`)."""
    import rules            # lazy: only the lambdas that bundle the engine run the item pass
    import general_rules
    import instances

    added = []
    for ln in lines:
        insts = instances.at(instances.INVOICE_LINE, ln.get("catalog_item_id"))
        if insts:
            added += rules.run_instances(ln, insts, modules=[general_rules])
    return added


# ─── invoice assembly (shared by create_invoice + create_from_template) ───

# Where a REVENUE item's credit waits until cash lands. A contra-asset: the credit balance nets
# against ACCOUNTS_RECEIVABLE, so net receivables read zero until collection and REVENUE means
# earned AND collected. Released to the item's real revenue account by record_invoice_paid.
# Why, and what does NOT defer: `modules/invoicing/AGENTS.md` § realized revenue.
HOLD_ACCOUNT = "REVENUE_PENDING"

# What a transaction item may credit. REVENUE is what you SELL; LIABILITY is what you COLLECT on
# someone else's behalf (a sales tax you hold for the state, a tip you hold for staff, a deposit).
# EQUITY is the migration case: an open invoice imported at cutover credits OWNER_EQUITY — its
# revenue was earned in the old system's books. The difference is only where the money lands.
_CREDIT_TYPES = {"REVENUE", "LIABILITY", "EQUITY"}


DEFAULT_LOCATION = "1"  # LOCATION#1 = main, the default by doctrine


def _compose_invoice_id(invoice_id, location):
    """`invoice_id = <location ordinal>#<id>` for platform-generated ids. An explicit id is kept
    verbatim (an already-prefixed id declares its own location; a thread id — cross-firm — is a
    peer-shared agreement key that must never be prefixed). Returns (invoice_id, location)."""
    if invoice_id:
        head, _, rest = str(invoice_id).partition("#")
        if head.isdigit() and rest:
            return str(invoice_id), head
        return str(invoice_id), (location or DEFAULT_LOCATION)
    location = location or DEFAULT_LOCATION
    return f"{location}#{new_invoice_id()}", location


# ─── the line model: quantity as ranges ───
#
# A line is a catalog item plus a quantity, and modifiers carve RANGES out of that quantity:
#
#   item#cappuccino#range#1   1-2   two plain
#   item#cappuccino#range#3   3-3   the third one…
#   item#extra-foam#range#3   3-3   …has extra foam
#
# Three cappuccinos with one modified is three rows, and fifty with a modifier on the first ten is
# STILL three rows — that is what a range buys over enumerating an id per unit.
#
# The range key is the line's IDENTITY: `item#<item>#range#<start>` is what `transition_item` hangs
# per-unit state off. So ordinals NEVER renumber in flight — voiding unit 3 of 10 leaves a gap and
# 4-10 keep their numbers, because renumbering would silently repoint every transition at the wrong
# unit. Compacting gaps later, deliberately, is fine.
#
# Overlapping ranges are ADDITIVE, not a conflict: two ranges both adding extra foam to unit 3 means
# it was ordered twice and is charged twice. There is no winner to pick.

RANGE_SK = "item#{item}#range#{start}"


def _item_key(raw) -> str:
    """The sk's item segment. Slugged, because a description falls back into this position when a
    line carries no catalog binding and a raw one would put spaces and `#` inside the sort key."""
    import re
    return re.sub(r"[^a-z0-9_-]+", "-", str(raw or "misc").lower()).strip("-") or "misc"


def range_sk(item: str, start: int) -> str:
    return RANGE_SK.format(item=_item_key(item), start=int(start))


def line_ranges(lines):
    """[(sk, line)] — assign each line its range key. `quantity` defaults to 1; ranges run
    consecutively per catalog item so two separate lines of the same item don't collide."""
    nxt, out = {}, []
    for ln in lines:
        item = _item_key(ln.get("catalog_item_id") or ln.get("description"))
        qty = int(ln.get("quantity") or 1)
        start = nxt.get(item, 1)
        nxt[item] = start + qty
        ln["range_start"], ln["range_end"] = start, start + qty - 1
        ln["quantity"] = qty
        out.append((range_sk(item, start), ln))
    return out


CENT = Decimal("0.01")


def money(v) -> Decimal:
    """Any number → an exact Decimal amount, quantized to the cent.

    Via `str()`, always: `Decimal(0.1)` is the binary expansion (0.1000000000000000055…) while
    `Decimal(str(0.1))` is 0.1. Rounding is HALF_UP because that is what an invoice means by
    rounding — Python's own `round()` is HALF_EVEN, which differs on an exact .005 tie."""
    if isinstance(v, Decimal):
        return v.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(v or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def line_total(ln) -> Decimal:
    """A line's money. COMPUTED from quantity x unit_price — the authoritative record of what was
    charged is the posted journal entry, so this is a derivation, not a stored fact. `amount` is
    still accepted as the total for callers that price a line directly rather than per unit.

    Returns a **Decimal**, and every sum built from it stays Decimal to the point it is stored or
    serialized. Money never touches binary floating point in between: one multiply-then-round
    survives a float round-trip by luck, and the moment a discount or a second tax multiplier joins
    the expression it stops surviving. JSON transport is lossless either way (a Decimal serializes
    to a number whose repr round-trips exactly) — it is the ARITHMETIC that has to be exact."""
    if ln.get("unit_price") is not None:
        return money(Decimal(str(ln["unit_price"])) * int(ln.get("quantity") or 1))
    return money(ln.get("amount"))


def line_is_incomplete(ln) -> bool:
    """Whether this line still needs the agent. An EMPTY VALUE is the whole signal — there is no
    `ask`/`fixed` schema, because the ticket already says what it doesn't know by arriving without
    it. A POS improvises; something has to turn that into an entry the protocol accepts."""
    priced = ln.get("unit_price") is not None or ln.get("amount") is not None
    if not priced:
        return True
    if line_total(ln) > 0 and (not ln.get("account") or ln.get("accountType") not in _CREDIT_TYPES):
        return True
    return False


def invoice_is_incomplete(inv) -> bool:
    return any(line_is_incomplete(ln) for ln in inv.get("lines") or [])


def authed_by(event) -> str:
    """The VERIFIED subject that wrote this row. Never read from the request body.

    Two ingress paths, two mechanisms, one guarantee:
      - an HTTP route behind the gerp API's JWT authorizer → the validated claims are on the event
      - the agent, via the AgentCore gateway → no claims reach here (the gateway authorizes by IAM),
        so the runtime passes the subject IT verified. The chat lambda checks the JWT signature
        itself and pins `claims.sub` per turn, so this is a propagated verified subject, not a
        caller's assertion. The gateway is not a public surface.

    Empty means nobody was signed in — a poker, a scheduled invoke, a stream consumer. Callers that
    know they are automation should pass AUTHOR_AGENT explicitly rather than leave it blank: blank
    means "unknown", which is a different and worse claim than "the agent did this"."""
    claims = (event or {}).get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {}) or {}
    return claims.get("sub") or str((event or {}).get("_authed_by") or "")


AUTHOR_AGENT = "agent"          # the gerp's own automation wrote it — a fact, not an absence


def carry_authorship(new: dict, prior: dict) -> dict:
    """Carry a row's original authorship across a rewrite.

    `put_invoice` rewrites the whole row, so any path that REBUILDS an invoice from a request would
    silently re-author it to whoever touched it last — turning an audit field into a "most recent
    editor" field, which is the one thing it must never be. Stamped once at create, carried forever.

    A later edit is not a re-authoring. If who-changed-it matters, that is a transition on the item
    (`transition_item`), which is the grain that exists for exactly this."""
    if not prior:
        return new
    for f in ("authed_by", "created_by"):
        if prior.get(f):
            new[f] = prior[f]
        else:
            new.pop(f, None)
    return new


def build_invoice(customer, lines, due_date="", memo="", invoice_id=None,
                  allow_unbilled=False, location="", job="", authed_by="", created_by=""):
    """Validate + assemble an invoice row. Each line is a catalog item plus a quantity, and gets a
    RANGE key (`item#<item>#range#<start>`) which is the grain `transition_item` hangs state off.

    `allow_unbilled` admits **zero-amount** lines: an operational task (a `clean` at rate 0) that is
    worth nothing to bill but everything to track, so the invoice is the work order as well as the
    bill. It contributes 0 to the subtotal and needs no revenue account — it can never post, since
    `post_journal_entry` rejects a non-positive leg and a task's states (`done`) match no money rule
    anyway.

    **A draft may be INCOMPLETE and that is not an error.** A line can arrive without a price or an
    account — a POS pushing an entry it can't express in the protocol's terms — and the invoice still
    builds, flagged `incomplete` for the agent to resolve. Nothing is lost by allowing it, because a
    draft is inert: `create_invoice` posts no journal entry, and a draft cannot be paid
    (`record_invoice_paid` requires `issued`). The completeness gate is `issue_invoice`, which is
    where money actually moves, and it has to exist there whatever this function does.

    Returns `(invoice, None)` or `(None, error_message)`."""
    if not customer:
        return None, "customer (contact_id) is required"
    if not lines or not isinstance(lines, list):
        return None, "lines is required (a non-empty list)"

    subtotal = Decimal(0)
    for ln in lines:
        # ABSENT is a hole; WRONG is an error. A line arriving without an account is a POS that
        # doesn't know where the revenue lands — the agent resolves that. A line asserting
        # `accountType: ASSET` is a caller mistake, and accepting it would let a draft claim
        # something that can never post. Only the second is rejected.
        at = ln.get("accountType")
        if at is not None and at not in _CREDIT_TYPES:
            return None, f"accountType must be one of {sorted(_CREDIT_TYPES)}, got {at!r}"
        amt = line_total(ln)
        if amt < 0:
            return None, "a line cannot be negative"
        if amt == 0 and not (allow_unbilled or line_is_incomplete(ln)):
            return None, "each line needs a positive amount"
        subtotal += amt

    # The range key IS the line's identity, and its sk in the lines table. `transition_item` hangs
    # per-unit state off it, which is why ordinals never renumber in flight.
    for sk, ln in line_ranges(lines):
        ln["item_id"] = sk
        # a ticket is not authored once — a barista opens it, someone adds a pastry, a manager comps
        # a line. A line inherits the invoice's author unless it names its own.
        if authed_by:
            ln.setdefault("authed_by", str(authed_by))
        if created_by or authed_by:
            ln.setdefault("created_by", str(created_by or authed_by))
        # copy the cost AS OF THIS SALE off the catalog. `rollup` reads this, never the catalog —
        # what a unit cost to make on the day it sold is an event fact, and re-reading it later
        # made a catalog price change silently rewrite every historical margin.
        if ln.get("catalog_item_id") and ln.get("unit_cost") is None:
            item = catalog_item(ln["catalog_item_id"])
            if item and item.get("unit_cost") is not None:
                ln["unit_cost"] = float(item["unit_cost"])

    seen = [ln["item_id"] for ln in lines]
    if len(set(seen)) != len(seen):
        return None, f"item_id must be unique within an invoice: {seen}"

    # `subtotal` is what you SOLD; `tax` is what you're holding for someone else (the items a rule
    # added — a tax, a tip, a fee). Both are lines; the split is only for reporting, and the journal
    # doesn't care. There is no `tax_rate` on an invoice: what a thing costs in tax is decided by the
    # rule instances that MATCH it, not by a number carried on the sale.
    tax = money(sum((line_total(ln) for ln in lines if ln.get("rule_key")), Decimal(0)))
    subtotal = money(subtotal - tax)

    invoice_id, location = _compose_invoice_id(invoice_id, location)
    inv = {
        "invoice_id": invoice_id,
        "location": location,  # the row attr every posting path reads — never parsed at post time
        **({"job": str(job)} if job else {}),  # per-job P&L: a dimension VALUE, not an object
        # authorship at two grains. `authed_by` is the fact (who was signed in) and is never
        # settable; `created_by` is the attribution (whose sale this is) and defaults to it. They
        # diverge when someone acts for someone else — a manager ringing a ticket for a server —
        # which is git's author/committer split, and the attribution is only trustworthy because
        # the fact sits beside it. Only `created_by` becomes a journal dimension; `authed_by` is
        # audit, not a slice of the business.
        **({"authed_by": str(authed_by)} if authed_by else {}),
        **({"created_by": str(created_by or authed_by)} if (created_by or authed_by) else {}),
        "customer": customer,
        "lines": lines,
        "subtotal": subtotal,
        "tax": tax,
        "total": money(subtotal + tax),
        "status": "draft",
        "due_date": due_date or "",
        "memo": memo or "",
        "created_at": now_ms(),
    }
    if invoice_is_incomplete(inv):
        inv["incomplete"] = True                 # what the stream consumer fires on
    return inv, None


# ─── item transition store (the transaction object's grain) ───
#
# An invoice's items each carry their OWN append-only stream of timestamped transition rows;
# an item's current state is the fold (its latest row), and "the invoice" is the fold across
# items — never itself stateful. Append-only, so a correction is another row, never an edit.
#
#   pk = invoice_id                          → one query returns the whole object's history
#   sk = "{item_id}#{at}#{transition_id}"    → item-major, then chronological
#
# `at` is ISO-8601 aware-UTC (sorts lexically). Idempotency is checked against the rows we
# already read for the fold, NOT via a conditional put: `at` is generated per call, so a retry
# would land on a different sk and a condition would never fire (the post_journal lesson —
# an id alone can't dedup a key that bakes in a fresh timestamp).

def new_transition_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def append_transition(row: dict):
    """Append one transition row (append-only — no update path by design)."""
    transitions_table().put_item(Item=to_ddb(row))


def read_transitions(invoice_id: str) -> list[dict]:
    """Every transition row for an invoice, ordered by sk (item-major, then chronological)."""
    rows, kwargs = [], {"KeyConditionExpression": _Key("invoice_id").eq(invoice_id)}
    while True:
        resp = transitions_table().query(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return rows


# An invoice-level row: something that happened TO THE INVOICE rather than to one of its lines.
# A timer added for an invoice runs, does its work and deletes itself, so unless it writes here
# there is no record it ever existed — and "we chased on day 3, 6 and 9" belongs on the invoice, not
# in a lambda log that expires.
#
# It rides the same stream so the history reads in one order, and takes a reserved item_id so the
# fold below can tell it from a line. `#` is already illegal in a generated item_id (`item#…#range#…`
# uses it as a separator), so nothing real can collide with this.
INVOICE_LEVEL = "#invoice"


def fold_items(rows: list[dict]) -> dict:
    """The fold: item_id → its latest transition row. This IS an item's current state — it is
    computed from the stream, never stored on the item.

    Invoice-level rows are skipped: they are not a line and folding them in would report a phantom
    item on every invoice that was ever chased."""
    latest = {}
    for r in rows:  # sk-ordered, so the last row seen per item is its newest
        item_id = r.get("item_id")
        if not item_id or item_id == INVOICE_LEVEL:
            continue
        latest[item_id] = r
    return latest


def invoice_level_rows(rows: list[dict]) -> list[dict]:
    """The other half of the same stream — what happened to the invoice itself, oldest first."""
    return [r for r in rows if r.get("item_id") == INVOICE_LEVEL]


# ─── cross-module: post_journal_entry ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


# ─── addressed events (the sell-side send) ───

def emit_event(detail_type: str, detail: dict):
    """Addressed to one firm on the shared bus. A thin name over `events.emit_to` so
    this module's callers keep reading `emit_event(kind, {"to": …})` — the source is
    this module, which is the one thing the four hand-written copies differed by."""
    to = detail.get("to", "")
    return events.emit_to("invoicing", to, detail_type,
                          {k: v for k, v in detail.items() if k != "to"})


# ─── the invoice's status machine ───
#
# Every edge in one place, and one function that walks one. Before this, each lambda carried its own
# `if status != X` and its own `status = Y`, so the machine existed as an agreement between files
# that never referenced each other.
#
# The statuses are STANDARD and modules depend on them — they are money positions, not vocabulary:
# not billed, billed, collected. `issue_invoice` debits the only ACCOUNTS_RECEIVABLE there is, so a
# status a module cannot name is an invoice outside the ledger. What a firm names its own is a TAG
# (below), which carries no accounting meaning. New statuses are added here, canonically, when the
# accounting wants one.

# The moves themselves are rows in `modules/invoicing/status_rules.py`.


def guard_transition(inv: dict, to: str) -> str | None:
    """The reason `inv` may not enter `to`, or None. Called before the caller does its work, so a
    refused transition costs nothing."""
    if to not in STATUSES:
        return f"{to} is not a status an invoice can enter (one of: {', '.join(STATUSES)})"
    allowed = next_statuses(inv["status"])
    if to not in allowed:
        return (f"invoice {inv['invoice_id']} is {inv['status']}; "
                + (f"it can only become {' or '.join(allowed)}" if allowed
                   else f"{inv['status']} is where an invoice stops")
                + f", not {to}")
    return None


def transition_invoice(inv: dict, to: str, **fields) -> tuple[dict, list]:
    """Move `inv` to `to`, write it, then run whatever the firm attached to `INVOICE#<to>`.

    Returns `(inv, ran)`. `fields` are whatever rides the status — `issued_at`, `payment_entry_id`.
    The rules run AFTER the write, so a rule announcing the transition announces something true.
    """
    was = inv["status"]
    inv["status"] = to
    inv.update(fields)
    put_invoice(inv)
    return inv, _run_transition_rules(inv, to, was)


def _run_transition_rules(inv: dict, to: str, was: str) -> list:
    """A rule that throws does not undo the transition — the caller asked for it and it happened.

    It prints in the shape `create_inc_from_log` files on, against the same subject payments uses
    for the same invoice, so whichever half loses a collection strikes one incident stream.
    """
    ctx = {
        "invoice_id": inv["invoice_id"],
        "customer":   inv.get("customer"),
        "total":      inv.get("total"),
        "from":       was,
    }
    try:
        return rules.run_instances(
            ctx, instances.for_key(instances.key(instances.INVOICE_STATUS, to)), modules=[collection_rules, dispatch_rules, metric_rules])
    except Exception as e:  # noqa: BLE001
        alog.exception("invoice status rules failed after the transition", invoice_id=inv["invoice_id"], status=to)
        print(json.dumps({"event": "transition_rules_failed", "incident": "fail",
                          "subject": f"collection:{inv['invoice_id']}", "category": "collection",
                          "label": f"Collection for invoice {inv['invoice_id']}",
                          "state": to, "error": str(e)}))
        # one row in the place the rule results go, so the 200 says the rules did not run
        return [{"rule_key": instances.key(instances.INVOICE_STATUS, to), "failed": True, "error": str(e)}]


# ─── tags — what a firm calls things, as opposed to what the accounting calls them ───
#
# A SET: many per invoice, unordered, independent. Applying `checked-out` does not remove
# `checked-in`. That is a line rather than an omission — exclusivity leads to ordering, ordering to
# legal transitions, and legal transitions are the status machine again with none of its guarantees.
#
# Rows in the LINES table beside the line items, not a set attribute on the invoice: a set cannot be
# a GSI key, and "every invoice tagged X" has to be a Query. `get_invoice` already reads that table.

TAG_REGISTRY = "invoice_tags"
TAG_BUCKET = "common"


def _registry_table():
    return _ddb_table(os.environ["SCHEMA_TABLE"])


def tag_declared(tag: str) -> bool:
    """Whether the firm has declared `tag`. The registry is what makes a vocabulary comparable
    across firms, so an undeclared tag is refused rather than invented per-invoice."""
    got = _registry_table().get_item(
        Key={"registry": TAG_REGISTRY, "bucket_name": f"{TAG_BUCKET}#{tag}"}).get("Item")
    return bool(got)


def tag_sk(tag: str) -> str:
    return f"tag#{tag}"


def read_tags(invoice_id: str) -> list[dict]:
    """Every tag on an invoice, with who applied it and when."""
    rows = items_line_table().query(
        KeyConditionExpression=_Key("invoice_id").eq(invoice_id) & _Key("sk").begins_with("tag#")
    ).get("Items", [])
    return sorted(({"tag": r["sk"][4:], "applied_at": r.get("applied_at"),
                    "applied_by": r.get("applied_by", "")} for r in rows),
                  key=lambda t: t["tag"])


def put_tag(invoice_id: str, tag: str, applied_by: str = ""):
    """Idempotent — re-applying a tag it already has is a no-op write, not a second row."""
    items_line_table().put_item(Item=to_ddb({
        "invoice_id": invoice_id,
        "sk": tag_sk(tag),
        "gsi_tag": f"tag#{tag}",
        "gsi_sk": f"{now_ms():013d}#{invoice_id}",
        "applied_at": now_ms(),
        **({"applied_by": applied_by} if applied_by else {}),
    }))


def drop_tag(invoice_id: str, tag: str) -> bool:
    """True if it was there. Removing one that is not is not an error — the caller wanted it gone."""
    got = items_line_table().delete_item(
        Key={"invoice_id": invoice_id, "sk": tag_sk(tag)}, ReturnValues="ALL_OLD")
    return bool(got.get("Attributes"))


def invoices_tagged(tag: str, limit: int = 100) -> list[str]:
    """The invoices carrying `tag`, newest first — a Query on `tag-index`, never a scan."""
    rows = items_line_table().query(
        IndexName="tag-index",
        KeyConditionExpression=_Key("gsi_tag").eq(f"tag#{tag}"),
        ScanIndexForward=False, Limit=limit,
    ).get("Items", [])
    return [r["invoice_id"] for r in rows]


def run_tag_rules(invoice_id: str, tag: str, applied: bool) -> list:
    """Whatever the firm attached to this tag. `INVOICE_TAG#<tag>` on apply,
    `INVOICE_TAG#<tag>#removed` on remove — a firm automating the put and the take-back separately.

    A rule that throws does not undo the tag: the caller asked to tag it and it is tagged.
    """
    key = instances.key(instances.INVOICE_TAG, tag if applied else f"{tag}#removed")
    try:
        return rules.run_instances({"invoice_id": invoice_id, "tag": tag},
                                   instances.for_key(key), modules=[collection_rules, dispatch_rules, metric_rules])
    except Exception as e:  # noqa: BLE001
        alog.exception("invoice tag rules failed after the tag", invoice_id=invoice_id, key=key)
        print(json.dumps({"event": "tag_rules_failed", "incident": "fail",
                          "subject": f"invoice-tag:{tag}", "category": "invoicing",
                          "label": f"Rules on tag `{tag}`",
                          "invoice_id": invoice_id, "error": str(e)}))
        return [{"rule_key": key, "failed": True, "error": str(e)}]


# ─── responses ───

def ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
