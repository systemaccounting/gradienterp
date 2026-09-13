"""dispatch rules — what a callsite attaches to hand a moment to the firm's own code.

One rule. A firm that wants something to happen when an invoice is issued, or a tag applied, attaches
`run_automation` there and names its script. That row is the whole configuration: no operator, no
deploy, and the twelve callsites need no per-firm branches.

**Its own library because `callsites.py` fences by LIBRARY.** Folding it in with `automation_rules`
— what a running script asks — would make it attachable at the `automation` callsite too, so a script
could dispatch at its own subject, which is the tight self-repeat nothing refuses. Two moments, two
libraries.

**Not in `general_rules` either.** That one resolves at `pay_run` and `invoice_line`, which fold what
rules return into journal line items; a dispatch row there would be a leg with no account, no side
and no amount.

**It emits rather than invoking.** The callsite is mid-write and has already decided what it is
doing; a synchronous script could stall an invoice transition or fail it. So the moment is announced
and `automate` picks it up, which also means a slow script costs the write nothing.

**It resolves the script's rule instances here.** `automate` runs untrusted code and holds no DDB;
this runs inside a callsite lambda that already has the instance table. A rule's params cannot depend
on a result the script has not produced yet, so they are always knowable before it starts — see
`modules/rules/AGENTS.md` on why that is what makes a rule a rule.
"""

from typing import Annotated

import events
import instances
from rules import rule


@rule
def run_automation(
    ctx,
    script:   Annotated[str,  "string", "The script to run, as a file name under the firm's approved prefix (e.g. 'collections/charge_next_card.py'). It must already be approved — this only asks for it by name."],
    subjects: Annotated[list, "list",   "Which `AUTOMATION#<subject>` rows the script will ask for, resolved here and sent with it. A script asking for a subject not listed gets nothing and takes its own default path."] = (),
):
    """Announce this moment for a firm's own script to handle.

    What the script receives as `params` is this callsite's own object — an invoice transition sends
    the invoice id and the status it entered. The engine's provenance list is stripped: it is a
    record of which rules ran, not something a script has any use for.
    """
    params = {k: v for k, v in ctx.items() if k != "rule_exec_id"}
    events.emit("rules", "automation.requested", {
        "script": script,
        "params": params,
        "rules": {s: instances.at(instances.AUTOMATION, s) for s in subjects},
    })
    return [{"requested": script}]


@rule
def add_scheduled_automation(
    ctx,
    script:     Annotated[str,  "string", "The script to run, by name under the firm's approved prefix (e.g. 'collections/notice.py'). It must already be approved — this only asks for it by name."],
    expression: Annotated[str,  "string", "How often, as a Scheduler expression — 'rate(3 days)'. Leave empty with `after` set for a single run."] = "",
    after:      Annotated[str,  "duration", "Run ONCE this long from now, e.g. '15 days'. Resolved when the callsite fires, so the row says 'fifteen days after this happens' rather than a date that goes stale."] = "",
    until:      Annotated[str,  "duration", "Stop repeating after this long, e.g. '15 days', and delete the schedule. Also resolved at dispatch. An ISO instant works too where a fixed date is genuinely meant."] = "",
    starting:   Annotated[str,  "duration", "Nothing runs before this long from now."] = "",
    names_it:   Annotated[str,  "field",  "Which field of the thing in hand names this schedule — 'invoice_id' at an invoice transition. Two invoices then get two schedules; without it they compose one name and the second is refused as a duplicate."] = "",
    subjects:   Annotated[list, "list",   "Which `AUTOMATION#<subject>` rows the script will ask for, resolved now and sent with it."] = (),
):
    """Schedule this firm's own script instead of running it now.

    The sibling of `run_automation`, and the same announcement: the callsite is mid-write, so this
    emits and something else creates the schedule. `modules/automation` consumes it, which is what
    keeps the approval check and the scheduler grant in the module that owns scheduling — a callsite
    lambda gets no ability to create schedules by having this row attached.

    **A step schedules the next step.** An invoice going unpaid gets a chase and a deadline; the
    deadline's script schedules what follows it. Nothing lays out the whole sequence in advance, so a
    later step exists only because an earlier one ran and found the reason still true, and a customer
    who pays on day three never had a day-thirty action to cancel.

    Every delay is relative to whatever created it. Nothing reads the original date or does
    arithmetic against it, so the absolute days are a property of the sequence rather than a number
    anything stores.
    """
    if not expression and not after:
        return [{"refused": script, "why": "needs `expression` to repeat or `after` to run once"}]

    # A row is static and a schedule needs an instant, so the offsets resolve HERE — at the moment
    # the callsite fires. `after: "15 days"` written in a row means fifteen days after whatever just
    # happened, which is the only reading that stays true; a stored date is a date that goes stale
    # the first time it passes.
    when = expression or f"at({_offset(after)[:-1]})"
    detail = {
        "script": script,
        "schedule_expression": when,
        "subject": str(ctx.get(names_it) or "") if names_it else "",
        "start_date": _offset(starting),
        "end_date": _offset(until),
        # `at()` fires once, so the schedule removes itself. A repeat bounded by `until` completes
        # at that date and removes itself too; one with neither runs until something stops it.
        "one_shot": when.strip().startswith("at("),
        "params": {k: v for k, v in ctx.items() if k != "rule_exec_id"},
        "rules": {s: instances.at(instances.AUTOMATION, s) for s in subjects},
    }
    events.emit("rules", "automation.scheduled", detail)
    return [{"scheduled": script, "when": when, "until": detail["end_date"]}]


def _offset(value: str) -> str:
    """'15 days' -> an ISO instant that far ahead. An ISO instant passes through, so a genuinely
    fixed date is still expressible; anything unparseable is treated as absent rather than guessed
    at, since a wrong date on a teardown is worse than no bound at all."""
    v = (value or "").strip()
    if not v or v[0].isdigit() and "-" in v[:5]:      # already an instant
        return v
    import datetime, re
    m = re.fullmatch(r"(\d+)\s*(second|minute|hour|day|week)s?", v, re.I)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = datetime.timedelta(**{f"{unit}s": n})
    return (datetime.datetime.now(datetime.timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
