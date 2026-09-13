def run(ctx, gerp_id, aws_account_id="", invoice_id="", requested_by="", **params):
    """The window's end: close the AWS account.

    `CloseAccount` is an Organizations call only the management account can make, so this does not
    make it. It assumes the closure role and invokes tower's `close_account`, which assumes into
    management for that one call and marks the row `closed`. Same shape as the build `begin`
    started: the script holds no credential, the role names exactly what it may do.

    The account closes whether or not the invoice was paid inside the window: the ERP application
    was destroyed on day 15 and the export delivered, so keeping the account open keeps an empty
    account. Paying settles what is owed — the balance recorded here is what is still unpaid — and
    the notices say so. A requested one goes ahead unless the row was moved off `close_requested`.

    Organizations closes 3 accounts at a time and 250 (or 20% of the org) per rolling 30 days. A
    429 from `close_account` is that limit: this schedules itself again `retry_after_s` on with the
    params it was called with, and nothing is filed.
    """
    import os, json
    balance_owed = 0
    if invoice_id:
        inv = ctx.call("manage_invoice", {"op": "get", "invoice_id": invoice_id})
        invoice = next(iter((inv or {}).get("invoices", [])), None)
        if invoice and invoice.get("status") == "unpaid":
            balance_owed = float(invoice.get("total") or 0)
    role, fn = os.environ.get("CLOSURE_REQUESTER_ROLE_ARN", ""), os.environ.get("CLOSE_ACCOUNT_FN", "")
    if not role or not fn:
        print(json.dumps({"event": "closure_withheld", "incident": "fail", "subject": f"closure:{gerp_id}",
                          "category": "collection", "label": f"AWS account for {gerp_id} not closed",
                          "error": "this gerp cannot close accounts (no role or function configured)"}))
        return {"gerp_id": gerp_id, "closed": False, "why": "not configured"}
    op = _operator(role, gerp_id)
    # how it ended and what is owed ride to the row: the gerp-cloud BFF reads them when the account
    # behind this gerp is deleted
    r = op.client("lambda").invoke(FunctionName=fn, Payload=json.dumps(
        {"gerp_id": gerp_id, "aws_account_id": aws_account_id,
         "how": "unpaid" if invoice_id else "requested", "invoice_id": invoice_id,
         "balance_owed": balance_owed}).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    body = out.get("body")
    body = json.loads(body) if isinstance(body, str) else (body or out)
    if out.get("statusCode") == 429 and not r.get("FunctionError"):
        return _wait_turn(ctx, gerp_id, aws_account_id, invoice_id, requested_by, body)
    if out.get("statusCode", 200) >= 300 or r.get("FunctionError"):
        raise RuntimeError(f"close_account refused: {body}")
    return {"gerp_id": gerp_id, "closed": True, **{k: v for k, v in body.items() if k != "gerp_id"}}


def _wait_turn(ctx, gerp_id, aws_account_id, invoice_id, requested_by, body):
    """Organizations named a quota; this step's turn is `retry_after_s` on. The one-shot that ran
    this deletes itself after firing, and is dropped first anyway so a run before it has gone
    replaces it rather than colliding with it."""
    import datetime, json
    retry_after_s = int(body.get("retry_after_s") or 3600)
    reason = body.get("reason") or "quota"
    retry_at = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=retry_after_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        ctx.call("manage_automation", {"op": "unschedule", "script": "closure/close.py", "subject": gerp_id})
    except Exception:  # noqa: BLE001 — already gone is the ordinary case
        pass
    ctx.call("manage_automation", {"op": "schedule", "script": "closure/close.py", "subject": gerp_id,
                                     "schedule_expression": f"at({retry_at[:-1]})", "one_shot": True,   # at() takes no Z; UTC
                                     "params": {"gerp_id": gerp_id, "aws_account_id": aws_account_id,
                                                "invoice_id": invoice_id, "requested_by": requested_by}})
    print(json.dumps({"event": "closure_waits", "gerp_id": gerp_id, "reason": reason, "retry_at": retry_at}))
    return {"gerp_id": gerp_id, "closed": False, "why": "waits its turn", "reason": reason, "retry_at": retry_at}


def _operator(role, gerp_id):
    import boto3
    c = boto3.client("sts").assume_role(RoleArn=role, RoleSessionName=f"closure-{gerp_id}"[:64])["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"])
