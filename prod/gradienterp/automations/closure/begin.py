def run(ctx, gerp_id="", customer="", invoice_id="", requested_by="", requested_at="",
        aws_account_id="", closes_after=15, **params):
    """Backup & destroy, for either reason — then schedule the window that follows.

    Called two ways. The `deadline` row on `INVOICE_STATUS#unpaid` schedules it fifteen days after an
    invoice goes unpaid (the invoice rides in `invoice_id`, the gerp in `customer`). The owner web app
    runs it at once when a customer asks to close (`requested_by`, `requested_at`). From here the
    two are one sequence: the build exports the customer's records into their own account and
    destroys the stack; notices say where the export is and when the account closes; fifteen days
    later `closure/close.py` closes the AWS account.

    **The gate is here, once.** A person stands in front of the destroy. For an unpaid closure that
    is the case this files — closed and tagged `approved` through the portal. For a requested one
    the customer already approved by typing the confirmation, so this tags the case itself with
    their account on it. Closing the AWS account fifteen days later has no second gate: it is the
    consequence the notices announce, and the export has been delivered by then.

    **Unapproved, it withholds** — files an incident, destroys nothing, and reschedules itself daily
    for the window so an approval on day 16 acts on day 17 with nobody re-running anything.

    **A request during an unpaid chase replaces the chase.** The unpaid notices for this gerp are
    dropped, and if the day-15 case is already waiting, the request is its approval. The invoice
    stays unpaid; asking to leave does not settle what is owed.
    """
    import json, os
    gerp_id = (gerp_id or customer or "").strip()
    if not gerp_id:
        return {"skipped": "nothing names which gerp this is for"}
    requested = bool(requested_by)
    subject = f"closure:{gerp_id}"

    role = os.environ.get("CLOSURE_REQUESTER_ROLE_ARN", "")
    if not role:
        return {"skipped": "this gerp cannot request closures", "gerp_id": gerp_id}
    op = _operator(role, gerp_id)
    table = os.environ["CUSTOMERS_TABLE"]

    # ── the reason, re-read ──
    if requested:
        row = _row(op, table, gerp_id)
        if not row:
            return {"skipped": "no such customer row", "gerp_id": gerp_id}
        if row.get("status") not in ("close_requested", "closing"):
            return {"skipped": f"row status is {row.get('status')}", "gerp_id": gerp_id}
        aws_account_id = aws_account_id or row.get("aws_account_id", "")
        # the notices go to the owner who asked; an unpaid closure's go to the invoice's contact
        to, label = row.get("owner_email", ""), row.get("label", "")
        invoice = None
    else:
        inv = ctx.call("manage_invoice", {"op": "get", "invoice_id": invoice_id}) if invoice_id else {}
        invoice = next(iter((inv or {}).get("invoices", [])), None)
        if not invoice:
            return {"skipped": "no such invoice", "invoice_id": invoice_id, "gerp_id": gerp_id}
        if invoice.get("status") != "unpaid":
            return {"skipped": f"status is {invoice.get('status')}", "invoice_id": invoice_id}
        to, label = "", ""

    # ── a request replaces a chase ──
    dropped = []
    if requested:
        unpaid = (ctx.call("manage_invoice", {"op": "get", "customer": gerp_id, "status": "unpaid"}) or {}).get("invoices", [])
        for inv_ in unpaid:
            for script in ("collections/notice.py",):
                try:
                    ctx.call("manage_automation", {"op": "unschedule", "script": script, "subject": inv_["invoice_id"]})
                    dropped.append(f"{script}@{inv_['invoice_id']}")
                except Exception:  # noqa: BLE001 — not scheduled is not an error
                    pass

    # ── the case ──
    found = (ctx.call("manage_tasks", {"op": "query", "subject_key": subject}) or {}).get("tasks") or []
    task = sorted(found, key=lambda t: t.get("created_at") or 0)[-1] if found else None
    if task is None:
        why = (f"Customer {gerp_id} asked to close their account ({requested_by}, {requested_at})."
               if requested else
               f"Invoice {invoice_id} has been unpaid for {closes_after} days.\n"
               f"Customer: {gerp_id}\nAmount: {invoice.get('total')}")
        content = "\n".join([
            why, "",
            "Approving this exports their records into their own AWS account and destroys the "
            "instance. The account stays open, billed, for fifteen days so the export can be "
            "downloaded, then it is closed.",
            "" if requested else
            "Tag this task `approved` to authorise it. Left untagged, nothing is destroyed — the "
            "instance stays up and this is asked again daily for the window.",
        ]).strip()
        put = ctx.call("manage_tasks", {"op": "put", "content": content, "category": "collection",
                                     "subject_key": subject, "invoice_id": invoice_id or ""}) or {}
        task = put.get("task") or put
    task_id = task.get("task_id")

    tags = [t["tag"] if isinstance(t, dict) else t
            for t in ((ctx.call("manage_tasks", {"op": "get", "task_id": task_id}) or {}).get("tags") or [])]

    if requested and "approved" not in tags:
        # the confirmation the customer typed is the approval; the tag with their account is the record
        ctx.call("manage_tasks", {"op": "update", "task_id": task_id, "add_tag": "approved", "applied_by": requested_by})
        ctx.call("manage_tasks", {"op": "update", "task_id": task_id, "deliver": "now"})
        tags.append("approved")

    if "closure_requested" in tags:
        return {"gerp_id": gerp_id, "requested": False, "why": "already requested", "task_id": task_id}

    if "approved" not in tags:
        _retry_daily(ctx, gerp_id, closes_after, params_for(gerp_id, invoice_id, requested_by,
                                                             requested_at, aws_account_id))
        return _withhold(gerp_id, invoice_id, "the closure task is awaiting approval", task_id)

    # ── approved: record, then the build ──
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    op.client("dynamodb").update_item(
        TableName=table, Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET #s = :s, close_requested_at = if_not_exists(close_requested_at, :t), close_requested_by = :b",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": {"S": "close_requested"}, ":t": {"S": now.isoformat()},
            ":b": {"S": (requested_by if requested else f"invoice {invoice_id}, approved on task {task_id}")[:255]}})

    project = os.environ.get("CLOSE_BUILD_PROJECT", "")
    if not project:
        # the operator's own switch. Nothing is scheduled either: without the export there is
        # nothing to download, and an account-close timer against a live stack is the one thing
        # this sequence must never arm.
        return {"gerp_id": gerp_id, "requested": True, "teardown": False, "task_id": task_id,
                "note": "recorded; closure is switched off here so nothing was destroyed"}

    # tag BEFORE the build starts, so a retry after a partial failure cannot start a second one
    ctx.call("manage_tasks", {"op": "update", "task_id": task_id, "add_tag": "closure_requested",
                              "applied_by": f"closure:{gerp_id}"})
    build = op.client("codebuild").start_build(
        projectName=project,
        environmentVariablesOverride=[
            {"name": "CUSTOMER_ID", "value": gerp_id, "type": "PLAINTEXT"},
            {"name": "CUSTOMER_ACCOUNT_ID", "value": aws_account_id or "", "type": "PLAINTEXT"},
            {"name": "TF_ACTION", "value": "destroy", "type": "PLAINTEXT"},
        ])

    # ── the window ──
    # One instant, computed once from `closes_after`, and everything that follows reads it: the
    # close fires AT it (not "rate(15 days)" from whenever this ran), the notices end at it and say
    # it, the case is due on it, the row carries it for the owner console, and the log names it.
    closes_on = (now + datetime.timedelta(days=int(closes_after))).strftime("%Y-%m-%dT%H:%M:%SZ")
    following = params_for(gerp_id, invoice_id, requested_by, requested_at, aws_account_id)
    following.update({"closes_on": closes_on, "to": to, "label": label})
    scheduled = []
    for script, expression, extra in (
        ("closure/notice.py", "rate(3 days)", {"end_date": closes_on}),
        ("closure/close.py", f"at({closes_on[:-1]})", {"one_shot": True}),   # at() takes no Z; UTC
    ):
        try:
            ctx.call("manage_automation", {"op": "schedule", "script": script, "subject": gerp_id,
                                             "schedule_expression": expression,
                                             "params": following, **extra})
            scheduled.append(script)
        except Exception as e:  # noqa: BLE001
            if "already exists" not in str(e):
                raise
    for script in ("closure/begin.py",):      # the daily retry, if one was armed
        try:
            ctx.call("manage_automation", {"op": "unschedule", "script": script, "subject": gerp_id})
        except Exception:  # noqa: BLE001
            pass
    # the date on the case and on the row, so a person reading either sees when the account closes
    ctx.call("manage_tasks", {"op": "update", "task_id": task_id, "updates": {"due_date": closes_on}})
    op.client("dynamodb").update_item(
        TableName=table, Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET closes_on = :c", ExpressionAttributeValues={":c": {"S": closes_on}})
    build_id = build.get("build", {}).get("id", "")
    print(json.dumps({"event": "closure_scheduled", "gerp_id": gerp_id, "closes_on": closes_on,
                      "closes_after_days": int(closes_after), "build_id": build_id,
                      "invoice_id": invoice_id or "", "requested_by": requested_by or "", "task_id": task_id}))
    return {"gerp_id": gerp_id, "requested": True, "teardown": True, "task_id": task_id,
            "build_id": build_id, "closes_on": closes_on,
            "scheduled": scheduled, "dropped": dropped}


def params_for(gerp_id, invoice_id, requested_by, requested_at, aws_account_id):
    return {"gerp_id": gerp_id, "invoice_id": invoice_id or "", "requested_by": requested_by or "",
            "requested_at": requested_at or "", "aws_account_id": aws_account_id or ""}


def _retry_daily(ctx, gerp_id, closes_after, p):
    """A daily re-ask for the window. Named `(closure/begin.py, gerp)` — the same name the day-15
    deadline had, and the Scheduler deletes a one-shot after it fires, so the name is free by the
    time this runs from it. Dropped first anyway: a manual run before day 15 replaces the
    deadline with the daily ask rather than colliding with it."""
    import datetime
    end = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(days=int(closes_after))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        ctx.call("manage_automation", {"op": "unschedule", "script": "closure/begin.py", "subject": gerp_id})
    except Exception:  # noqa: BLE001 — not scheduled is the ordinary case from the web app
        pass
    try:
        ctx.call("manage_automation", {"op": "schedule", "script": "closure/begin.py", "subject": gerp_id,
                                         "schedule_expression": "rate(1 day)", "params": p,
                                         "end_date": end})
    except Exception as e:  # noqa: BLE001
        if "already exists" not in str(e):
            raise


def _operator(role, gerp_id):
    import boto3
    c = boto3.client("sts").assume_role(RoleArn=role, RoleSessionName=f"closure-{gerp_id}"[:64])["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"])


def _row(op, table, gerp_id):
    item = op.client("dynamodb").get_item(TableName=table, Key={"gerp_id": {"S": gerp_id}}).get("Item") or {}
    return {k: next(iter(v.values())) for k, v in item.items()}


def _withhold(gerp_id, invoice_id, why, task_id):
    """Nothing closes and nothing fails. `create_inc_from_log` files on the `incident` line."""
    import json
    print(json.dumps({"event": "closure_withheld", "incident": "fail", "subject": f"closure:{gerp_id}",
                      "category": "collection", "label": f"Closure for {gerp_id} not authorised",
                      "error": f"{why} — {gerp_id} is still running"}))
    return {"gerp_id": gerp_id, "invoice_id": invoice_id, "requested": False, "why": why,
            "task_id": task_id}
