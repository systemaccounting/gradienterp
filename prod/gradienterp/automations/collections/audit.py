def run(ctx, **params):
    """Daily: does every scheduled step still have a reason to exist, and does every reason still
    have its steps?

    Everything else in these sequences reacts to an event, and an event can be dropped — `ingest_stripe`
    went three months without an invocation because its endpoint was registered in test mode while
    charges ran live. Being wrong here is not a stale row: it is a customer's account closing after
    they paid, and the question afterwards being "what happened to my account".

    **It fixes and records.** A countdown with no reason is deleted on sight — a report that waits
    is a report that lets the countdown reach zero — and the incident says which subject, which
    schedules, and what disagreed. Correcting silently would hide the bug that made this necessary.

    **Two sequences, two subjects.** The chase is named by the invoice and its reason is an unpaid
    invoice. The closure is named by the gerp and its reason is EITHER an unpaid invoice for that
    gerp OR a customer row that says `close_requested`/`closing`. A closure with neither is stale.
    The mirror — an unpaid invoice with nothing chasing it — costs the customer nothing and the firm
    the whole debt, which is exactly why it is easy to leave out.
    """
    import json, os

    def scheduled_for(steps):
        out = {}
        for step in steps:
            for row in (ctx.call("manage_automation", {"op": "list", "script": step}) or {}).get("schedules", []):
                subject = row.get("subject") or ""
                if subject:
                    out.setdefault(subject, []).append(step)
        return out

    chase = scheduled_for(("collections/notice.py",))
    closure = scheduled_for(("closure/begin.py", "closure/notice.py", "closure/close.py"))

    # `ctx.subject` is the same sanitizing the scheduling tool applied; a listing hands back that
    # form and nothing hands back the original
    unpaid_by_invoice, unpaid_gerps = {}, set()
    for inv in (ctx.call("manage_invoice", {"op": "get", "status": "unpaid"}) or {}).get("invoices", []):
        unpaid_by_invoice[ctx.subject(str(inv["invoice_id"]))] = inv["invoice_id"]
        if inv.get("customer"):
            unpaid_gerps.add(ctx.subject(str(inv["customer"])))

    def row_says_closing(gerp_subject):
        role, table = os.environ.get("CLOSURE_REQUESTER_ROLE_ARN", ""), os.environ.get("CUSTOMERS_TABLE", "")
        if not role or not table:
            return False
        import boto3
        c = boto3.client("sts").assume_role(RoleArn=role, RoleSessionName=f"closure-audit"[:64])["Credentials"]
        ddb = boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                            aws_session_token=c["SessionToken"]).client("dynamodb")
        # the subject is sanitized and the key is not: only an exact id can be read back, so a gerp
        # id that was cut or rewritten to fit the name reads as "not closing" and is reported
        item = ddb.get_item(TableName=table, Key={"gerp_id": {"S": gerp_subject}}).get("Item") or {}
        return (item.get("status") or {}).get("S") in ("close_requested", "closing")

    stale, dropped, unchased = [], [], []

    for subject, steps in sorted(chase.items()):
        if subject in unpaid_by_invoice:
            continue
        stale.append(f"chase:{subject}")
        for step in steps:
            try:
                ctx.call("manage_automation", {"op": "unschedule", "script": step, "subject": subject})
                dropped.append(f"{step}@{subject}")
            except Exception as e:  # noqa: BLE001
                print(f"could not unschedule {step} for {subject}: {e}")

    for subject, steps in sorted(closure.items()):
        if subject in unpaid_gerps or row_says_closing(subject):
            continue
        stale.append(f"closure:{subject}")
        for step in steps:
            try:
                ctx.call("manage_automation", {"op": "unschedule", "script": step, "subject": subject})
                dropped.append(f"{step}@{subject}")
            except Exception as e:  # noqa: BLE001
                print(f"could not unschedule {step} for {subject}: {e}")

    for subject, invoice_id in sorted(unpaid_by_invoice.items()):
        if not chase.get(subject) and not any(closure.get(g) for g in unpaid_gerps):
            unchased.append(invoice_id)

    if stale:
        print(json.dumps({
            "event": "collection_audit", "incident": "fail",
            "subject": "collection:audit", "category": "collection",
            "label": "Schedules outliving their reason",
            "error": (f"{len(stale)} subject(s) had steps scheduled with no reason left: {stale[:10]}; "
                      f"{len(dropped)} schedule(s) deleted. Each one was a countdown running against "
                      "something that had moved on — find which event was missed rather than only "
                      "accepting the cleanup."),
        }))
    if unchased:
        print(json.dumps({
            "event": "collection_audit", "incident": "fail",
            "subject": "collection:unchased", "category": "collection",
            "label": "Unpaid invoices nothing is chasing",
            "error": (f"{len(unchased)} invoice(s) are unpaid with no steps scheduled: "
                      f"{unchased[:10]}. Either the transition ran before the rules existed, or "
                      "the rows on INVOICE_STATUS#unpaid were removed."),
        }))
    if not stale and not unchased:
        print(json.dumps({
            "event": "collection_audit", "incident": "ok",
            "subject": "collection:audit", "category": "collection",
            "label": "Collection schedules", "checked": len(chase) + len(closure)}))

    return {"chase_subjects": len(chase), "closure_subjects": len(closure),
            "unpaid_invoices": len(unpaid_by_invoice),
            "stale": stale, "unscheduled": dropped, "unchased": unchased}
