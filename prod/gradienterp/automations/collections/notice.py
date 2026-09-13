def run(ctx, invoice_id, closes_after=15, **params):
    """Tell the customer the invoice is unpaid, with a link that works today.

    One repeating schedule fires this — `every: 3 days, until: <+15d>` — so it is called with the
    SAME arguments every time. It cannot be told which day it is; it works that out from the
    invoice's own `unpaid_at`, which `mark_unpaid` stamped when the charge failed.

    Everything is read FRESH. The amount can change under a sequence that runs for two weeks, and a
    Checkout Session expires within 24 hours, so a link made when the sequence started would be dead
    long before the second notice.

    Order matters. The link is created first and the notice is sent either way: a notice carrying a
    dead link is worse than one carrying none, because the reader tried and it did not work, while a
    notice with the amount, the days overdue and the closure date is already a working notice.
    """
    import time

    inv = ctx.call("manage_invoice", {"op": "get", "invoice_id": invoice_id})
    invoice = next(iter((inv or {}).get("invoices", [])), None)
    if not invoice:
        return {"skipped": "no such invoice", "invoice_id": invoice_id}
    if invoice.get("status") != "unpaid":
        # The re-read is what makes the sequence correct rather than the deleting. Someone who paid
        # between one notice and the next gets no more of them, whether or not anything cancelled
        # the schedule.
        return {"skipped": f"status is {invoice.get('status')}", "invoice_id": invoice_id}

    unpaid_at = invoice.get("unpaid_at")
    if not unpaid_at:
        return {"skipped": "no unpaid_at to count from", "invoice_id": invoice_id}
    day = int((time.time() * 1000 - int(unpaid_at)) // 86_400_000)

    # The invoice names a CONTACT, not an address — `customer` is a contact_id. Reading the contact
    # is also where the recipient legitimately comes from: the firm's own records, rather than
    # anything that arrived in content the firm received.
    to = (params.get("to") or "").strip()
    if not to:
        got = ctx.call("manage_contacts", {"op": "get", "contact_id": invoice.get("customer")}) or {}
        contact = got.get("contact") or got
        to = (contact.get("email") or "").strip()
    if not to:
        # No address is not a reason to fail the sequence — but it IS a reason someone has to fix
        # something, so say which invoice and stop rather than looping silently.
        return {"skipped": "no email on the contact", "invoice_id": invoice_id,
                "contact_id": invoice.get("customer")}

    link = ""
    try:
        made = ctx.call("payment_links", {"kind": "payment", "invoice_id": invoice_id})
        link = (made or {}).get("url", "")
    except Exception as e:  # noqa: BLE001
        # Degrade rather than skip — see the docstring.
        print(f"payment link unavailable for {invoice_id}: {e}")

    left = max(0, int(closes_after) - day)
    body = "\n".join(filter(None, [
        f"Invoice {invoice_id} is {day} days past due.",
        f"Amount outstanding: {invoice.get('total')}",
        f"Pay now: {link}" if link else None,
        "",
        (f"If it stays unpaid, the instance is backed up and torn down in {left} days, and your "
         "records stay available to download afterwards.") if left else
        "This is the last notice before the instance is backed up and torn down.",
        "",
        "Reply to this message if something is wrong with the invoice.",
    ]))

    ctx.call("send_email", {"to": to,
                            "subject": f"Invoice {invoice_id} — {day} days past due",
                            "body": body})
    return {"invoice_id": invoice_id, "day": day, "link": bool(link), "days_left": left}
