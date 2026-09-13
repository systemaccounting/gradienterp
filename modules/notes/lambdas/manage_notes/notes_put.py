"""notes_put — write a note version.

`content` is a TEMPLATE and `private_values` carries what its `$n` spans stand for, the same split
`escalate` uses. A note is where someone writes "call Dana about the late invoice", so classing the
whole field secret would be safe and would publish nothing; split, the SHAPE of the annotation
publishes (what it is about, how often, against which invoice) and the specifics do not.
"""

import json

import template
from _helpers import (
    notes_table,
    validate_fields, new_version_ts, new_note_id, now_ms,
    ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    if not body.get("content"):
        return err("content is required")

    private_values = body.get("private_values") or []
    bad = template.check(body["content"], private_values)
    if bad:
        return err(bad)

    note_id = body.get("note_id") or new_note_id()
    version_ts = new_version_ts()

    item = {
        "note_id": note_id,
        "version_ts": version_ts,
        "content": body["content"],
        "created_at": now_ms(),
        **({"private_values": private_values} if private_values else {}),
    }
    for fk in ("contact_id", "journal_entry_id", "purchase_order_id", "invoice_id"):
        if body.get(fk):
            item[fk] = body[fk]

    errors = validate_fields(item)
    if errors:
        return err("validation failed", validation_errors=errors)

    notes_table().put_item(Item=item)

    return ok({"note": item})
