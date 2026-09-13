"""manage_tasks — the firm's task list, one tool; `op` picks the verb.

get / put / update / query / scan, the five verbs that were their own lambdas until the
wave-1 consolidation (tmp doc 023). Each verb's code is unchanged; the handler routes.

update's two shapes:
  {"task_id", "updates": {...}}    — header fields change; each actual change lands in ONE
                                     appended changelog row (the task's history as rows).
  {"task_id", "deliver": "now"|ms} — the ratchet: delivery set ONCE (conditional write,
                                     first close wins) + open_flag dropped. May combine.
"""

import json

from _helpers import (
    HEADER, TAG_PREFIX, validate_fields, get_header, task_rows, put_row, append_changelog,
    deliver_header, check_parent_cycle, diff_changes, new_task_id, now_ms, to_ms, ok, err,
    put_tag, drop_tag, read_tags, tag_declared, tasks_table, tasks_with_tag, open_children,
)

_PROTECTED = {"task_id", "sk", "created_at", "open_flag", "delivery", "updated_at"}
_RETIRED = {"severity", "priority", "resolved_at"}

FK_TO_INDEX = {
    "contact_id":        "contact-index",
    "journal_entry_id":  "journal-entry-index",
    "purchase_order_id": "purchase-order-index",
    "invoice_id":        "invoice-index",
    "subject_key":       "subject-index",
}


def _get(body):
    task_id = body.get("task_id")
    if not task_id:
        return err("task_id is required")

    if body.get("history"):
        rows = task_rows(task_id)
        header = next((r for r in rows if r.get("sk") == HEADER), None)
        if not header:
            return err(f"task_id '{task_id}' not found", status=404)
        # tag rows share the partition with the changelog; they are the task's CURRENT labels, not
        # events in its history, so they come back as `tags` and the history stays the journey.
        history = [r for r in rows
                   if r.get("sk") != HEADER and not str(r.get("sk", "")).startswith(TAG_PREFIX)]
        return ok({"task": header, "history": history, "tags": read_tags(task_id)})

    item = get_header(task_id)
    if not item:
        return err(f"task_id '{task_id}' not found", status=404)
    tags = read_tags(task_id)
    return ok({"task": item, **({"tags": [t["tag"] for t in tags]} if tags else {})})


def _put(body):
    if not body.get("content"):
        return err("content is required")

    task_id = body.get("task_id") or new_task_id()
    n = now_ms()

    item = {
        "task_id": task_id,
        "sk": HEADER,
        "content": body["content"],
        "created_at": n,
        "updated_at": n,
    }
    # `private_values` rides here rather than in `content`: when content is a TEMPLATE (an
    # escalation), what its $1/$2 stand for is class-secret and must live in its own field, so
    # filing a public issue from `content` is safe by construction (modules/schemas/template.py).
    for fk in ("contact_id", "journal_entry_id", "purchase_order_id", "invoice_id",
               "subject_key", "category", "private_values", "assigned_to"):
        if body.get(fk):
            item[fk] = body[fk]
    if body.get("due_date"):
        item["due_date"] = body["due_date"]

    parents = body.get("parents")
    if parents is not None:
        if not isinstance(parents, list) or not all(isinstance(p, str) and p for p in parents):
            return err("parents must be a list of task_ids")
        cycle = check_parent_cycle(task_id, parents)
        if cycle:
            return err(cycle)
        if parents:
            item["parents"] = parents

    try:
        quote = to_ms(body.get("quote"))
        delivery = to_ms(body.get("delivery"))
    except (TypeError, ValueError):
        return err("quote/delivery take 'now' or ms-epoch")
    if quote is not None:
        item["quote"] = quote
    if delivery is not None:
        item["delivery"] = delivery   # created already-shipped (a record, not live work)
    else:
        item["open_flag"] = "1"

    errors = validate_fields(item)
    if errors:
        return err("validation failed", validation_errors=errors)

    put_row(item)
    return ok({"task": item})


