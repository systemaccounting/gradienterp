"""The automation rules — written to be asked by a firm's own script.

`modules/automation` runs firm-authored code with no tool allowlist, by design: a firm never waits on
an operator to automate something new. So the guard on a reckless script was never going to be which
tools it may call. It is that the parts worth getting right are not IN the script.

    judgment  →  rules      what order, how many, is this worth another try
    effect    →  tools      the charge, the email, the write

A script is glue between them. A reviewer reading one should see sequencing and not somebody's
opinion about card declines — an opinion in a script is reviewed once, by whoever approved that
script, while the same opinion here is written once and promised to every firm. And it puts the
knobs where an owner can reach them: the parts of an automation that live in an instance are the
parts its owner retunes without sending the script back through review.

**Primarily for scripts, not exclusively.** This is a library, not a fence. A platform callsite may
resolve it if a rule here genuinely fits, and a script may ask `general_rules` — the `automation`
callsite lists both. What the separate file buys is that every function in it exists for the same
reason, and that a reader of `payroll_rules` is not wondering which parts are for scripts.

Nothing here knows a processor. `retry_order` takes a list and returns a list; `retry_decision` takes
an outcome and returns whether to go on. Both would read the same in a shipping script or a payroll
one.

What STARTS a script is `run_automation`, in `dispatch_rules.py` — a separate library because it is
attached at a different moment, by a callsite rather than asked by a script, and `callsites.py`
fences by library.
"""

from typing import Annotated

from rules import rule


@rule
def retry_order(
    ctx,
    limit:      Annotated[int,  "count", "How many attempts the script may make in total. The cap is here rather than in the script because retrying the same thing many ways in a row is what a processor's fraud tooling reads as someone testing stolen cards — a firm that wants two attempts and a firm that wants one both edit this number."] = 3,
    selected_first: Annotated[bool, "bool", "Try whatever the subject already selected before the rest. Off means the list is attempted in the order it arrived."] = True,
):
    """The attempts to make, in order, from the candidates the caller gathered.

    ctx: `{"candidates": [{"id": …}, …], "selected": "<id>"}` — ids of anything attemptable. This
    rule does not know what they are; a saved card, a carrier, and a payout rail all sort the same.

    Returns one row per attempt rather than reordering the caller's list. The engine stamps every
    returned row with the instance that produced it, so building new rows keeps that stamp off the
    caller's own data — and a rule that mutates what it was handed is not one this catalog wants.

    Two instances attached means two sets of attempts, concatenated in `n` order, which is what
    instances always mean. If that is not wanted, attach one.
    """
    # int(), because a param read back from DDB is a Decimal and a Decimal cannot index a slice.
    # Arithmetic params get away without this; a count that becomes a position does not.
    limit = int(limit)
    cands = list(ctx.get("candidates") or [])
    sel = ctx.get("selected") or ""
    if selected_first and sel:
        cands = [c for c in cands if c.get("id") == sel] + [c for c in cands if c.get("id") != sel]
    return [{"attempt": i + 1, "id": c.get("id"), "candidate": c}
            for i, c in enumerate(cands[:max(0, limit)])]


@rule
def retry_decision(
    ctx,
    stop_on: Annotated[list, "list", "Status codes that END the loop — the failure will repeat whatever else is tried. 409 is the platform's own 'the payer has to come back': `charge_saved_method` returns it for a card needing browser confirmation, and no other card fixes that."] = (409,),
    give_up_after: Annotated[int, "count", "Stop once this many attempts have already failed, whatever their status. 0 = no limit beyond the caller's own list."] = 0,
):
    """Whether a failed attempt is worth another one.

    ctx: `{"status": <int>, "attempt": <int>, …}` — whatever the caller knows about the failure.

    Returns `[]` for stop and one row for continue, because that is what a rule returning nothing
    already means everywhere else in this catalog: `multiply_item_value` returns `[]` rather than a
    zero-value item, and a distribution returns nothing when the cap is reached. A script reads the
    result for truth.

    Encoding "409 means stop" in a script means every firm's script has to know it, and the first one
    that does not spends three declines learning.
    """
    status = ctx.get("status")
    attempt = int(ctx.get("attempt") or 0)
    give_up_after = int(give_up_after)
    if status in tuple(stop_on):
        return []
    if give_up_after and attempt >= give_up_after:
        return []
    return [{"retry": True, "after_status": status, "attempt": attempt}]
