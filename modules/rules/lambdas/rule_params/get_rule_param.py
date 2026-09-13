"""op=get — read rules-params rows for an owner, a rule's param-spec, or the catalog.

Returns the current rows so the agent can show / confirm values, and (when a `rule` is
given) that rule's **param-spec** — what it consumes (the W-4 fields for `us_federal`, a
rate for `futa`, `{percentage, cap}` for `net_income_percent_dividend`) — so the agent
drives the onboarding interview from the spec rather than a hardcoded form. With
`catalog=true` it instead lists every available rule + spec (optionally filtered by
`trigger`) — the menu of offerable instruments. Read-only; writes go through op=set.

Specs come from reflection over the bundled rule libraries (general_rules + payroll_rules +
treasury_rules). If a lib is absent (a stripped build), its rules are just omitted.

Every rule in the catalog is the same kind: general code that does nothing until an INSTANCE names it
(`manage_rules` op=add). Its spec is read off its SIGNATURE, so the menu the agent sees is exactly what the
function consumes — a param it never reads can't be advertised, and one it reads can't be hidden.
"""

import json

from aws import json_default as _json_default

import callsites as _callsites

import params as H

try:
    import rules as _engine
    import general_rules as _general
    import payroll_rules as _payroll
    _RULE_LIBS = [_general, _payroll]
except ImportError:
    _engine = None
    _RULE_LIBS = []

try:
    import treasury_rules as _treasury   # the marketplace's distribution instruments
    _RULE_LIBS.append(_treasury)
except ImportError:
    pass

try:
    import stock_rules as _stock         # inventory's stock-movement rules (produce_on_sale)
    import collection_rules as _collection  # payments' charge_saved_card
    _RULE_LIBS.append(_stock)
    _RULE_LIBS.append(_collection)
except ImportError:
    pass

try:
    import automation_rules as _automation   # what a running script asks: retry_order, retry_decision
    _RULE_LIBS.append(_automation)
except ImportError:
    pass

try:
    import dispatch_rules as _dispatch       # run_automation — a callsite handing off to firm code
    _RULE_LIBS.append(_dispatch)
except ImportError:
    pass

try:
    import state_rules as _state             # what a value may become: next_possible_values
    import transition_rules as _transition   # invoicing's: what an item POSTS entering a state
    _RULE_LIBS.append(_state)
    _RULE_LIBS.append(_transition)
except ImportError:
    pass

try:
    import agreement_rules as _agreement     # what a firm answers without a turn: accept_within, accept_in_stock, auto_order
    _RULE_LIBS.append(_agreement)
except ImportError:
    pass



def _spec(rule):
    """The rule's param spec, read off its SIGNATURE (not a declared dict, so it can't drift from
    what the function actually consumes). None if the name isn't a rule."""
    if not _engine or not rule or rule == "_rules":
        return None
    try:
        return _engine.spec(_engine._resolve(rule, _RULE_LIBS)) or None
    except ValueError:
        return None


def _catalog(callsite=None):
    """Every rule across the bundled libs — the menu the agent picks from when it writes an instance.

    Each entry is {name, spec, callsites}: what the rule is called, what params it takes (each with a
    type, a description, and whether it's required — a param with no default is), and WHERE it can be
    attached. That last one is read from `modules/rules/callsites.py`, which is where each site
    declares the key it queries and the libraries resolvable there — a rule knows neither, so
    labelling the function would be a second source of truth that drifts.

    `callsite` narrows to one site's menu, which is what "what can I set up for the pay run" means.

    A site whose subjects are partly answered from code carries `canonical` — the subjects `add_rule`
    refuses because a row on them would be stored and never read — and `instead`, where a firm's own
    version of that decision goes.
    """
    libs = {m.__name__: m for m in _RULE_LIBS}
    sites = _callsites.BY_NAME
    if callsite and callsite not in sites:
        return None
    wanted = [sites[callsite]] if callsite else _callsites.CALLSITES

    out = []
    for name, spec in _engine.offered_rules(_RULE_LIBS).items():
        where = [c.name for c in wanted
                 if any(getattr(libs.get(lib), name, None) is not None for lib in c.libs)]
        if callsite and not where:
            continue
        out.append({"name": name, "spec": spec or None, "callsites": where})
    return sorted(out, key=lambda x: x["name"])


def _answered_from_code(callsite=None):
    """Per site, the subjects `add_rule` refuses because a row on them would be stored and never
    read, and where a firm's own version of that decision goes."""
    wanted = ([_callsites.BY_NAME[callsite]] if callsite in _callsites.BY_NAME
              else _callsites.CALLSITES)
    return {c.name: {"subjects": list(c.canonical), "instead": c.instead}
            for c in wanted if c.canonical}


def handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        event = json.loads(body)
    elif isinstance(body, dict):
        event = body

    if event.get("catalog"):
        site = event.get("callsite")
        cat = _catalog(site)
        if cat is None:
            return {"statusCode": 400, "body": json.dumps({
                "error": f"unknown callsite: {site}",
                "callsites": [{"name": c.name, "key": f"{c.kind}#…", "is": c.describes}
                              for c in _callsites.CALLSITES]})}
        return {"statusCode": 200,
                "body": json.dumps({"catalog": cat,
                                    "answered_from_code": _answered_from_code(site)},
                                   default=_json_default)}

    pk = event.get("contact_id") or H.GENERAL
    rule = event.get("rule")
    rows = H.query_pk(pk)
    if rule:
        rows = [r for r in rows if r.get("rule") == rule or r.get("sk") == rule]

    out = {"rows": rows}
    if rule:
        spec = _spec(rule)
        if spec is not None:
            out["spec"] = spec
    return {"statusCode": 200, "body": json.dumps(out, default=_json_default)}
