"""callsites — every place a module reaches the instance table, declared once.

A rule instance is a row on a key. A callsite is the query for that key plus the libraries that may
be resolved there, and until now both halves were written inline at each of the twelve sites and
collected nowhere. So nothing could answer "what can I attach to the pay run", and a rule attached
where its library is not loaded failed at `_resolve` — at run time, possibly unattended.

    PAY_RUN.instances(worker_id)  →  the rows
    PAY_RUN.libs                  →  what may resolve there

**Declared here rather than at the callsite** because the callsites are lambda handlers with
module-level env reads, so a tool cannot import them to read a registry. Same reason
`modules/schemas/_registries.py` is one shared file: it is what makes the set readable.

**Not on the rule.** A rule does not know its key — `multiply_item_value` has no idea invoicing looks
it up at `INVOICE_LINE#`. Labelling the function would be a second source of truth that drifts, which is
the invariant `spec()` exists to hold (derived, never declared).

Libraries are named, not imported, and resolved by the caller passing them. That keeps this file free
of every rule library — a lambda bundles what its own callsite needs.
"""

import instances



class Callsite:
    """One place a module runs rules: a key kind, and the libraries resolvable there."""

    def __init__(self, name, kind, libs, describes, canonical=(), instead=""):
        self.name = name
        self.kind = kind
        self.libs = tuple(libs)          # module NAMES; the caller imports and passes the objects
        self.describes = describes
        self.canonical = tuple(canonical)   # subjects answered from code — see answers_from_code
        self.instead = instead              # where a firm's own version of that decision goes

    def answers_from_code(self, subject):
        """Is this subject one the module answers from its own rows, leaving the table unread?

        A pattern ending `#*` covers a whole namespace, the way `INVOICE_LINE#*` means every item.
        Anything else is one exact subject, because the neighbouring ones are a firm's to write:
        `REVENUE#paid` is what collecting cash means and `REVENUE#settled` is a hotel's own move.
        """
        for pat in self.canonical:
            if pat.endswith("#*"):
                if subject == pat[:-2] or subject.startswith(pat[:-1]):
                    return True
            elif subject == pat:
                return True
        return False

    def key(self, value):
        return instances.key(self.kind, value)

    def instances(self, subject):
        """This callsite's rows for one subject, plus its catch-all."""
        return instances.at(self.kind, subject)

    def __repr__(self):
        return f"<callsite {self.name} on {self.kind}# via {'+'.join(self.libs)}>"