def _update(body):
    task_id = body.get("task_id")
    updates = dict(body.get("updates") or {})
    deliver = body.get("deliver")
    if not task_id:
        return err("task_id is required")
    add_tag = (body.get("add_tag") or "").strip()
    delete_tag = (body.get("delete_tag") or "").strip()
    applied_by = (body.get("applied_by") or "").strip()
    if not updates and deliver in (None, "") and not add_tag and not delete_tag:
        return err("updates dict, add_tag, delete_tag and/or deliver is required")
    if add_tag and delete_tag:
        return err("one tag at a time — add_tag or delete_tag, not both")

    retired = _RETIRED & set(updates)
    if retired:
        return err(f"retired fields: {sorted(retired)} — due_date is the ceiling, "
                   f"quote the forecast, deliver closes")
    for k in list(updates):
        if k in _PROTECTED:
            updates.pop(k)

    current = get_header(task_id)
    if not current:
        return err(f"task_id '{task_id}' not found", status=404)

    n = now_ms()
    out = dict(current)

    if updates:
        if "parents" in updates:
            parents = updates["parents"]
            if not isinstance(parents, list) or not all(isinstance(p, str) and p for p in parents):
                return err("parents must be a list of task_ids")
            cycle = check_parent_cycle(task_id, parents)
            if cycle:
                return err(cycle)
        if "quote" in updates:
            try:
                updates["quote"] = to_ms(updates["quote"])
            except (TypeError, ValueError):
                return err("quote takes 'now' or ms-epoch")

        changes = diff_changes(current, updates)
        merged = {**current, **updates, "updated_at": n}
        merged = {k: v for k, v in merged.items() if v not in (None, "", [])}
        errors = validate_fields(merged)
        if errors:
            return err("validation failed", validation_errors=errors)
        if changes:
            put_row(merged)
            append_changelog(task_id, changes, n)
        out = merged

    # Tags before delivery, so a close that states its reason can do both in one call and the tag is
    # already there when the delivery ratchet and anything watching the stream see it.
    if add_tag:
        if not tag_declared(add_tag):
            return err(f"tag '{add_tag}' is not declared — add it to the task_tags registry with "
                       f"write_schema (op: extend) first, so it means the same thing everywhere it is used")
        put_tag(task_id, add_tag, applied_by, n)
        append_changelog(task_id, {"tag": {"from": None, "to": add_tag}}, n)
    if delete_tag:
        if not drop_tag(task_id, delete_tag):
            return err(f"task does not carry '{delete_tag}'", status=404)
        append_changelog(task_id, {"tag": {"from": delete_tag, "to": None}}, n)

    if deliver not in (None, ""):
        try:
            delivered_ms = to_ms(deliver)
        except (TypeError, ValueError):
            return err("deliver takes 'now' or ms-epoch")
        kids = open_children(task_id)
        if kids:
            names = [k.get("task_id") for k in kids[:5]]
            return err(f"open children block delivery: {names} — deliver them first", status=409)
        if not deliver_header(task_id, delivered_ms):
            return err(f"task '{task_id}' is already delivered — the ratchet closes once", status=409)
        append_changelog(task_id, {"delivery": {"from": None, "to": delivered_ms}}, delivered_ms)
        out = get_header(task_id) or out

    tags = read_tags(task_id)
    return ok({"task": out, **({"tags": [t["tag"] for t in tags]} if tags else {})})


def _query(body):
    open_only = bool(body.get("open"))
    parent = body.get("parent")
    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    fk_field, fk_value = None, None
    for fk in FK_TO_INDEX:
        if body.get(fk):
            if fk_field is not None:
                return err("specify only one of contact_id / journal_entry_id / purchase_order_id / invoice_id / subject_key / open / parent at a time")
            fk_field = fk
            fk_value = body[fk]
    tag = (body.get("tag") or "").strip()
    if tag and (fk_field or open_only or parent):
        return err("tag is its own lookup — ask for it alone, then filter the answer")
    if fk_field and (open_only or parent):
        return err("specify only one of contact_id / journal_entry_id / purchase_order_id / invoice_id / subject_key / open / parent at a time")
    if not fk_field and not open_only and not parent and not tag:
        return err("one of contact_id / journal_entry_id / purchase_order_id / invoice_id / subject_key / tag / open=true / parent is required")

    if tag:
        # The index is over TAG ROWS and projects keys only, so it answers which tasks carry the
        # tag and nothing about them. The headers are a read per task — a tag is a label a firm
        # applies deliberately, so this is tens of rows, not the table.
        ids = tasks_with_tag(tag, limit)
        tasks = [h for h in (get_header(t) for t in ids) if h]
        if body.get("open") is not None or open_only:
            tasks = [t for t in tasks if t.get("open_flag")]
        return ok({"tasks": tasks, "tag": tag,
                   "tags": {t["task_id"]: [x["tag"] for x in read_tags(t["task_id"])] for t in tasks}})

    if parent:
        # children of X = the open set filtered by contains(parents, X) — the sparse GSI
        # keeps the open set small by nature, so the filter is cheap
        kwargs = {
            "IndexName": "open-tasks-index",
            "KeyConditionExpression": "open_flag = :o",
            "FilterExpression": "contains(parents, :p)",
            "ExpressionAttributeValues": {":o": "1", ":p": parent},
            "Limit": limit,
        }
    elif open_only:
        kwargs = {
            "IndexName": "open-tasks-index",
            "KeyConditionExpression": "open_flag = :o",
            "ExpressionAttributeValues": {":o": "1"},
            "Limit": limit,
        }
    else:
        kwargs = {
            "IndexName": FK_TO_INDEX[fk_field],
            "KeyConditionExpression": f"{fk_field} = :v",
            "ExpressionAttributeValues": {":v": fk_value},
            "Limit": limit,
        }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = tasks_table().query(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")
    return ok({"tasks": items, "next_key": next_key})


def _scan(body):
    filter_expression = body.get("filter_expression")
    expression_attribute_names = body.get("expression_attribute_names")
    expression_attribute_values = body.get("expression_attribute_values")
    limit = body.get("limit", 100)
    start_key = body.get("exclusive_start_key")

    # headers only — changelog rows are history, never scan results
    fe = "sk = :hdr" + (f" AND ({filter_expression})" if filter_expression else "")
    values = {":hdr": HEADER, **(expression_attribute_values or {})}
    kwargs = {"Limit": limit, "FilterExpression": fe, "ExpressionAttributeValues": values}
    if expression_attribute_names:
        kwargs["ExpressionAttributeNames"] = expression_attribute_names
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = tasks_table().scan(**kwargs)
    items = resp.get("Items", [])
    next_key = resp.get("LastEvaluatedKey")

    return ok({"tasks": items, "next_key": next_key})


_OPS = {"get": _get, "put": _put, "update": _update, "query": _query, "scan": _scan}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    body = dict(body)
    op = body.pop("op", None)
    if op not in _OPS:
        return err(f"op is required: one of {', '.join(sorted(_OPS))}")
    return _OPS[op](body)
