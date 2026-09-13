"""The firm's outbound mail servers, one row per address it may send AS.

    pk = gerp_id, sk = SENDER#billing@shop.com
        → {host, port, username, tls, secret_name, default}

The FROM-ADDRESS is the key because that is what varies and what a caller cares about: a script
says who the mail is from and never names a machine to connect to. `billing@` for invoices,
`hello@` for correspondence, and a firm running two brands has two domains.

It is also the whole sending constraint. A gerp can only send as an address that has a row, and
rows are written deliberately — by `configure_smtp`, and only after a test message actually sent.

There is no fallback. A gerp with no rows cannot send, which is a real state for one whose owner
has not configured a mail server yet, and saying so is better than sending as an address the firm
does not own.
"""

import os

from aws import resource

TABLE = os.environ.get("SETTINGS_TABLE", "")
GERP_ID = os.environ.get("GERP_ID", "")
PREFIX = "SENDER#"


class NoSender(RuntimeError):
    pass


def _rows():
    from boto3.dynamodb.conditions import Key

    resp = resource("dynamodb").Table(TABLE).query(
        KeyConditionExpression=Key("gerp_id").eq(GERP_ID) & Key("sk").begins_with(PREFIX),
    )
    return resp.get("Items", [])


def resolve(address: str = "") -> dict:
    """The sender for this send, or a readable refusal.

    Given an address, it must have a row. Omitted, the row flagged `default` — and if none is
    flagged, nothing sends. Never an arbitrary row: a firm with three senders and no default
    would otherwise have mail go out claiming to be from whichever one sorted first.
    """
    rows = _rows()
    if not rows:
        raise NoSender(
            "no mail server is configured for this firm yet, so nothing can be sent. "
            "Configure one with configure_smtp and I can email from your own address."
        )

    by_address = {r["sk"][len(PREFIX):]: r for r in rows}
    if address:
        row = by_address.get(address)
        if not row:
            known = ", ".join(sorted(by_address)) or "none"
            raise NoSender(f"this firm cannot send as {address} — configured senders: {known}")
        return _sender(address, row)

    # most recently updated wins a tie, so a half-landed flag move is never ambiguous
    flagged = sorted(
        (r for r in rows if r.get("default")),
        key=lambda r: int(r.get("updated_at", 0)),
        reverse=True,
    )
    if not flagged:
        known = ", ".join(sorted(by_address))
        raise NoSender(
            f"no default sender is set, so `from` is required — configured senders: {known}"
        )
    return _sender(flagged[0]["sk"][len(PREFIX):], flagged[0])


def _sender(address: str, row: dict) -> dict:
    return {
        "address": address,
        "host": row["host"],
        "port": int(row.get("port", 587)),
        "username": row.get("username") or address,
        "tls": row.get("tls", "starttls"),
        "secret_name": row["secret_name"],
    }
