def run(ctx, gerp_id, invoice_id="", requested_by="", closes_on="", to="", label="", **params):
    """Every three days of the window: the export is ready, here is where, here is the date.

    Same re-read as every step. An unpaid closure is told only while the invoice is still unpaid;
    a requested one only while the row still reads as closing or closed — a gerp the operator
    applied back into its account is not told it is shut. The recipient is the invoice's contact
    for an unpaid closure and the owner's address `begin` read off the row for a requested one. The first one goes out the moment `begin` schedules
    it, while the build is still exporting: the export is done within minutes and the screen
    the mail points at shows it.
    """
    if invoice_id:
        inv = ctx.call("manage_invoice", {"op": "get", "invoice_id": invoice_id})
        invoice = next(iter((inv or {}).get("invoices", [])), None)
        if not invoice or invoice.get("status") != "unpaid":
            return {"skipped": "invoice is no longer unpaid", "gerp_id": gerp_id}
        got = ctx.call("manage_contacts", {"op": "get", "contact_id": invoice.get("customer")}) or {}
        to = to or ((got.get("contact") or got).get("email") or "").strip()
    elif requested_by:
        import os
        role, table = os.environ.get("CLOSURE_REQUESTER_ROLE_ARN", ""), os.environ.get("CUSTOMERS_TABLE", "")
        if not role or not table:
            return {"skipped": "this gerp cannot read closures", "gerp_id": gerp_id}
        status = _row_status(role, table, gerp_id)
        if status not in ("close_requested", "closing", "closed"):
            return {"skipped": f"row is {status or 'gone'}", "gerp_id": gerp_id}
    if not to:
        return {"skipped": "no address to send to", "gerp_id": gerp_id}

    label = label or gerp_id
    day = closes_on[:10] if closes_on else ""
    body = "\n\n".join([
        f"Your {label} gerp has been shut down"
        + (" at your request." if requested_by else f" for non-payment of invoice {invoice_id}."),
        "Your books and documents were exported before the instance was removed. Download them from\n"
        "the gerp's screen at https://gradienterp.cloud — its card still opens for that — until the\n"
        + (f"AWS account closes on {day}. Everything still in it is deleted then." if day else
           "AWS account closes, fifteen days after the shutdown. Everything still in it is deleted then."),
        "Reply to this message if something is wrong." if requested_by else
        "Paying the invoice settles what you owe. It does not restore the instance, and the AWS "
        "account still closes on the date above.",
        f"— gradientERP  ({gerp_id})",
    ])
    ctx.call("send_email", {"to": to, "subject": f"your {label} gerp is closed"
                            + (f", export ready until {day}" if day else ""), "body": body})
    return {"gerp_id": gerp_id, "to": to}


def _row_status(role, table, gerp_id):
    import boto3
    c = boto3.client("sts").assume_role(RoleArn=role, RoleSessionName=f"closure-{gerp_id}"[:64])["Credentials"]
    ddb = boto3.client("dynamodb", aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                       aws_session_token=c["SessionToken"])
    item = ddb.get_item(TableName=table, Key={"gerp_id": {"S": gerp_id}}).get("Item") or {}
    return item.get("status", {}).get("S", "")
