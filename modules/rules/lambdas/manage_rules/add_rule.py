"""op=add — add a rule to the business.

Writing this row is the whole act of "using" a rule. gradienterp does not deploy code so a firm can
have a sales tax or hire a Californian; the agent reads the business at onboarding, picks from the
rules we OFFER (`rule_params op=get catalog=true`), and writes rows.

    add_rule(matches="INVOICE_LINE#beans", name="ca_sales_tax", rule="multiply_item_value", n=300,
             param={factor: 0.0725, creditor: "SALES_TAX_PAYABLE", name: "CA sales tax"})

    add_rule(matches="PAY_RUN#c-ava-reyes", name="futa", rule="rate_posting", n=200,
             param={factor: 0.006, base: "gross", cap: "futa_wage_base", consumed: "gross_wages",
                    debit: "PAYROLL_TAX_EXPENSE", debitType: "EXPENSE",
                    credit: "FUTA_PAYABLE", creditType: "LIABILITY"})

What it MATCHES is also when it runs: code looks rules up by the key the thing in hand already
carries — `INVOICE_LINE#<inventory key>` when that item is priced onto a line (`#*` = every line),
`PAY_RUN#<contact_id>` on that worker's pay run, `CLOSE_SHIFT#<contact_id>` when one of their time-entries
closes. A person appears in these keys as their CONTACT_ID, never a name: the key is immutable, so a
name written into one is permanent, while a contact_id resolves through the contact and names them
only if that contact points at a public profile. Add a second rule on the same
key and BOTH run, in `n` order — a city tax on top of a state tax, Medicare on top of Social Security.
Nothing is seeded: a firm that owes no sales tax, or has no employees, simply has no rows.

A rule instance is current config: writing the same (matches, n, name) replaces the row, deleting it
turns the rule off. There is no versioning — a period already run is frozen in the ledger, so the
config store keeps no history (see `modules/rules/instances.py`). A worker's new W-4 or next year's
SDI rate is a delete-and-replace; the platform tax TABLES, which do need both years to coexist, live
in the separate `params.py` store, not here.

The rule name is fenced: it must resolve to a real `@rule` across the bundled libraries, so a typo (or
an attempt to name a module internal) fails here rather than at pay time.
"""

import json

from aws import json_default as _json_default

import instances
import general_rules   # the general rules: multiply_item_value, rate_posting
import payroll_rules   # labor's: wage_accrual, us_federal, ca_pit
import stock_rules     # inventory's: produce_on_sale (the made-to-order backflush)
import callsites as _callsites
import automation_rules  # the script-facing library: retry_order, retry_decision
import dispatch_rules  # run_automation — a callsite handing off to firm code
import collection_rules   # payments': charge_saved_card (attached to an invoice transition)
import metric_rules       # metrics': record_metric — a moment as a product event
import state_rules        # what a value may become: next_possible_values
import transition_rules  # invoicing's: what an item POSTS when it enters a state
import agreement_rules   # what a firm answers without a turn: accept_within, accept_in_stock, auto_order
import rules

RULE_LIBS = [general_rules, payroll_rules, stock_rules, collection_rules, automation_rules,
             dispatch_rules, state_rules, transition_rules, agreement_rules, metric_rules]
OFFERED = rules.offered_rules(RULE_LIBS)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    matches = body.get("matches")
    rule = body.get("rule")
    name = body.get("name")
    param = body.get("param") or {}
    n = body.get("n", 300)

    if not matches or "#" not in matches:
        return _err("matches is required, e.g. 'INVOICE_LINE#beans' (or 'INVOICE_LINE#*' for every item), "
                    "'PAY_RUN#<contact_id>' for a pay run, 'CLOSE_SHIFT#<contact_id>' for a closed shift")
    if not name:
        return _err("name is required — what this firm calls this use of the rule, e.g. 'ca_sales_tax'")
    if rule not in OFFERED:
        return _err(
            f"unknown rule: {rule}. Available: {sorted(OFFERED)}",
            available=sorted(OFFERED),
        )

    # Will anything ever RUN this row? A callsite queries a key kind and resolves a fixed set of
    # libraries (`modules/rules/callsites.py`), so a rule attached where its library is not loaded
    # raises `unknown rule` at run time — unattended, possibly months later. Refusing here moves that
    # to the moment the row is written, in a conversation, where it can be fixed.
    #
    # Safe to refuse rather than warn because every key kind in `instances.py` has a callsite
    # (`test_every_key_kind_has_a_callsite`), so the only way to reach this is a rule that genuinely
    # cannot run where it is being attached.
    reachable = _callsites.libs_for_key(matches)
    if not any(getattr(lib, rule, None) is not None
               for lib in RULE_LIBS if lib.__name__ in reachable):
        sites = [c.name for c in _callsites.for_key(matches)]
        if not sites:
            return _err(f"nothing reads {matches.split('#', 1)[0]}# keys, so a row on {matches} would "
                        f"never run. See rule_params op=get catalog=true for where rules attach.")
        return _err(
            f"{rule} cannot run at {matches} — that key is read by {', '.join(sites)}, which "
            f"{'resolves' if len(sites) == 1 else 'resolve'} {sorted(reachable)}. "
            f"rule_params op=get catalog=true callsite={sites[0]} lists what can attach there.",
            callsites=sites,
        )

    # Would this row ever be READ? Some subjects are answered from the module's own rows with the
    # table left unread, so a row on one is accepted, listed by the list op, and does nothing —
    # silent, on the surfaces where being wrong is worst. Which subjects those are is declared with
    # the callsite (`modules/rules/callsites.py`), which is the only registry a tool can read.
    fixed = _callsites.code_answers(matches)
    if fixed:
        return _err(
            f"{matches} is answered from code, so a row here would be stored and never read. "
            + (fixed[0].instead or f"nothing attaches at {matches}"),
            callsites=[c.name for c in fixed],
        )

    # A param is required exactly when the rule's SIGNATURE gives it no default — the spec knows this
    # (derived from the function), so there is no second list to keep in step.
    missing = [p for p, meta in OFFERED[rule].items() if meta.get("required") and p not in param]
    if missing:
        return _err(f"param is missing {missing} for {rule}", spec=OFFERED[rule])

    # a "pattern" param (applies_to) is agent-authored regex fullmatched against short keys at
    # run time — validate it compiles and cap its length here, so a bad pattern fails at
    # authoring instead of silently matching nothing every sale.
    for p, meta in OFFERED[rule].items():
        if meta.get("type") == "pattern" and param.get(p):
            pat = str(param[p])
            if len(pat) > 200:
                return _err(f"{p} is too long (200 char cap)")
            try:
                import re
                re.compile(pat)
            except re.error as e:
                return _err(f"{p} is not a valid regex: {e}")

    row = instances.add(matches=matches, n=n, name=name, rule=rule, param=param)
    return _ok({
        "added": {"matches": row["pk"], "sk": row["sk"], "rule": rule, "name": name, "param": param},
        "note": "this now runs whenever that key comes up; add another to stack.",
    })


def _ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body, default=_json_default)}


def _err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, default=_json_default)}
