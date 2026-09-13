import json

from _helpers import (
    local_get_item, local_all_items, ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    item_id = body.get("item_id")

    if item_id:
        item = local_get_item(item_id)
        if not item:
            return err(f"item not found: {item_id}", status=404)
        return ok({"items": [item]})

    items = local_all_items()
    return ok({"items": items})
