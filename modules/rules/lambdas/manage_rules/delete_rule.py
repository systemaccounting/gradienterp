"""op=delete — turn an automation off.

An instance is active while its row exists; this deletes the row. On a key that ships a canonical
default (`ITEM_CREATED#*`, `STOCK_ADJUSTED#*`, the `ITEM_TRANSITION#…` set), deleting the firm's row RESTORES the
built-in behavior — the response says so, so the agent relays it correctly ("count variance will post to
COGS again"). No off-flag, no rate-of-zero: gone is off.
"""

import json

from aws import json_default as _json_default

import instances
import stock_rules

_CANONICAL_KEYS = {r["pk"] for r in stock_rules.CANONICAL_ADJUSTMENT}

try:
    import catalog_rules
    _CANONICAL_KEYS |= {r["pk"] for r in catalog_rules.CANONICAL}
except ImportError:
    pass

try:
    import transition_rules
    _CANONICAL_KEYS |= {r["pk"] for r in transition_rules.CANONICAL}
except ImportError:
    pass


def handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        event = json.loads(body)
    elif isinstance(body, dict):
        event = body

    matches = event.get("matches")
    name = event.get("name")
    n = event.get("n")
    if not matches or not name:
        return _err("matches and name are required — the row's key, as manage_rules op=list shows it "
                    "(e.g. matches='STOCK_SOLD#doppio', name='backflush')")

    # `n` is part of the row's sort key, so deleting needs it — but a caller who knows the name
    # should not have to know the order too. Absent, it is looked up. It defaulted to 300 before,
    # which silently missed every row attached at any other n and reported it as "not found".
    if n is None:
        here = [r for r in instances.for_key(matches) if r.get("name") == name]
        if not here:
            return _err(f"no rule instance named {name!r} at {matches} — manage_rules op=list lists what's "
                        "attached", status=404)
        if len(here) > 1:
            ns = sorted(int(r["n"]) for r in here)
            return _err(f"{len(here)} instances at {matches} are named {name!r}, at n={ns} — "
                        "pass the n of the one to remove")
        n = here[0]["n"]

    gone = instances.delete(matches, n, name)
    if gone is None:
        return _err(f"no rule instance at {matches} | {instances.sort_key(n, name)} — "
                    "manage_rules op=list lists what's attached (check the n)", status=404)

    out = {"deleted": {"matches": matches, "name": name, "n": int(n),
                       "rule": gone.get("rule"), "param": gone.get("param") or {}}}
    if matches in _CANONICAL_KEYS:
        out["note"] = "this key carries a built-in canonical default, which now applies again"
    return {"statusCode": 200, "body": json.dumps(out, default=_json_default)}


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}
