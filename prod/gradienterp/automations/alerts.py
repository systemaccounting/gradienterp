"""gradienterp's golive alerts: a mail to report@ when an account signs up and when a gerp is
created. Runs on `run_automation` rows at `METRIC#account.signed_up` and
`METRIC#subscription.started` (the metric_posted callsite), so what arrives as params is the posted
event: `event`, `subject_id`, `ts`, `properties`, `via`, `caller`. The subject is an account sub,
and upsert_customer_contact.py keeps that person as a contact keyed by it, so the mail names them.
Off is deleting the two rows; this script then sits approved and idle."""

TO = "report@gradienterp.cloud"


def _person(ctx, subject_id):
    """The name and email behind an account sub, or nothing: the contact lands through the web
    app's hook after the confirmation, so a signup can arrive before it."""
    try:
        got = ctx.call("manage_contacts", {"op": "get", "contact_id": subject_id}) or {}
    except Exception:  # noqa: BLE001
        return "", ""
    c = got.get("contact") or got
    name = (c.get("name") or " ".join(x for x in (c.get("first"), c.get("last")) if x)).strip()
    return name, (c.get("email") or "").strip()


def run(ctx, event="", subject_id="", properties=None, **_):
    p = properties or {}
    name, email = _person(ctx, subject_id)
    who = " ".join(x for x in (name, email) if x) or subject_id
    if event == "account.signed_up":
        subject = f"new account: {who}"
        body = f"{who} signed up via {p.get('source') or 'web'}\naccount {subject_id}"
    elif event == "subscription.started":
        subject = f"new gerp: {p.get('gerp_id')} in {p.get('region')}"
        body = f"{p.get('gerp_id')} in {p.get('region')}, plan {p.get('plan')}\nowner {who}\naccount {subject_id}"
    else:
        return {"skipped": f"no line for {event}"}
    ctx.call("send_email", {"to": TO, "subject": subject, "body": body})
    return {"mailed": TO, "event": event}
