"""modules/treasury — the distribution rule (the holder's payout).

The investor side: a holder bought a non-voting claim that pays from the visible margin (see
README.md). One general rule — `distribution_share` — is that whole family. A perpetuity and a
capped dividend are not two functions; they are two INSTANCES of this one, differing by a `cap`
param. "10% of net income until $750k is returned" and "5% of net income for as long as you hold
it" are rows, not code.

    rule (code, here)      distribution_share(ctx, param) -> [posting]
    instance (data, DDB)   DISTRIBUTION#biz#investor#seed / 0100#net_income_percent_dividend
                           { rule: distribution_share,
                             param: { factor: 0.10, cap: 750000, holder: "investor" } }

**The attachment IS the dispatch.** An instrument pays because settlement attached an instance to
it. There is no rule set to consult, no name list, no cap-of-zero meaning "off" — an instrument with
nothing attached simply produces nothing. The instance row IS the instrument's terms.

Bundled into the distribution handler alongside the engine + the instance store; resolved by name
(`run_instances(ctx, instances.for_key(pk), modules=[treasury_rules])`).

Deferred: `interest_*` (rate × principal, calendar-fired) is this rule with `base="principal"` and a
different ctx — an instance, not code. The buyout (`pay_present_value`) retires an instrument rather
than paying from one, so it is a separate rule when it lands.
"""

from typing import Annotated

from rules import rule, debit, credit, mul, clamp, _D


@rule
def distribution_share(
    ctx,
    factor: Annotated[float, "rate",   "Multiplied by the base. 0.10 = 10% of the period's net income."],
    holder: Annotated[str,   "string", "Who the payable is owed to (a contact_id). A term of the instrument, carried on the dimensions of the entry and the distribution.paid event; the arithmetic ignores it."],
    base:   Annotated[str,   "string", "Which ctx field is the base — 'net_income' for a margin-anchored claim."] = "net_income",
    cap:    Annotated[float, "money",  "A LIFETIME ceiling on cumulative payout — 'pay 10% of net income until $750k is returned'. Omit for a perpetuity."] = None,
    paid:   Annotated[str,   "string", "With `cap`: which ctx field holds the lifetime paid to date. The handler folds it from the ledger."] = "cumulative_paid",
):
    """`factor` × a base, booked DR RETAINED_EARNINGS / CR DIVIDENDS_PAYABLE, until cumulative
    payouts reach `cap`.

    NOT `rate_posting`, though it looks like it. Both are factor × base with an optional cap, but
    the cap means a different thing and the difference is an order of magnitude. `rate_posting`'s
    cap is an annual WAGE BASE — it clamps the *base*, so only the slice of this period's base under
    the cap is charged. Here the cap is a LIFETIME ceiling on what the instrument has PAID — it
    clamps the *amount*, so the final period pays out exactly the remainder and the instrument then
    pays nothing forever. With cap 50k, 48k already paid, 100k of net income at 10%: this rule pays
    the 2,000 that finishes the deal; a wage-base clamp would pay 10% of 2,000 = 200.

    No distribution on a loss, with no `if` for it: a negative base yields a negative amount (or, under
    a cap, clamps to zero), and a non-positive amount returns no effects at all — which is also how a
    finished instrument says it is finished. `post_journal_entry` rejects a non-positive leg, so
    "nothing to pay" must be no legs, never a zero one.
    """
    amount = mul(ctx.get(base, 0), factor)
    if cap not in (None, ""):
        paid_to_date = _D(ctx.get(paid, 0))
        amount = clamp(amount, lo=0, hi=_D(cap) - paid_to_date)
    if amount <= 0:
        return []
    return [
        debit("RETAINED_EARNINGS", amount, "EQUITY"),
        credit("DIVIDENDS_PAYABLE", amount, "LIABILITY"),
    ]
