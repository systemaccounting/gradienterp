import json

from _helpers import (
    notes_table,
    ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    note_id = body.get("note_id")
    if not note_id:
        return err("note_id is required")

    items = notes_table().query(
        KeyConditionExpression="note_id = :n",
        ExpressionAttributeValues={":n": note_id},
        ScanIndexForward=False,
        Limit=1,
    ).get("Items", [])
    item = items[0] if items else None

    if not item:
        return err(f"note_id '{note_id}' not found", status=404)
    return ok({"note": item})
