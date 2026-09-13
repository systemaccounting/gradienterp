"""modules/rules — the rule engine.

**A rule is a general function.** It computes something — a transaction object, a journal posting,
a field value — and it is written once, for everyone. It never knows a tenant, a jurisdiction, or an
industry; whatever varies is a parameter.

**A rule instance is a named use of a rule, storing its parameters.** One row: the rule's name, the
params it runs with, the key it applies to, and an `n` that orders it against other instances on the
same key. `INVOICE_LINE#room_deluxe → 0300#ca_sales_tax → multiply_item_value(factor=.0725, …)`.

**Modules call rules.** Invoicing, building an invoice line for a hotel room, queries the instances
for that room's inventory key, and runs each one. This is controlled extension of module behavior: a
firm shapes what a posting contains — a tax, a fee, a gratuity, a payout — by adding instance rows,
and the platform ships no code for any of it.

A rule returns a list and the module that called it uses it. `charge_saved_card` also sends an event
on the firm's bus.

Automation that reacts to things rather than being reached by a callsite is not this module.

## where a record came from

Everything a rule creates says which rule instance made it, and everything a rule reads says it was read:

  rule_key       the instance that created this object — `INVOICE_LINE#beans|0300#ca_sales_tax`. Stamped on
                 everything it returned: transaction objects, journal postings, defaults. An object with no
                 `rule_key` was not created by a rule (a hand-typed invoice line).
  rule_exec_id   a LIST. One id is created per rule invocation and appended to the object the rule
                 READ *and* to everything it CREATED. So a hotel room that triggered a tax carries the
                 exec id (it was read) but no rule_key (no rule made it), while the tax carries both —
                 and the exec id is what ties the two together afterwards.

## what a rule returns

Whatever its callsite needs. No tag, no vocabulary, nothing to collect — the caller called it, so it
knows what it asked for, and it uses the returned list directly.

  debit() / credit()                  a journal-entry leg     → post_journal_entry's lineItems
  catalog_item() / rule_added_item()  a transaction object    → a line on the invoice
  default()                           a {field, value} pair   → folded by defaults(), stamped on a
                                                                record being created

Each is a convenience for the shape its callsite wants, not a member of a taxonomy. `run_instances`
writes `rule_key` and `rule_exec_id` onto each returned dict, which is the one thing it adds beyond
calling the functions in order.

No infra of its own — bundled into the lambdas that call it, alongside the rule libraries they
resolve names against.
"""

import inspect
import uuid
from decimal import Decimal
from itertools import islice
from typing import get_args, get_origin, Annotated


# ── the shapes a rule returns; the module that called it uses them ────

def debit(account, amount, account_type):
    return {"side": "DEBIT", "account": account, "accountType": account_type, "amount": amount}


def credit(account, amount, account_type):
    return {"side": "CREDIT", "account": account, "accountType": account_type, "amount": amount}


def catalog_item(item, qty=1, **attrs):
    """A transaction object drawn FROM THE CATALOG: `qty` of an inventory `item_id`. One catalog
    covers both kinds a transaction needs — a `room_deluxe` is a CAPACITY item (booking it is
    inventory's `reserve`), a `minibar_coke` a STOCK item — so the key never has to say which store it
    came from. `attrs` ride onto it as overrides (a promo `rate`, a specific `account`, `memo`, dims);
    name / uom / rate (`unit_price`) / COGS (`unit_cost`) resolve from the catalog at build time.

    It keeps its catalog key, so rules still match it — a room-night a template created is taxable
    like any other line. See modules/invoicing/template_rules.py."""
    return {"item": item, "qty": qty, **attrs}


def rule_added_item(name, amount, account, account_type, **attrs):
    """A transaction object a rule COMPUTED from another one — a tax, a tip, a fee, a royalty. It is
    an object like any other: it lands on the transaction, carries its own account, and walks its own
    states.

    That IS the trick. A tax is not a special leg spliced into a journal entry by a lambda that knows
    what a tax is; it is another transaction object, and the entry falls out of the objects uniformly.
    Nothing downstream needs a concept of "tax".

    It has NO catalog key — nothing bought it, it was computed — so no rule instance matches it, and
    tax-on-tax is impossible by construction rather than by a guard anyone has to remember."""
    return {
        "description": name, "amount": amount,
        "account": account, "accountType": account_type,
        **attrs,
    }


def default(field, value):
    """The default value for a `field` on a record being CREATED. The caller stamps it onto the row
    (an explicit caller-supplied value wins), so the stored record is explicit and nothing has to
    infer at read time.

    The point is WHERE the decision lives. A default buried in a lambda is invisible: the agent can't
    find it, can't change it, and can't tell it was ever a decision. As a rule it is a named place
    with params — discoverable, and re-pointable by writing a row."""
    return {"field": field, "value": value}


# ── the rule marker, and the fence ──────────────────────────────────────────

