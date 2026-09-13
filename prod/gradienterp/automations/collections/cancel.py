def run(ctx, invoice_id, customer="", **params):
    """The invoice was paid: drop everything this sequence still has scheduled against it.

    A tidy-up, not the guarantee. Every step re-reads the invoice and stops when the status has
    moved on, so an uncancelled schedule does nothing wrong — it wakes, reads, exits. What this
    saves is the waking.

    Two subjects. The chase is named by the invoice; the closure that follows is named by the gerp
    (a requested close has no invoice). A closure schedule dropped here after the build has run
    means the AWS account stays open — the instance is already gone, and paying does not bring it
    back. A script that is not scheduled is not an error.
    """
    dropped, absent = [], []
    for script, subject in (
        ("collections/notice.py", invoice_id),
        ("closure/begin.py", customer),
        ("closure/notice.py", customer),
        ("closure/close.py", customer),
    ):
        if not subject:
            continue
        try:
            ctx.call("manage_automation", {"op": "unschedule", "script": script, "subject": subject})
            dropped.append(f"{script}@{subject}")
        except Exception:  # noqa: BLE001
            absent.append(script)
    return {"invoice_id": invoice_id, "unscheduled": dropped, "not_scheduled": absent}
