"""manage_contacts — the contacts table behind one tool.

`op` picks the operation and is STRIPPED before any row logic runs, so the registry
validation on put/update sees exactly the fields a contact may carry.
"""
import json

from _helpers import (
    contacts_table,
    validate_contact,
    ROLE_FLAGS, now_ms, to_ddb, ok, err,
)

VALID_ROLES = set(ROLE_FLAGS.values())


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    body = dict(body)
    op = body.pop("op", None)
    ops = {"get": _get, "put": _put, "update": _update, "query": _query, "scan": _scan}
    if op not in ops:
        return err(f"op is required: one of {sorted(ops)}")
    return ops[op](body)


def _get(body):
    contact_id = body.get("contact_id")
    if not contact_id:
        return err("contact_id is required")

    item = contacts_table().get_item(Key={"contact_id": contact_id}).get("Item")

    if not item:
        return err(f"contact_id '{contact_id}' not found", status=404)
    return ok({"contact": item})


def _put(body):
    contact_id = body.get("contact_id")
    if not contact_id:
        return err("contact_id is required")

    now = now_ms()
    item = {**body}
    item.setdefault("created_at", now)
    item["updated_at"] = now

    errors = validate_contact(item)
    if errors:
        return err("validation failed", validation_errors=errors)

    contacts_table().put_item(Item=to_ddb(item))

    return ok({"contact": item})


def _update(body):
    contact_id = body.get("contact_id")
    updates = body.get("updates", {})
    if not contact_id:
        return err("contact_id is required")
    if not updates:
        return err("updates dict is required and must be non-empty")

    # contact_id and created_at are not user-settable via update
    updates.pop("contact_id", None)
    updates.pop("created_at", None)

    existing = contacts_table().get_item(Key={"contact_id": contact_id}).get("Item")

    if not existing:
        return err(f"contact_id '{contact_id}' not found", status=404)

    merged = {**existing, **updates, "updated_at": now_ms()}

    errors = validate_contact(merged)
    if errors:
        return err("validation failed", validation_errors=errors)

    set_clauses = []
    names = {}
    values = {}
    for i, (k, v) in enumerate({**updates, "updated_at": merged["updated_at"]}.items()):
        n = f"#f{i}"
        p = f":v{i}"
        set_clauses.append(f"{n} = {p}")
        names[n] = k
        values[p] = to_ddb(v)
    contacts_table().update_item(
        Key={"contact_id": contact_id},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(contact_id)",
    )

    return ok({"contact": merged})


def _query(body):
    role = body.get("role")
    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    # a public profile id → the person. Rows stamped by an authenticated write carry a Cognito
    # subject (`authed_by` / `created_by` on an invoice, for instance), not a name — and a person's
    # profile is keyed by that same subject, so one lookup turns it back into a contact and a
    # statement can say "Ava Reyes" instead of a uuid. A Query on the sparse account-index, never a
    # scan — the same index the chat lambda uses to resolve a caller's role. `account_id` is still
    # accepted as an input name: callers pass a subject, and for a person that IS the profile id.
    profile_id = body.get("gerp_profile_id") or body.get("account_id")
    if profile_id:
        from boto3.dynamodb.conditions import Key
        items = contacts_table().query(
            IndexName="account-index",
            KeyConditionExpression=Key("gerp_profile_id").eq(str(profile_id)),
            Limit=1,
        ).get("Items", [])
        return ok({"contacts": items, "next_key": None})

    if role not in VALID_ROLES:
        return err(f"role must be one of {sorted(VALID_ROLES)} "
                   "(or pass gerp_profile_id / account_id to look one up)")
    flag = f"is_{role}"

    kwargs = {
        "FilterExpression": "#f = :t",
        "ExpressionAttributeNames": {"#f": flag},
        "ExpressionAttributeValues": {":t": True},
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = contacts_table().scan(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")

    return ok({"contacts": items, "next_key": next_key})


def _scan(body):
    filter_expression = body.get("filter_expression")
    expression_attribute_names = body.get("expression_attribute_names")
    expression_attribute_values = body.get("expression_attribute_values")
    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    kwargs = {"Limit": limit}
    if filter_expression:
        kwargs["FilterExpression"] = filter_expression
    if expression_attribute_names:
        kwargs["ExpressionAttributeNames"] = expression_attribute_names
    if expression_attribute_values:
        kwargs["ExpressionAttributeValues"] = expression_attribute_values
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = contacts_table().scan(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")
    return ok({"contacts": items, "next_key": next_key})
