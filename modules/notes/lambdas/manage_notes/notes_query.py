import json

from _helpers import (
    notes_table,
    latest_per_note, ok, err,
)


FK_TO_INDEX = {
    "contact_id":        "contact-index",
    "journal_entry_id":  "journal-entry-index",
    "purchase_order_id": "purchase-order-index",
    "invoice_id":        "invoice-index",
}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    fk_field, fk_value = None, None
    for fk in FK_TO_INDEX:
        if body.get(fk):
            if fk_field is not None:
                return err("specify only one FK at a time", second_fk=fk)
            fk_field = fk
            fk_value = body[fk]
    if not fk_field:
        return err(f"one of {sorted(FK_TO_INDEX)} is required")

    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    kwargs = {
        "IndexName": FK_TO_INDEX[fk_field],
        "KeyConditionExpression": f"{fk_field} = :v",
        "ExpressionAttributeValues": {":v": fk_value},
        "ScanIndexForward": False,
        "Limit": limit * 4,  # over-fetch to allow dedup; cap below
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = notes_table().query(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")

    deduped = latest_per_note(items)
    deduped.sort(key=lambda r: r.get("version_ts", ""), reverse=True)
    deduped = deduped[:limit]

    return ok({"notes": deduped, "next_key": next_key})