def rule(fn):
    """Mark a function as a rule.

    A rule instance names its rule as a STRING, so this marker is the fence: a name only ever
    resolves to a function wearing it, never to a module internal.

    It takes no arguments. A rule carries no trigger and no order — WHEN it runs is a module deciding
    to call it, and its order is the instance's `n`. And it carries no param spec, because its
    SIGNATURE is the spec: `spec()` reads it straight off the function, so what the agent is offered
    is exactly what the function consumes and the two cannot drift.

    Signature: `fn(ctx, **params) -> [dict]`. Annotate each param with its type, a semantic kind and
    a description; a param with no default is required.

        @rule
        def multiply_item_value(
            item,
            factor:   Annotated[float, "rate",    "Multiplied by the item's value. .0725 = 7.25%."],
            creditor: Annotated[str,   "account", "Who the money is owed to — SALES_TAX_PAYABLE."],
            name:     Annotated[str,   "string",  "What it's called on the invoice."] = "tax",
        ):

    An instance's params are passed as keyword args, so a typo'd param is a TypeError at call time
    rather than a value silently ignored."""
    fn.is_rule = True
    return fn


def spec(fn):
    """A rule's param spec, read off its signature: {name: {type, description, default?, required}}.

    Derived, never declared — a rule that reads a param the agent was never told about, or advertises
    one it never reads, is not expressible. The first parameter is the object the rule runs on (the
    ctx), not a param, so it is skipped.

    Each param is `Annotated[<python type>, <kind>, <description>, <options?>]`: the kind is what the
    agent renders it as (rate / money / account / enum…), and an enum carries its allowed values."""
    out = {}
    for i, (name, p) in enumerate(inspect.signature(fn).parameters.items()):
        if i == 0:
            continue
        meta = get_args(p.annotation)[1:] if get_origin(p.annotation) is Annotated else ()
        entry = {"type": meta[0] if len(meta) > 0 else "string",
                 "description": meta[1] if len(meta) > 1 else "",
                 "required": p.default is inspect.Parameter.empty}
        if len(meta) > 2:
            entry["options"] = list(meta[2])
        if p.default is not inspect.Parameter.empty:
            entry["default"] = p.default
        out[name] = entry
    return out


def _resolve(name, modules):
    """The rule named `name`, searched across the rule libraries, fenced by the marker — an instance
    naming a junk rule (or a module internal) fails closed instead of executing something arbitrary."""
    for m in modules:
        fn = getattr(m, name, None)
        if fn is not None and getattr(fn, "is_rule", False):
            return fn
    raise ValueError(f"unknown rule: {name}")


def offered_rules(modules):
    """{name: param spec} for every rule available — what an onboarding agent is OFFERED, so it knows
    which instances it can write. Nothing is seeded; the agent reads the business and writes rows."""
    return {name: spec(getattr(m, name))
            for m in modules for name in dir(m)
            if getattr(getattr(m, name), "is_rule", False)}


def rule_key(inst):
    """The key of a rule instance — `pk|sk`, e.g. `INVOICE_LINE#beans|0300#ca_sales_tax`. The row's own
    primary key, so anything stamped with it points straight back at the row that created it: which
    rule, on what, at what order, in which version."""
    return f"{inst.get('pk', '')}|{inst.get('sk', '')}"


def run_instances(ctx, instances, modules):
    """Run each rule instance against `ctx`, in the order they arrive (their `n`), and return the flat
    list of what they returned.

    `ctx` is the object the instances were found for: a transaction object (found by its inventory
    key), a worker's pay context, an instrument's period. Two instances on the same key both run — a
    city tax stacks on a state tax, a Medicare leg on a Social Security leg.

    **Every instance sees the same `ctx`, and none sees what another returned.** This is a fold with
    no feedback: `n` decides who goes first, and that is the whole of the sequencing available here.
    No rule writes to `ctx` either — the `rule_exec_id` append below is the only write, and it is
    provenance. So `n` orders the returned rows and changes nothing that is computed, which is what
    lets a firm insert an instance mid-sequence without perturbing the ones around it.
    A rule cannot branch on an earlier rule's result, so a decision that depends on one is not
    expressible as a rule instance at all — it belongs in a script (`modules/automation`), which is
    the only caller that can run something, look at the answer, and decide what to do next.

    A callsite that needs ONE instance's return in particular gets it by position: order that
    instance last with its `n`. Nothing marks a slot as reserved, so a callsite relying on that owes
    the convention a comment where it reads the result.

    Where a record came from is stamped here, uniformly, so no rule can forget to:
      - one `rule_exec_id` per invocation, appended to `ctx` (the object READ) and to everything returned
        (everything CREATED). The hotel room ends up with the exec id and no rule_key; the tax it
        triggered ends up with both, and the exec id is what links them.
      - `rule_key` on everything returned — object, posting or default."""
    out = []
    for inst in instances:
        fn = _resolve(inst["rule"], modules)
        exec_id = f"rx_{uuid.uuid4().hex[:16]}"
        key = rule_key(inst)
        ctx.setdefault("rule_exec_id", []).append(exec_id)      # the object it READ
        try:
            # `list(...)` so a rule may `return [...]` OR `yield` — and either way the call
            # happens HERE, inside the guard: a generator would defer a bad-param TypeError past it.
            returned = list(fn(ctx, **(inst.get("param") or {})))
        except TypeError as e:
            # a param the rule doesn't take, or a required one the instance never carried. Loud, and
            # named: which instance, which rule, which param — the alternative is a rule quietly
            # computing on a default nobody chose.
            raise TypeError(f"rule instance {key} → {inst['rule']}: {e}") from e
        for row in returned:                                    # everything it CREATED
            row["rule_key"] = key
            row.setdefault("rule_exec_id", []).append(exec_id)
            out.append(row)
    return out


