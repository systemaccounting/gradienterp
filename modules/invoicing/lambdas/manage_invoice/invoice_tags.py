"""manage_invoice_tags — apply and remove the labels a firm puts on its own invoices.

A tag is NOT a status. Statuses are `draft → issued → paid`, they are money positions, and modules
depend on them. A tag carries no accounting meaning, which is what makes it safe for a firm to invent
whatever it likes and hang automation off it.

    {"op": "apply",  "invoice_id": "...", "tag": "disputed"}
    {"op": "remove", "invoice_id": "...", "tag": "disputed"}
    {"op": "list",   "invoice_id": "..."}
    {"op": "find",   "tag": "disputed"}      → the invoices carrying it

Applying runs whatever the firm attached to `INVOICE_TAG#<tag>`, which is where the automation hangs:
`ready-to-bill` fires a rule that issues, `disputed` fires one that opens a task.

A tag must be declared in the `invoice_tags` registry first — that is what keeps a vocabulary
comparable across firms instead of invented per-invoice, and what lets two spellings of one idea not
become two ideas. An agent applying interactively is told by name and declares it in the same turn.
Unattended, nobody sees that, so the refusal files an incident.
"""

import json

from _helpers import (
    authed_by, drop_tag, get_invoice, invoices_tagged, put_tag, read_tags,
    run_tag_rules, tag_declared, ok, err,
)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    op = (body.get("op") or "").strip().lower()
    tag = (body.get("tag") or "").strip()
    invoice_id = (body.get("invoice_id") or "").strip()

    if op == "find":
        if not tag:
            return err("tag is required — which tag to find invoices by")
        return ok({"tag": tag, "invoice_ids": invoices_tagged(tag)})

    if not invoice_id:
        return err("invoice_id is required")
    inv = get_invoice(invoice_id)
    if not inv:
        return err(f"invoice not found: {invoice_id}", status=404)

    if op == "list":
        return ok({"invoice_id": invoice_id, "tags": read_tags(invoice_id)})

    if not tag:
        return err("tag is required")

    if op == "apply":
        if not tag_declared(tag):
            who = authed_by(event)
            if not who:
                # Nobody is watching this one — a rule effect, a script, a scheduled invoke. The
                # incident carries the tag, and the rule that wanted it is a row, so the agent has
                # both halves when the owner asks.
                print(json.dumps({
                    "event": "tag_undeclared", "incident": "fail",
                    "subject": f"invoice-tag:{tag}", "category": "invoicing",
                    "label": f"Tag `{tag}`",
                    "invoice_id": invoice_id,
                    "error": f"tag '{tag}' is not declared in the invoice_tags registry",
                }))
            return err(f"tag '{tag}' is not declared — add it to the invoice_tags registry with "
                       f"write_schema (op: extend) first, then apply it", status=409)
        put_tag(invoice_id, tag, authed_by(event))
        ran = run_tag_rules(invoice_id, tag, applied=True)
        return ok({"invoice_id": invoice_id, "tag": tag, "applied": True,
                   **({"rules": ran} if ran else {})})

    if op == "remove":
        # Removing one that is not there is not an error: the caller wanted it gone and it is gone.
        was = drop_tag(invoice_id, tag)
        ran = run_tag_rules(invoice_id, tag, applied=False) if was else []
        return ok({"invoice_id": invoice_id, "tag": tag, "removed": was,
                   **({"rules": ran} if ran else {})})

    return err("op must be one of: apply, remove, list, find")
