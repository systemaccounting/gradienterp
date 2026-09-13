"""Sending, over one connection, with per-recipient outcomes.

SMTP has no batch API and does not need one: open a connection and send N messages down it. The
handshake happens once, which is what a batch API would have saved anyway.

This is the same code for every provider. Google Workspace, Fastmail, a cPanel host, a self-run
postfix — connect, STARTTLS, login, loop, quit. Where providers DIFFER is pacing, and the reply
codes for that are standardised too, so the differences are handled without knowing whose server
it is:

    4xx   transient — back off and retry. What a provider says when you send too fast.
    5xx   permanent — record that recipient as failed and carry on. Bad address, rejected sender.
    drop  some providers close after N messages — reconnect and continue.

A message that fails is named in the result. A caller that retries a whole batch on a partial
failure sends the successes twice, and email has no idempotency key to save it.
"""

import smtplib
import time
from email.message import EmailMessage

from aws import log

RETRIES = 2          # a 4xx is the provider pacing us, not a broken message
BACKOFF = 2.0        # seconds, doubled per attempt


def _connect(sender: dict, password: str) -> smtplib.SMTP:
    if sender["tls"] == "ssl":
        conn = smtplib.SMTP_SSL(sender["host"], sender["port"], timeout=30)
    else:
        conn = smtplib.SMTP(sender["host"], sender["port"], timeout=30)
        if sender["tls"] != "none":
            conn.starttls()
    conn.login(sender["username"], password)
    return conn


def _compose(sender: str, msg: dict) -> EmailMessage:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = msg["to"]
    if msg.get("cc"):
        m["Cc"] = ", ".join(msg["cc"]) if isinstance(msg["cc"], list) else msg["cc"]
    m["Subject"] = msg.get("subject") or ""
    m.set_content(msg.get("body") or "")
    return m


def send_all(sender: dict, password: str, messages: list) -> dict:
    """Send every message, returning what happened to each.

    Raises only when NOTHING could be sent — a bad host, a rejected credential. That is the case
    where retrying the whole call is safe, so it reads as a failed call rather than as a partial.
    """
    conn = _connect(sender, password)   # raises: nothing sent, caller retries safely
    sent, failed = [], []
    try:
        for msg in messages:
            outcome, conn = _deliver(conn, sender, password, msg)
            (failed if outcome.get("error") else sent).append(outcome)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return {"sent_count": len(sent), "failed_count": len(failed), "sent": sent, "failed": failed}


def _deliver(conn, sender: dict, password: str, msg: dict):
    """One message. Returns (outcome, connection) — the connection may have been REPLACED, which
    is why it comes back out: a provider that closes mid-batch must not fail every message after
    the first one it dropped."""
    to = msg.get("to") or ""
    delay = BACKOFF
    for attempt in range(RETRIES + 1):
        last = attempt == RETRIES
        try:
            conn.send_message(_compose(sender["address"], msg))
            return {"to": to}, conn
        except smtplib.SMTPRecipientsRefused as e:
            code, text = _first_refusal(e.recipients, to)
            if code < 400 or code >= 500 or last:
                return {"to": to, "code": code, "error": text}, conn
        except smtplib.SMTPResponseException as e:
            if e.smtp_code < 400 or e.smtp_code >= 500 or last:
                return {"to": to, "code": e.smtp_code, "error": _text(e.smtp_error)}, conn
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            if last:
                log.warning("gave up after retries", sender=sender["address"], to=to,
                            attempts=attempt + 1, error=str(e))
                return {"to": to, "code": 0, "error": f"connection lost: {e}"}, conn
            try:
                conn = _connect(sender, password)
            except Exception as reconnect_error:
                log.warning("reconnect failed", sender=sender["address"], to=to,
                            host=sender.get("host"), error=str(reconnect_error))
                return {"to": to, "code": 0, "error": f"connection lost: {reconnect_error}"}, conn
        time.sleep(delay)
        delay *= 2
    log.warning("gave up after retries", sender=sender["address"], to=to, attempts=RETRIES + 1)
    return {"to": to, "code": 0, "error": "gave up after retries"}, conn


def _first_refusal(recipients: dict, to: str):
    code, text = recipients.get(to, (550, b"refused"))
    return code, _text(text)


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
