"""manage_locations — the agent's tool over the LOCATION# settings rows.

Locations are config: the ordinal is the identifier (item ids lead with it, journal dimensions
carry it, statements slice by it); label/city/name are description the owner can change any time.
#1 is seeded at provisioning as "main" and IS the default forever — this tool never sets a
default, because there is nothing to set.

ops: list | add (allocates the next ordinal) | update (explicit ordinal — rename description or
set a provider id like square_location_id at connect time).
"""

import json

from _locations import list_locations, write_location


def _ok(body, status=200):
    return {"statusCode": status, "body": json.dumps(body)}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    op = body.get("op", "list")

    if op == "list":
        return _ok({"locations": list_locations()})

    if op in ("add", "update"):
        spec = {k: body.get(k) for k in
                ("ordinal", "city", "name", "label",
                 "square_location_id", "stripe_account_suffix", "paypal_merchant_id")
                if body.get(k) is not None}
        if op == "add":
            spec.pop("ordinal", None)  # add always allocates the next one
        elif not str(spec.get("ordinal") or "").strip():
            return _ok({"error": "update needs the ordinal (list shows them)"}, 400)
        try:
            row = write_location(spec)
        except ValueError as ve:
            return _ok({"error": str(ve)}, 400)
        return _ok({"status": op, "location": row})

    return _ok({"error": "op must be list | add | update"}, 400)
