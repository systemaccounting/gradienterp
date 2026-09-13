"""state rules — what a value may become next.

Anything with a lifecycle asks the same question. An invoice is `draft`, `issued`, `paid`; a PO is
`open` then `received`; a shipment, a task, a firm's own tag on any of them. The thing that decides
which moves are legal is not different per module, so it is one rule and every module contributes
rows.

    NEXT_VALUES#<what>#<current>  ->  the values `current` may become

**A projection, not a predicate.** Asked "may this become paid?" a rule would answer yes or no, and
`run_instances` has no veto — two instances on one key both run and neither sees the other, so one
saying no could not stop the other saying yes. Asked "where can this go from here?" it returns rows
like every other rule: two instances union into more destinations, which is what stacking means
everywhere in this catalog, and no rows means nothing is permitted. Refused and unconfigured become
the same answer, and it is the safe one.

It also reads the way a caller thinks. Holding an issued invoice, you ask where it may go — not which
statuses list `issued` as a permitted predecessor.

**Whose rows answer is the whole of the platform/firm line.** A module keeps CANONICAL rows in code
for the values it cannot let a firm redefine — an invoice status is a money position, and issuing
debits the only ACCOUNTS_RECEIVABLE there is, so `draft → paid` is money that never entered the
ledger. Everything else reads the table, which is how a firm builds a sequence over its own tags with
no deploy and no operator, and how it gets to be wrong and have its agent fix it.

Nothing here knows an invoice. `next_possible_values` takes a value and returns values.
"""

from typing import Annotated

from rules import rule


@rule
def next_possible_values(
    ctx,
    values: Annotated[list, "list", "The values this one may become. A move to anything else is refused. Listing nothing permits nothing, which is what a value with no row already means."] = (),
    when:   Annotated[str,  "pattern", "Optional regex the CURRENT value must fullmatch for these to be offered. Empty offers them always. This is what lets one row cover a family of values rather than one row each."] = "",
):
    """The values `ctx["current"]` may become.

    ctx: `{"current": "<value>", …}` — whatever else the caller knows rides along.

    Returns one row per permitted value. The caller checks membership — it asked for the set, so it
    does not need this to have an opinion about the move it has in mind.
    """
    current = str(ctx.get("current") or "")
    if when:
        import re
        if not re.fullmatch(when, current):
            return []
    return [{"next": v} for v in values]
