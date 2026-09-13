"""oob — the one place a public reader learns what it may publish.

Every `oob_*` reader used to hand-pick its own projection, which meant every new reader redrew the
private/public line by hand and a field added to a table later was exposed or not depending on
whether someone remembered to think about it. This makes the line a property of the DATA: the
per-customer registry says what each field IS, and a reader is a filter over that.

Four classes (AGENTS.md § the oob annotation):

  economic     money and quantity — the platform's primary product. Publishes.
  operational  timings, states, locations, keys, metadata. Publishes.
  subject      names or references a PERSON. Handed back separately, never inlined: the value is
               whatever the gerp's CONTACT references — a `gerp_profile_id` when the person is
               public, nothing when they are private. A private person is ABSENT from a published
               ROW; aggregates OVER private people still publish (a count discloses nothing about
               a member), and the gerp's own CRM keeps serving them by `contact_id` internally.
  secret       a credential or a legal identity document. Never served, to anyone.

**Absent means secret.** A field nobody classified is closed, so shipping a new column can never
publish it by accident — the failure mode is a missing field someone reports, not a leak nobody
notices. `extend_schema` requires a class on every new field precisely so "absent" stays rare.

This filters WHAT MAY BE SAID about a row. Whether the business publishes at all is a separate
and earlier question — `GERP#openly_operated`, checked at every exit by the reader itself.
"""

import os

PUBLISHABLE = {"economic", "operational"}
SUBJECT = "subject"
SECRET = "secret"

_IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
_CACHE: dict[str, dict[str, str]] = {}


def _classes(registry: str) -> dict[str, str]:
    """field name -> class, for one registry. Cached per container: the registry is a few hundred
    rows that change at schema-extension time, not per request, and a stale entry can only WITHHOLD
    a newly added field (absent -> secret) rather than publish one."""
    if registry in _CACHE:
        return _CACHE[registry]

    out: dict[str, str] = {}
    if _IS_LAMBDA:
        import boto3
        from boto3.dynamodb.conditions import Key
        table = boto3.resource("dynamodb").Table(os.environ["SCHEMA_TABLE"])
        kwargs = {"KeyConditionExpression": Key("registry").eq(registry)}
        while True:
            resp = table.query(**kwargs)
            for row in resp.get("Items", []):
                schema = row.get("schema")
                cls = row.get("class")
                if not cls and isinstance(schema, dict):
                    cls = schema.get("class")
                out[row["name"]] = cls or SECRET
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    else:
        import json
        path = os.path.join(os.environ.get("LOCAL_CANONICAL_DIR", "modules/schemas/data"),
                            f"{registry}.json")
        if os.path.exists(path):
            with open(path) as f:
                for _bucket, fields in json.load(f).items():
                    for name, spec in fields.items():
                        out[name] = (spec.get("class") if isinstance(spec, dict) else None) or SECRET

    _CACHE[registry] = out
    return out


def project(row: dict, registry: str) -> dict:
    """A row reduced to what may be published, plus the subject references a caller must resolve.

    Returns `{"fields": {...}, "subjects": {field: value}}`. Subject VALUES are handed back
    unresolved on purpose — this module knows what a person-reference IS, and only the profile
    store turns a public identifier into a name. Keeping those apart is what makes absence the
    default: a reader that never runs the second lookup publishes a row with no person in it.
    """
    classes = _classes(registry)
    fields, subjects = {}, {}
    for key, value in row.items():
        cls = classes.get(key, SECRET)
        if cls in PUBLISHABLE:
            fields[key] = value
        elif cls == SUBJECT:
            subjects[key] = value
        # secret (and unclassified) falls through — never copied anywhere
    return {"fields": fields, "subjects": subjects}


def publishable(row: dict, registry: str) -> dict:
    """Just the publishable fields — for a reader with no identity story yet."""
    return project(row, registry)["fields"]
