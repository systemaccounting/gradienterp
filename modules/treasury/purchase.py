"""modules/treasury — paying for an agreed rule.

Step 3 of the handshake: request → accept → **purchase** → create.

Paying is not a new decision. The owner committed when they named the terms, and the counterparty's
stamp closed it — so this fires off the ACCEPT, not off another yes. Coming back with "ready to send
the 500k?" re-opens something the owner already settled, and makes the agent feel like a form.

Which entry depends only on which side of the row this firm is on, and they are transposes:

    buyer   DR INVESTMENTS   / CR CASH           the claim acquired, carried at cost
    seller  DR CASH          / CR OWNER_EQUITY   capital in — at risk, no vote, a capped return

Both stamp the entry id onto the agreement as `funds_receipt_ledger_entry`, which is what lets
`create` know the money is accounted for. Deterministic ids (`capital-out-<thread>` /
`capital-<thread>`) so a redelivered stream record neither double-books nor re-creates.

Shared by the stream consumer (which is the normal path) and the two agent tools (which stay for the
case where an owner records a deal struck before the rails existed).
"""

from agreements import note

BUY_ENTRY = "capital-out-{thread}"
SELL_ENTRY = "capital-{thread}"


def legs(side: str, amount):
    """The two legs, by which side of the deal this firm is on."""
    if side == "buyer":
        return [
            {"account": "INVESTMENTS", "accountType": "ASSET", "side": "DEBIT", "amount": amount},
            {"account": "CASH", "accountType": "ASSET", "side": "CREDIT", "amount": amount},
        ]
    return [
        {"account": "CASH", "accountType": "ASSET", "side": "DEBIT", "amount": amount},
        {"account": "OWNER_EQUITY", "accountType": "EQUITY", "side": "CREDIT", "amount": amount},
    ]


def entry_id(side: str, thread: str) -> str:
    return (BUY_ENTRY if side == "buyer" else SELL_ENTRY).format(thread=thread)


def pay(row: dict, gerp_id: str, post, now_ms) -> str | None:
    """Post this firm's money leg for an agreed row and stamp it on the agreement.

    Returns the posted entry id, or None when there is nothing to do — already paid, not agreed, or
    this firm is neither party (which happens on a row that arrived for someone else).

    `post` and `now_ms` are injected rather than imported so this stays usable from both the stream
    consumer and the tool lambdas, which carry different `_helpers`."""
    if not (row.get("buyer_stamp") and row.get("seller_stamp")):
        return None                                  # not agreed — nothing has been committed to yet
    if row.get("funds_receipt_ledger_entry"):
        return None                                  # already paid; the stamp is the idempotency guard

    side = "buyer" if row.get("buyer") == gerp_id else "seller" if row.get("seller") == gerp_id else None
    if not side:
        return None

    terms = row.get("terms") or {}
    amount = terms.get("total")
    if amount is None or float(amount) <= 0:
        print(f"[purchase] {row.get('thread')} has no price on its terms; not posting")
        return None

    spec = (terms.get("items") or [{}])[0]
    counterparty = row.get("seller") if side == "buyer" else row.get("buyer")
    eid = entry_id(side, row["thread"])
    posted = post({
        "lineItems": legs(side, amount),
        "memo": (f"capital out to {counterparty}" if side == "buyer" else f"capital in from {counterparty}")
                + f" for {spec.get('product')} (thread {row['thread']})",
        "source": eid,
        "dimensions": {"instrument_id": row["thread"],
                       "issuer" if side == "buyer" else "holder": counterparty,
                       "rule": spec.get("product")},
        "entryId": eid,
        "timestamp": str(now_ms()),
    })
    if not posted:
        print(f"[purchase] failed to post the {side} leg for {row['thread']}")
        return None

    note(row["thread"], row["terms_hash"], funds_receipt_ledger_entry=posted)
    print(f"[purchase] {side} leg {posted} for {amount} on {row['thread']}")
    return posted