CALLSITES = [
    # general_rules is deliberate here, not sloppy: `rate_posting` is the general engine for any
    # payroll rate (its params name PAYROLL_TAX_EXPENSE, FUTA_PAYABLE, 'gross'), and payroll_rules
    # holds the named ones built on it.
    Callsite("pay_run", instances.PAY_RUN, ["payroll_rules", "general_rules", "metric_rules"],
             "a worker's pay run — withholdings and employer taxes"),
    Callsite("close_shift", instances.CLOSE_SHIFT, ["payroll_rules", "metric_rules"],
             "a shift closing — the wage accrual"),
    Callsite("invoice_line", instances.INVOICE_LINE, ["general_rules"],
             "a line being added to an invoice — taxes, tips, fees on what is sold"),
    Callsite("invoice_status", instances.INVOICE_STATUS, ["collection_rules", "dispatch_rules", "metric_rules"],
             "an invoice entering a status — what should happen when it is issued or paid"),
    Callsite("invoice_tag", instances.INVOICE_TAG, ["collection_rules", "dispatch_rules", "metric_rules"],
             "a tag applied to or removed from an invoice — the firm's own automation"),
    Callsite("item_transition", instances.ITEM_TRANSITION, ["transition_rules", "metric_rules"],
             "an invoice ITEM entering a state — what it posts",
             # What collecting cash means, and that money held for someone else is never earned.
             # Every OTHER state reads the table, which is how a firm's own move posts: a hotel
             # collecting at `settled` writes on `REVENUE#settled` what canonical holds for `paid`.
             canonical=("REVENUE#paid", "REVENUE#earned", "REVENUE#refunded",
                        "LIABILITY#paid", "LIABILITY#refunded"),
             instead="attach it to the state your firm actually moves money on — a hotel collecting "
                     "at `settled` writes on ITEM_TRANSITION#REVENUE#settled"),
    # Not "what happens when a value is entered" — that is the callsite above and INVOICE_STATUS#.
    # This is asked EARLIER, by whoever is about to make the move: may it be made at all. One
    # callsite for every module because nothing about it is per-module; the subject says what it is
    # about (`invoice_status`, `invoice_tag`, a PO's state) and what the value is now.
    Callsite("next_values", instances.NEXT_VALUES, ["state_rules"],
             "a value about to change — what it may become. `NEXT_VALUES#<what>#<current>`",
             # A STATUS is a money position and `issue_invoice` debits the only ACCOUNTS_RECEIVABLE
             # there is, so an invoice reaching `paid` without passing `issued` is money that never
             # entered the ledger. The whole namespace answers from `modules/invoicing/status_rules.py`
             # and the table is never read for it. A firm's own vocabulary is `invoice_tag`, whose
             # sequences ARE its rows on this same kind.
             canonical=("invoice_status#*",),
             instead="a firm's own sequence goes on its TAGS — NEXT_VALUES#invoice_tag#<tag>, whose "
                     "rows are read from the table"),
    Callsite("invoice_template", instances.INVOICE_TEMPLATE, ["template_rules"],
             "a template expanding into an item set"),
    Callsite("item_created", instances.ITEM_CREATED, ["catalog_rules"],
             "an inventory item being created — the defaults stamped onto it"),
    Callsite("stock_sold", instances.STOCK_SOLD, ["stock_rules", "metric_rules"],
             "stock moving on a sale — the made-to-order backflush"),
    Callsite("stock_adjusted", instances.STOCK_ADJUSTED, ["stock_rules", "metric_rules"],
             "a count adjustment landing — how the variance is valued"),
    Callsite("reorder", instances.REORDER, ["stock_rules", "agreement_rules", "metric_rules"],
             "the reorder loop reading a par level — and, with `auto_order`, issuing the PO itself"),
    # A counterparty's proposal, the moment it is stamped on this firm's mirror. What a firm can
    # say as a policy — take offers from X up to N, answer a PO from the shelf — runs here with no
    # model call; the agent is poked only for a proposal no row permits an answer to.
    Callsite("proposal_received", instances.PROPOSAL, ["agreement_rules"],
             "a counterparty's proposal landing — what this firm answers without a turn. `PROPOSAL#<kind>`"),
    Callsite("distribution", instances.DISTRIBUTION, ["treasury_rules"],
             "an instrument's period — what it pays its holder"),
    # A firm's own script, not a lambda handler like the twelve above. Declared here for the same
    # two reasons: `get_rule_param catalog=true callsite=automation` lists what can attach, and
    # `add_rule` refuses a row whose library does not load here. `general_rules` is listed because a
    # script may use it too.
    Callsite("automation", instances.AUTOMATION, ["automation_rules", "general_rules"],
             "a firm's own script deciding — how many tries to make, whether to keep going"),
]

BY_NAME = {c.name: c for c in CALLSITES}


def for_kind(kind: str) -> list:
    """The callsite for this key kind. A list because the lookup is by prefix and stays tolerant, but
    the key names the callsite now, so it is one."""
    return [c for c in CALLSITES if c.kind == kind]


def for_key(pk: str) -> list:
    """Every callsite that would read this instance key (`INVOICE_LINE#doppio` → the ITEM ones)."""
    return for_kind(pk.split("#", 1)[0]) if "#" in pk else []


def code_answers(pk: str) -> list:
    """The callsites that answer `pk` from code, so a row written on it would never be read."""
    if "#" not in pk:
        return []
    kind, subject = pk.split("#", 1)
    return [c for c in for_kind(kind) if c.answers_from_code(subject)]


def libs_for_key(pk: str) -> set:
    """The library names that could resolve a rule attached to `pk` — the union across its
    callsites, since a row on `INVOICE_LINE#doppio` runs at whichever of them fires."""
    return {lib for c in for_key(pk) for lib in c.libs}
