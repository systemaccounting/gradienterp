"""configure_smtp — the firm tells the gerp where its mail lives.

Writes one `SENDER#<address>` row, and only after a test message has actually gone out. A wrong
host, a blocked port or a From the provider will not accept then fails while the owner is sitting
there, rather than at 3am when a dunning notice does not go.

The password never passes through here as a value — it arrives by name. `collect_secret` opens a
secure field in the chat UI, the chat lambda writes it to SSM, and what reaches this tool is the
NAME. Same path a Stripe key takes.

The first sender written becomes the default, when no other row holds it. Otherwise a firm
configures `billing@theirshop.com`, sends, and the mail still goes out from somewhere else — a
surprise nobody reports as a bug, it just quietly looks unprofessional. A later sender does not
steal the flag; moving it is `make_default`.
"""

import json
import os
import time

from botocore.exceptions import ClientError

from aws import client, resource, log

TABLE = os.environ.get("SETTINGS_TABLE", "")
GERP_ID = os.environ.get("GERP_ID", "")
SECRET_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{os.environ.get('GERP_ID', '')}/secrets"
)
PREFIX = "SENDER#"
TLS_MODES = ("starttls", "ssl", "none")


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}


def table():
    return resource("dynamodb").Table(TABLE)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    op = (body.get("op") or "set").strip()
    address = (body.get("address") or "").strip()

    if not address or "@" not in address:
        return _err("address is required — the address this firm sends as, e.g. billing@theirshop.com")

    if op == "remove":
        table().delete_item(Key={"gerp_id": GERP_ID, "sk": PREFIX + address})
        return {"statusCode": 200, "body": json.dumps({"status": "removed", "address": address})}

    if op == "make_default":
        return _set_default(address)

    host = (body.get("host") or "").strip()
    secret_name = (body.get("secret_name") or "").strip()
    if not host:
        return _err("host is required — the firm's SMTP server, e.g. smtp.gmail.com")
    if not secret_name:
        return _err(
            "secret_name is required — collect the mail password with collect_secret first and "
            "pass the NAME it was stored under"
        )
    tls = (body.get("tls") or "starttls").strip()
    if tls not in TLS_MODES:
        return _err(f"tls must be one of {list(TLS_MODES)}, got {tls!r}")

    sender = {
        "address": address,
        "host": host,
        "port": int(body.get("port") or (465 if tls == "ssl" else 587)),
        "username": (body.get("username") or address).strip(),
        "tls": tls,
        "secret_name": secret_name,
    }

    # the test send IS the validation — nothing is written if the server will not take it
    try:
        password = client("ssm").get_parameter(
            Name=f"{SECRET_PREFIX}/{secret_name}", WithDecryption=True
        )["Parameter"]["Value"]
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] == "ParameterNotFound":
            return _err(f"no stored secret named {secret_name!r}: {e}", 404)
        log.error("secret read failed", secret_name=secret_name, address=address, error=str(e))
        return _err(f"could not read the stored secret {secret_name!r}: {e}", 502)

    test_to = (body.get("test_to") or address).strip()
    try:
        _test_send(sender, password, test_to)
    except Exception as e:
        log.info("smtp test send refused", address=address, host=host, test_to=test_to, error=str(e))
        return _err(
            f"the test message did not send, so nothing was saved: {e}. "
            f"Check the host, port, TLS mode, and that {sender['username']} may send as {address}.",
            502,
        )

    now = int(time.time())
    item = {"gerp_id": GERP_ID, "sk": PREFIX + address, **sender, "updated_at": now}
    del item["address"]                       # it is the key; storing it twice invites drift
    if not _any_default():
        item["default"] = True
    table().put_item(Item=item)

    return {"statusCode": 200, "body": json.dumps({
        "status": "configured", "address": address, "tested": test_to,
        "default": bool(item.get("default")),
    })}


def _test_send(sender: dict, password: str, to: str):
    import smtplib
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = sender["address"]
    m["To"] = to
    m["Subject"] = "mail is working"
    m.set_content(
        f"This is a test from your books.\n\n"
        f"Mail sent as {sender['address']} through {sender['host']} arrived, so notices, "
        f"invoices and anything else your agent sends will reach people from your own address."
    )
    if sender["tls"] == "ssl":
        conn = smtplib.SMTP_SSL(sender["host"], sender["port"], timeout=30)
    else:
        conn = smtplib.SMTP(sender["host"], sender["port"], timeout=30)
        if sender["tls"] != "none":
            conn.starttls()
    try:
        conn.login(sender["username"], password)
        conn.send_message(m)
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def _rows():
    from boto3.dynamodb.conditions import Key

    return table().query(
        KeyConditionExpression=Key("gerp_id").eq(GERP_ID) & Key("sk").begins_with(PREFIX),
    ).get("Items", [])


def _any_default() -> bool:
    return any(r.get("default") for r in _rows())


def _set_default(address: str):
    """Move the flag. Cleared on the others first, so a failure between the two leaves nothing
    claiming to be default rather than two things claiming it."""
    rows = _rows()
    if not any(r["sk"] == PREFIX + address for r in rows):
        return _err(f"{address} is not a configured sender", 404)
    now = int(time.time())
    for r in rows:
        if r.get("default") and r["sk"] != PREFIX + address:
            table().update_item(
                Key={"gerp_id": GERP_ID, "sk": r["sk"]},
                UpdateExpression="REMOVE #d SET updated_at = :n",
                ExpressionAttributeNames={"#d": "default"},
                ExpressionAttributeValues={":n": now},
            )
    table().update_item(
        Key={"gerp_id": GERP_ID, "sk": PREFIX + address},
        UpdateExpression="SET #d = :t, updated_at = :n",
        ExpressionAttributeNames={"#d": "default"},
        ExpressionAttributeValues={":t": True, ":n": now},
    )
    return {"statusCode": 200, "body": json.dumps({"status": "default", "address": address})}
