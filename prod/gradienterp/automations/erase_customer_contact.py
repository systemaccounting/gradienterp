def run(ctx, account_id, **_):
    """gradienterp's own CRM, the other way: the person behind a deleted account, erased.

    Called through the hook the firm published for its web app (`customers/erase`), when the
    account is deleted. The contact `customers/upsert` wrote stays as a row — the firm's invoices
    reference its id — and names nobody: the name becomes "erased account", the email, phone and
    address go. `is_customer` stays, so the invoice history still reads as a customer's.

    `manage_contacts` update merges, so only the named fields change. A contact that was never
    written (an account deleted before it ever saved its record) is nothing to erase.
    """
    if not account_id:
        return {"skipped": "account_id is required"}
    try:
        ctx.call("manage_contacts", {"op": "update", "contact_id": account_id, "updates": {
            "first_name": "erased", "middle_name": "", "last_name": "account",
            "email": "", "phone": "", "addresses": []}})
    except Exception as e:  # noqa: BLE001 — ctx.call raises on a non-2xx; only a 404 is not an error
        if getattr(e, "status", None) == 404:
            return {"contact_id": account_id, "erased": False, "why": "no contact"}
        raise
    return {"contact_id": account_id, "erased": True}
