import json

import template
from _helpers import (
    notes_table,
    validate_fields, new_version_ts,
    ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    note_id = body.get("note_id")
    updates = body.get("updates", {})
    if not note_id:
        return err("note_id is required")
    if not updates:
        return err("updates dict is required and must be non-empty")

    # note_id / version_ts / created_at are not user-settable through update
    for k in ("note_id", "version_ts", "created_at"):
        updates.pop(k, None)

    items = notes_table().query(
        KeyConditionExpression="note_id = :n",
        ExpressionAttributeValues={":n": note_id},
        ScanIndexForward=False,
        Limit=1,
    ).get("Items", [])
    latest = items[0] if items else None

    if not latest:
        return err(f"note_id '{note_id}' not found", status=404)

    merged = {**latest, **updates, "version_ts": new_version_ts()}

    # checked on the MERGE, not on `updates`: re-wording the content without re-stating the values
    # (or the reverse) is exactly how a template and its values drift apart, and a version whose
    # `$2` has nothing behind it publishes a note nobody can read.
    bad = template.check(merged.get("content", ""), merged.get("private_values") or [])
    if bad:
        return err(bad)

    errors = validate_fields(merged)
    if errors:
        return err("validation failed", validation_errors=errors)

    notes_table().put_item(Item=merged)

    return ok({"note": merged})
