"""manage_invoice — the plumbing of an invoice under one tool.

    op: create        draft an invoice from typed lines
        from_template draft one from the owner's template (a rule instance) and the catalog
        complete_line fill a hole in a DRAFT line — price, account, catalog item
        transition    move ONE item to a new state (money posts by the rules attached to it)
        get           one invoice by id (its items folded to current state), or a filtered list
        tag / untag   the firm's own labels on an invoice; tags lists them; find_by_tag finds by one

`issue_invoice` and `record_invoice_paid` stay their own tools: they post to the ledger.

Each op's body is the tool it absorbed, moved in unchanged as a sibling file and loaded by path,
so the handler sees the same event it always took — minus `op`. The POS read route
(`GET /invoices`, `GET /invoices/{invoice_id}`) reaches this lambda with no body: a request that
carries path or query parameters and no op is a `get`.
"""

import importlib.util
import json

from aws import refuse_non_owner
from pathlib import Path

from _helpers import err

_HERE = Path(__file__).resolve().parent

OPS = {
    "create": "create_invoice",
    "from_template": "create_from_template",
    "complete_line": "complete_line",
    "transition": "transition_item",
    "get": "get_invoices",
    "tag": "invoice_tags",
    "untag": "invoice_tags",
    "tags": "invoice_tags",
    "find_by_tag": "invoice_tags",
}
# the tags body keeps its own verb; this tool's op names are flat so there is one discriminator
TAG_VERB = {"tag": "apply", "untag": "remove", "tags": "list", "find_by_tag": "find"}

_loaded = {}


def _module(name):
    if name not in _loaded:
        spec = importlib.util.spec_from_file_location(f"manage_invoice_{name}", _HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _loaded[name] = mod
    return _loaded[name]


def handler(event, context):
    refused = refuse_non_owner(event)
    if refused:
        return refused
    string_body = isinstance(event.get("body"), str)
    body = json.loads(event["body"]) if string_body else dict(event)
    op = (body.pop("op", "") or "").strip()
    if not op and (event.get("pathParameters") or event.get("queryStringParameters")
                   or (event.get("rawPath") or "").startswith("/invoices")):
        op = "get"
    if op not in OPS:
        return err(f"op is required: one of {', '.join(OPS)}")
    if op in TAG_VERB:
        body["op"] = TAG_VERB[op]
    sub = {**event, "body": json.dumps(body)} if string_body else body
    return _module(OPS[op]).handler(sub, context)
