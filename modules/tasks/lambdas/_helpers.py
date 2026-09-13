"""tasks — shared helpers.

The table is (task_id, sk): a task is a partition. sk="HEADER" carries current state (every
GSI + hot read lives there; mutated in place); sk="<ms>#<id>" rows are the append-only
CHANGELOG (which field changed, from -> to) written beside every header update — history as
rows, because a DDB item keeps no past. The one lifecycle transition is the delivery RATCHET:
a conditional set-if-absent of `delivery` that also drops `open_flag` (sparse GSI).
`parents` (list of task_ids) carries all structure — subtasks, DAGs, incident->class.
"""

import json
import os
import time
import uuid
from decimal import Decimal

from aws import table as _table

HEADER = "HEADER"

# Resolved lazily: the env var is read at call time, not import time, so a harness can point at a
# scratch table between cases. In Lambda these hit real DynamoDB; locally the SAME calls hit the
# local endpoint — one code path, so ConditionExpression, Decimal coercion and the sparse GSI
# behave identically in both.
def tasks_table():
    return _table(os.environ["TASKS_TABLE"])


def registry_table():
    return _table(os.environ["SCHEMA_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ─── per-customer registry (loaded once at cold start) ───

_KNOWN_FIELDS = None


def _load_registry():
    global _KNOWN_FIELDS
    if _KNOWN_FIELDS is not None:
        return
    fields = set()
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "task_fields"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            fields.add(item["name"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _KNOWN_FIELDS = fields


def validate_fields(item: dict) -> list[str]:
    """Reject unknown field names against the customer's task_fields registry.

    This now runs LOCALLY as well. It used to pass through when not in Lambda, which is why a
    field absent from the registry could pass every local test and 400 in production."""
    _load_registry()
    return [f"unknown field '{k}'" for k in item if k not in _KNOWN_FIELDS]


# ─── id + time ───

def now_ms() -> int:
    return int(time.time() * 1000)


def new_task_id() -> str:
    return uuid.uuid4().hex


def to_ms(v):
    """'now' / ms-epoch / numeric string -> ms int; None/'' -> None. ISO not accepted here —
    the moving dates (quote/delivery) are machine-set instants, not civil dates."""
    if v in (None, ""):
        return None
    if v == "now":
        return now_ms()
    return int(v)


def changelog_sk(at_ms: int) -> str:
    return f"{at_ms:013d}#{uuid.uuid4().hex[:8]}"


def diff_changes(current: dict, updates: dict) -> dict:
    """{field: {from, to}} for keys whose value actually changes (bookkeeping keys skipped)."""
    changes = {}
    for k, v in updates.items():
        if k == "updated_at":
            continue
        old = current.get(k)
        if old != v:
            changes[k] = {"from": old, "to": v}
    return changes


# ─── header + changelog access (lambda | local jsonl keyed (task_id, sk)) ───

def get_header(task_id: str):
    return tasks_table().get_item(Key={"task_id": task_id, "sk": HEADER}).get("Item")


def put_row(item: dict):
    tasks_table().put_item(Item=item)


def append_changelog(task_id: str, changes: dict, at_ms: int):
    if changes:
        put_row({"task_id": task_id, "sk": changelog_sk(at_ms), "at": at_ms, "changes": changes})


def deliver_header(task_id: str, delivered_ms: int) -> bool:
    """The ratchet: set delivery ONCE (first close wins), drop open_flag. False if already
    delivered (or missing — callers check existence first)."""
    t = tasks_table()
    try:
        t.update_item(
            Key={"task_id": task_id, "sk": HEADER},
            UpdateExpression="SET delivery = :d, updated_at = :u REMOVE open_flag",
            ConditionExpression="attribute_exists(task_id) AND attribute_not_exists(delivery)",
            ExpressionAttributeValues={":d": delivered_ms, ":u": delivered_ms},
        )
        return True
    except t.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def open_children(task_id: str) -> list:
    """Open tasks naming task_id a parent (the close guard reads this — an open child blocks
    the parent's delivery). The sparse open set keeps it cheap."""
    resp = tasks_table().query(
        IndexName="open-tasks-index",
        KeyConditionExpression="open_flag = :o",
        FilterExpression="contains(parents, :p)",
        ExpressionAttributeValues={":o": "1", ":p": task_id},
    )
    return resp.get("Items", [])


def task_rows(task_id: str) -> list:
    """Every row in the task's partition, header first then changelog chronological."""
    rows, kwargs = [], {
        "KeyConditionExpression": "task_id = :t",
        "ExpressionAttributeValues": {":t": task_id},
    }
    while True:
        resp = tasks_table().query(**kwargs)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(rows, key=lambda r: "" if r.get("sk") == HEADER else r["sk"])


# ─── tags — what a firm calls a task, as opposed to whether it is done ───
#
# A SET: many per task, unordered, independent. Nothing here refuses a tag because another is
# present. Exclusivity would lead to ordering and ordering to legal transitions, which is the
# open/closed ratchet again with none of its guarantees — and a task already has that one ordered
# thing. A firm that wants `investigating -> approved` writes `NEXT_VALUES#task_tag#` rows and its
# own script consults them; that is the firm sequencing, not the mechanism.
#
# Rows in the task's own partition, beside the header and the changelog, so reading a task's tags is
# the query that already fetches it. "Every task tagged X" needs the GSI, because a set cannot be a
# key.
#
# The tag rows are CURRENT STATE and the changelog is the journey. Adding writes a row and appends a
# changelog entry; deleting removes the row and appends another. Without that split you get tags
# buried in prose that nothing can query, or a history that silently loses every removal.

TAG_REGISTRY = "task_tags"
TAG_BUCKET = "common"
TAG_PREFIX = "tag#"


def tag_declared(tag: str) -> bool:
    """Whether the firm has declared `tag`. The registry is where a vocabulary ACCUMULATES — the
    tags that recur across firms can be absorbed canonically and offered to the next one — so a tag
    invented per-task is both outside that and a typo away from being invisible to the query looking
    for it.

    Its own registry rather than invoicing's: they are vocabularies about different objects, and a
    shared one would mean a tag recurring on invoices starts being offered on tasks, where it means
    nothing."""
    got = _table(os.environ["SCHEMA_TABLE"]).get_item(
        Key={"registry": TAG_REGISTRY, "bucket_name": f"{TAG_BUCKET}#{tag}"}).get("Item")
    return bool(got)


def tag_sk(tag: str) -> str:
    return f"{TAG_PREFIX}{tag}"


def read_tags(task_id: str) -> list[dict]:
    """Every tag on a task, with who applied it and when."""
    rows = [r for r in task_rows(task_id) if str(r.get("sk", "")).startswith(TAG_PREFIX)]
    return sorted(({"tag": r["sk"][len(TAG_PREFIX):], "applied_at": r.get("applied_at"),
                    "applied_by": r.get("applied_by", "")} for r in rows),
                  key=lambda t: t["tag"])


def put_tag(task_id: str, tag: str, applied_by: str = "", at_ms: int | None = None):
    """Idempotent — re-applying a tag it already carries is a no-op write, not a second row."""
    at = at_ms if at_ms is not None else now_ms()
    put_row({
        "task_id": task_id,
        "sk": tag_sk(tag),
        "gsi_tag": f"{TAG_PREFIX}{tag}",
        "gsi_tag_sk": f"{at:013d}#{task_id}",
        "applied_at": at,
        **({"applied_by": applied_by} if applied_by else {}),
    })


def drop_tag(task_id: str, tag: str) -> bool:
    """False when the task did not carry it — so a caller can say so rather than reporting a
    removal that removed nothing."""
    resp = tasks_table().delete_item(
        Key={"task_id": task_id, "sk": tag_sk(tag)}, ReturnValues="ALL_OLD")
    return bool(resp.get("Attributes"))


def tasks_with_tag(tag: str, limit: int = 100) -> list[str]:
    """Task ids carrying `tag`, newest first."""
    resp = tasks_table().query(
        IndexName="tag-index",
        KeyConditionExpression="gsi_tag = :t",
        ExpressionAttributeValues={":t": f"{TAG_PREFIX}{tag}"},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [r["task_id"] for r in resp.get("Items", [])]


def check_parent_cycle(task_id: str, parents: list, depth: int = 20):
    """Walk up the parent chains; a path back to task_id (or absurd depth) is refused."""
    frontier = list(parents or [])
    seen = set()
    while frontier and depth > 0:
        depth -= 1
        nxt = []
        for p in frontier:
            if p == task_id:
                return f"cycle: \'{p}\' is (or leads back to) this task"
            if p in seen:
                continue
            seen.add(p)
            hdr = get_header(p)
            if hdr:
                nxt += list(hdr.get("parents") or [])
        frontier = nxt
    if frontier:
        return "parent chain too deep (20) — refusing as a probable cycle"
    return None


# ─── responses ───

def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
