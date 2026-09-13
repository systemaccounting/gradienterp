"""op=list — the ATTACHED automations: what rule instances this firm has running, and on what.

The instances table is the firm's automation config (the attachment IS the dispatch), and this is
its read: every row, or the rows on one key, each carrying its rule's param spec so the agent can
see and explain an automation in one call. Distinct from `rule_params` op=get, which reads the CATALOG
(what's offerable) and the PARAMS store (platform / employer-wide values) — this reads what is
actually attached.

Canonical defaults are part of the answer: a moment that ships a built-in row (the catalog default
on `ITEM_CREATED#*`, the count-variance valuation on `STOCK_ADJUSTED#*`, the money rules on `ITEM_TRANSITION#…`) is
in effect wherever no firm row overrides it, so those rows are returned too, marked
`"canonical": true` — the agent can say "count variance currently posts to COGS (built-in default)" without
guessing.
"""

import json

from aws import json_default as _json_default

import instances
import rules as engine

import automation_rules  # the script-facing library: retry_order, retry_decision
import dispatch_rules  # run_automation — a callsite handing off to firm code
import collection_rules
import general_rules
import payroll_rules
import stock_rules
import agreement_rules   # what a firm answers without a turn

_RULE_LIBS = [general_rules, payroll_rules, stock_rules, collection_rules, automation_rules, dispatch_rules, agreement_rules]
_CANONICAL = list(stock_rules.CANONICAL_ADJUSTMENT)

try:
    import treasury_rules
    _RULE_LIBS.append(treasury_rules)
except ImportError:
    pass

try:
    import catalog_rules
    _RULE_LIBS.append(catalog_rules)
    _CANONICAL += list(catalog_rules.CANONICAL)
except ImportError:
    pass

try:
    import transition_rules
    _RULE_LIBS.append(transition_rules)
    _CANONICAL += list(transition_rules.CANONICAL)
except ImportError:
    pass


def _spec(rule_name):
    try:
        return engine.spec(engine._resolve(rule_name, _RULE_LIBS)) or None
    except ValueError:
        return None


def _present(row, canonical=False):
    return {
        "matches": row.get("pk"),
        "name": row.get("name"),
        "n": row.get("n"),
        "rule": row.get("rule"),
        "param": row.get("param") or {},
        "canonical": canonical,
        "spec": _spec(row.get("rule")),
    }


def handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        event = json.loads(body)
    elif isinstance(body, dict):
        event = body

    matches = event.get("matches")

    if matches:
        # one key — asking about a specific subject includes that callsite's catch-all, since a
        # `<CALLSITE>#*` row runs on it too. Asking about the catch-all itself does not.
        kind, _, subject = matches.partition("#")
        if subject and subject != instances.ANY:
            written = instances.at(kind, subject)
        else:
            written = instances.for_key(matches)
        canonical = [] if written else [r for r in _CANONICAL if r["pk"] == matches]
    else:
        written = instances.all_rows()
        covered = {r.get("pk") for r in written}
        canonical = [r for r in _CANONICAL if r["pk"] not in covered]

    rows = [_present(r) for r in written] + [_present(r, canonical=True) for r in canonical]
    return {"statusCode": 200, "body": json.dumps({"rows": rows}, default=_json_default)}
