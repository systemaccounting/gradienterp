import json

from _helpers import (
    notes_table,
    latest_per_note, ok,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    filter_expression = body.get("filter_expression")
    expression_attribute_names = body.get("expression_attribute_names")
    expression_attribute_values = body.get("expression_attribute_values")
    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    kwargs = {"Limit": limit * 4}
    if filter_expression:
        kwargs["FilterExpression"] = filter_expression
    if expression_attribute_names:
        kwargs["ExpressionAttributeNames"] = expression_attribute_names
    if expression_attribute_values:
        kwargs["ExpressionAttributeValues"] = expression_attribute_values
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = notes_table().scan(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")

    deduped = latest_per_note(items)
    deduped.sort(key=lambda r: r.get("version_ts", ""), reverse=True)
    deduped = deduped[:limit]

    return ok({"notes": deduped, "next_key": next_key})
