"""The moves an invoice may make, as rows rather than as a literal.

`next_possible_values` (`modules/rules/state_rules.py`) is general — a PO's states and a firm's own
tags ask it the same question — so what is invoice-specific is only which rows answer.

CANONICAL, so the table is not read for them and there is nowhere to put a different opinion: a
status is a money position, and `issue_invoice` debits the only ACCOUNTS_RECEIVABLE there is, so an
invoice reaching `paid` without passing `issued` is money that never entered the ledger. A firm's own
vocabulary is a TAG, which carries no accounting meaning and whose sequences ARE its own rows on the
same key kind.

Beside `transition_rules.py` at the module root rather than inside `lambdas/_helpers.py`, because
`add_rule` reads this to refuse a row it would store and never look at, and a lambda helper reads
env at import.
"""

import instances
import rules
import state_rules

STATUS_VALUES = "invoice_status"


def _canonical(current, values):
    return {"pk": instances.key(instances.NEXT_VALUES, f"{STATUS_VALUES}#{current}"),
            "sk": instances.sort_key(100, "canonical"), "n": 100,
            "name": "canonical", "rule": "next_possible_values", "param": {"values": values}}


CANONICAL_STATUS = [
    _canonical("draft",  ["issued"]),
    _canonical("issued", ["unpaid", "paid"]),
    # a charge that failed is chased, and paying ends the chase
    _canonical("unpaid", ["paid"]),
]

# Every status an invoice can be in, for messages and for the "is this even a status" check. Derived
# so it cannot drift from the rows: the ones you can leave, plus the ones you can reach.
STATUSES = sorted({r["pk"].rsplit("#", 1)[-1] for r in CANONICAL_STATUS}
                  | {v for r in CANONICAL_STATUS for v in r["param"]["values"]})


def next_statuses(current: str) -> list:
    """What an invoice in `current` may become. Canonical rows answer and the table is not read."""
    key = instances.key(instances.NEXT_VALUES, f"{STATUS_VALUES}#{current}")
    rows = [r for r in CANONICAL_STATUS if r["pk"] == key]
    return [r["next"] for r in rules.run_instances({"current": current}, rows,
                                                   modules=[state_rules])]
