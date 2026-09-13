"""The general rules — owned by nobody.

Two functions that between them cover a startling amount of what a business owes anyone. A sales tax,
a district tax, a gratuity, a platform fee, a royalty are all `multiply_item_value`. FUTA, CA SUI,
CA ETT, SDI and both halves of FICA are all `rate_posting`. None of them is code: each is a rule
INSTANCE, a row naming one of these with its own params.

They live here rather than in a domain module because nothing about them is domain-specific — a rule
is a general function, and modules with nothing to do with each other end up calling the same one.

**Reuse is not a requirement.** When a rule's ALGORITHM genuinely differs — a progressive bracket
worksheet is not a multiply — write another general rule rather than contorting one of these. Just
consider how it composes first. What is NOT allowed is a per-tenant or per-jurisdiction function: the
rates and tables are params, always.

Each param is annotated where it is declared, so `spec()` reads the rule's inputs straight off the
signature and what the agent is offered can't drift from what the function consumes.
"""

import re
from typing import Annotated

from rules import rule, rule_added_item, mul, clamp, _D
from rules import debit as _debit, credit as _credit   # aliased: `debit`/`credit` are PARAM names below


@rule
def multiply_item_value(
    item,
    factor:       Annotated[float, "rate",    "Multiplied by the item's value. 0.0725 = 7.25%."],
    creditor:     Annotated[str,   "account", "The account the added item is payable to — SALES_TAX_PAYABLE for a tax, TIPS_PAYABLE for a gratuity."],
    name:         Annotated[str,   "string",  "What the added item is called on the invoice, e.g. 'CA sales tax'."] = "tax",
    creditorType: Annotated[str,   "string",  "The creditor account's type. LIABILITY when you are holding the money for someone else (a tax, a tip); REVENUE when you are earning it (a fee)."] = "LIABILITY",
    applies_to:   Annotated[str,   "pattern", "Optional regex fullmatched against the item's catalog key. Empty = every item this instance's key dispatches. '^\\d+#beans$' = beans at every location; '^2#beans$' = one location's."] = "",
):
    """A transaction object worth `factor` × another object's value, payable to `creditor`.

    That is the entire rule. A 7.25% CA sales tax payable to SALES_TAX_PAYABLE and an 18% gratuity
    payable to TIPS_PAYABLE differ only in three values, so neither is code.

    The object it creates has no catalog key — nothing bought it — so no instance matches it and a tax
    cannot be taxed. The engine stamps it with the key of the instance that ran, and appends the
    exec id to both it and the object it was computed from.

    Returns [] rather than a zero-value object — `post_journal_entry` rejects a non-positive leg.

    `applies_to` is matching-as-a-param: dispatch delivers candidates (the instance's key), the
    rule's parameters refine. The condition runs here, in the function."""
    if applies_to:
        key = item.get("catalog_item_id") or item.get("item_id") or ""
        if not re.fullmatch(applies_to, key):
            return []
    amount = mul(item.get("amount", 0), factor)
    if amount <= 0:
        return []
    return [rule_added_item(
        name=name,
        amount=float(amount),
        account=creditor,
        account_type=creditorType,
    )]


@rule
def rate_posting(
    ctx,
    factor:     Annotated[float, "rate",   "Multiplied by the base. 0.006 = 0.6%."],
    debit:      Annotated[str,   "string", "The account debited — PAYROLL_TAX_EXPENSE for an employer tax; WAGES_PAYABLE for an employee withholding (it comes out of net pay)."],
    credit:     Annotated[str,   "string", "The account credited — FUTA_PAYABLE, SUTA_PAYABLE, FICA_PAYABLE…"],
    base:       Annotated[str,   "string", "Which ctx field is the base — 'gross' for a payroll tax."] = "gross",
    cap:        Annotated[float, "money",  "Only the slice of the base under this annual cap is charged (a wage base). Omit for an uncapped rate."] = None,
    consumed:   Annotated[str,   "string", "With `cap`: which ctx.ytd field says how much of the cap is already used (e.g. 'gross_wages'). The cap is a running annual one."] = "gross_wages",
    debitType:  Annotated[str,   "string", "EXPENSE | LIABILITY | ASSET."] = "EXPENSE",
    creditType: Annotated[str,   "string", "Usually LIABILITY (you now owe it to someone)."] = "LIABILITY",
):
    """`factor` × a base, optionally capped, posted as a two-legged journal entry.

    Nearly every payroll tax IS this. FUTA, CA SUI, CA ETT, SDI and the Social Security half of FICA
    differ only in a rate, a wage base, and which two accounts move — so they are five INSTANCES of
    this one rule, and none of them is code. The Medicare half of FICA is the same rule with no `cap`.
    An employer tax debits an expense; an employee withholding debits WAGES_PAYABLE (it comes out of
    what you owe them) — that difference is a param, not a function.

    `cap` + `consumed` express a RUNNING annual cap: only the slice of this period's base still under
    the cap is charged, so a worker who has already passed the wage base is charged nothing and the
    rule returns no effects at all (rather than a zero-amount leg, which post_journal_entry rejects).

    Returns [] when nothing is due — a rule that produces nothing is how "this doesn't apply this
    period" is said. Whether it applies to this WORKER is not the rule's business: that is which
    instances exist."""
    amount_base = _D(ctx.get(base, 0))
    if cap not in (None, ""):
        used = _D((ctx.get("ytd") or {}).get(consumed, 0))
        amount_base = clamp(amount_base, lo=0, hi=_D(cap) - used)
    amount = mul(amount_base, factor)
    if amount <= 0:
        return []
    return [
        _debit(debit, amount, debitType),
        _credit(credit, amount, creditType),
    ]