# ── value rules — a rule that RETURNS A SCALAR, and reads another's ─────────

def value(instances, modules, role, ctx, count=None):
    """Read a VALUE rule over `instances` by `role` — the reorder loop's `required_count` /
    `order_required`, which produce a number rather than a list.

    **Value rules YIELD.** A value is a value over time, so the rule is a generator and the caller
    decides how much of it to pull: `count=None` (default) pulls the head — the value NOW, the single
    scalar most callers want — and `count=N` pulls up to N successive values (a setpoint's change
    points, a forecast's steps). A constant yields once and stops, so pulling the head costs one
    `next()` and nothing else; nothing has to be a generator later, so streaming never becomes a
    migration.

    `count` is NOT capped. An oversized pull just runs the invocation out, and Lambda reports that to
    the caller itself — an `Unhandled` function error carrying `Task timed out after N seconds` — so an
    invented ceiling would only stand in for a bound the platform already enforces truthfully. A tool
    exposing `count` says so in the param's description: a big pull may be cut off, which is fine, read
    again from where it stopped. Resuming needs no cursor — the read is pure, so the caller just moves
    the ctx forward (a later `ts`).

    `role` matches an instance's `name` (what the firm calls this use) OR its `rule` (the function),
    and the instance's OWN `rule` is what runs — so a value rule is PLUGGABLE: `order_required` composes
    `ev("required_count", …)` and gets whichever function the firm bound to that role (a constant, a
    seasonal, a forecast), never knowing which. `ctx` carries an `ev` closure returning the GENERATOR,
    so a rule reads another with an explicit `next(...)` (head) or iterates it (stream). Distinct from
    `run_instances` (every instance on a key, results collected) — a value read makes no record, so
    nothing is stamped."""
    def ev(r, c):
        for inst in instances:
            if inst.get("name") == r or inst.get("rule") == r:
                return iter(_resolve(inst["rule"], modules)(c, **(inst.get("param") or {})))
        raise ValueError(f"no value-rule for role '{r}' among the instances")
    gen = ev(role, {**ctx, "ev": ev})
    if count is None:
        return next(gen)
    return list(islice(gen, count))


# ── the one fold a caller needs ─────────────────────────────────────────────

def defaults(rows):
    """`[{field, value}, …]` folded to `{field: value}` — what `manage_stock (op: create_item)` stamps onto the record it
    is creating. Later instances (higher `n`) win, so a firm's own rule can override a canonical one.

    Not a collector: nothing is filtered. It is a fold, because a mapping is the shape that caller
    wants and a list of pairs is not."""
    return {r["field"]: r["value"] for r in rows}


def _D(v):
    return v if isinstance(v, Decimal) else Decimal(str(v))


# ── composable arithmetic ops — domain-blind, the primitives rules compose ──
# Most rules are just a binding of domain values onto these. A dividend is
# clamp(mul(base, rate), hi=cap−paid); a wage-base tax is mul(clamp(base, hi=base−ytd),
# rate). The finance names (`bracket`, `dividend`, `coupon`) are a layer over this
# arithmetic — `base` and `rate` carry the meaning, the ops don't know the domain.

def mul(base, rate):
    """A fraction of a base: base × rate, rounded to cents so every amount a rule produces is
    already cents-clean."""
    return (_D(base) * _D(rate)).quantize(Decimal("0.01"))


def clamp(x, lo=None, hi=None):
    """Bound x to [lo, hi] (each optional): max(lo, min(hi, x)). A `hi` below `lo`
    (e.g. a cap already exhausted, or a wage base already exceeded) drives the result
    to `lo` — so clamp(amount, lo=0, hi=cap−paid) is 0 once the cap is reached."""
    x = _D(x)
    if hi is not None:
        x = min(x, _D(hi))
    if lo is not None:
        x = max(x, _D(lo))
    return x


def _bracket_tax(amount, schedule):
    """Annual tax for `amount` against a (floor, base, rate) schedule: find the
    highest bracket whose floor the amount clears, then base + rate × (amount − floor).
    Both Pub 15-T and CA Method B publish their schedules in exactly this shape."""
    floor, base, rate = Decimal(0), Decimal(0), Decimal(0)
    for f, b, r in schedule:
        if amount >= f:
            floor, base, rate = _D(f), _D(b), _D(r)
        else:
            break
    return base + rate * (amount - floor)
