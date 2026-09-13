def run(ctx, account_id="", gerp_id="", name="", legal_name="", email="", first="", last="", middle="", phone="",
        street="", unit="", city="", state="", zip="", country="", gerp_ids=None, **_):
    """gradienterp's own CRM: the person behind an account, and the business behind a gerp, as
    contacts in gradienterp's books.

    Called through the hook the firm published for its web app (`customers/upsert`). The web app
    posts the account's private record whenever it changes; this writes it as a person contact
    keyed by the account id, so the firm's agent works on its customers as ordinary contacts —
    a dunning notice addresses a person, the mail merge is `contacts` + `send_email`, and "who is
    behind Card Test Co" is a contact read. Nothing platform-shaped.

    The record is the whole of it every time, but the contact holds more than the record: the
    Stripe ids the payments lambdas wrote when the account saved a card. `manage_contacts` put is
    create-or-replace and would drop them, so an existing contact is UPDATED (a merge) and only a
    contact nobody has met is put.

    `gerp_ids` are the gerps this account owns from before the business's own legal profile was
    asked. Each one's contact — the payer the firm's invoices bill — carries the owner's legal
    name as `legal_name`, because a gerp is not a legal entity and the invoice names the person
    running it. A corrected name lands on every one; a gerp whose contact is not born yet (no
    card saved) is skipped, and the card's arrival writes it.

    With `gerp_id` instead, the same door carries the business behind a gerp: its contact — an
    organization keyed by the gerp id — takes `name` (the label), `legal_name`, and the legal
    profile's email, phone and business address. The web app posts it whenever the owner edits
    the business info. The same merge: the contact keeps the Stripe ids the card landing wrote.
    """
    if not account_id and not gerp_id:
        return {"skipped": "account_id or gerp_id is required"}
    number, street_name = "", street.strip()
    if street_name and street_name.split(" ", 1)[0].rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ").isdigit():
        number, street_name = (street_name.split(" ", 1) + [""])[:2]
    if gerp_id:
        fields = {"contact_id": gerp_id, "entity_type": "organization", "is_customer": True,
                  "name": name, "legal_name": legal_name, "email": email, "phone": phone}
        address_type = "business"
    else:
        fields = {"contact_id": account_id, "entity_type": "person", "is_customer": True,
                  "first_name": first, "middle_name": middle, "last_name": last,
                  "email": email, "phone": phone}
        address_type = "home"
    if street_name or city or country:
        fields["addresses"] = [{"address_type": address_type, "street_number": number, "street_name": street_name,
                                "unit": unit, "city": city, "state": state, "postal_code": zip,
                                "country": country}]
    record = {k: v for k, v in fields.items() if v not in ("", None)}
    contact_id = record["contact_id"]
    try:
        out = ctx.call("manage_contacts", {"op": "update", "contact_id": contact_id,
                                           "updates": {k: v for k, v in record.items() if k != "contact_id"}})
    except Exception as e:  # noqa: BLE001 — ctx.call raises on a non-2xx; 404 is the first sight of this party
        if getattr(e, "status", None) != 404:
            raise
        out = ctx.call("manage_contacts", {"op": "put", **record})
    if gerp_id:
        return {"contact_id": gerp_id, "written": True, "contact": (out or {}).get("contact", out)}
    legal_name, gerps = " ".join(p for p in (first.strip(), last.strip()) if p), []
    for gid in (gerp_ids or []):
        if not gid or not legal_name:
            continue
        try:
            ctx.call("manage_contacts", {"op": "update", "contact_id": gid, "updates": {"legal_name": legal_name}})
            gerps.append(gid)
        except Exception as e:  # noqa: BLE001 — ctx.call raises on a non-2xx; a contact not yet born is not an error
            if getattr(e, "status", None) != 404:
                raise
    return {"contact_id": account_id, "written": True, "gerps": gerps,
            "contact": (out or {}).get("contact", out)}
