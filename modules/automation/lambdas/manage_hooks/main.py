"""manage_hooks — a url outsiders may call, and the secret that admits them.

`op: publish` mints both halves in one act: a route record naming an approved script, and a bearer
secret the record names by NAME — 256 bits from the OS, base64url — stored at the firm's automation
env path. The value is returned ONCE, to hand to the caller, the way a firm hands a vendor an API
key. Publishing again for the same `caller` rotates: a new secret, the same record.

`op: unpublish` deletes the record and its secret. The url stops answering at once. Nothing here
can read a secret back.
"""

import json
import os
import re
import secrets

from aws import client as _client
from aws import log
from botocore.exceptions import ClientError

from _helpers import err, ok

BUCKET = os.environ["CABINET_BUCKET"]
ROUTES_PREFIX = os.environ.get("ROUTES_PREFIX", "automations/routes/")
APPROVED_MODULES = "automations/approved/modules/"
ENV_PATH = os.environ.get("AUTOMATION_ENV_PATH", "")
HOOKS_BASE_URL = os.environ.get("HOOKS_BASE_URL", "")


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    if op == "publish":
        return _publish(body)
    if op == "unpublish":
        return _unpublish(body)
    return err("op is required: publish or unpublish")


def _publish(body):
    path = (body.get("path") or "").strip().strip("/")
    script = (body.get("script") or "").strip()
    caller = (body.get("caller") or "").strip()
    if not path or not re.fullmatch(r"[a-z0-9][a-z0-9_/-]*", path):
        return err("path is required: lowercase, digits, _ - and /, e.g. 'customers/upsert'")
    if not script:
        return err("script is required — an approved script, e.g. 'upsert_customer_contact.py'")
    if not caller or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", caller):
        return err("caller is required: who this hook is for, lowercase, e.g. 'bff' or 'acme'")

    s3 = _client("s3")
    try:
        s3.head_object(Bucket=BUCKET, Key=APPROVED_MODULES + script)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("404", "403", "NoSuchKey", "AccessDenied"):
            return err(f"{script} is not an approved script — approve it first", 404)
        log.error("approved script head failed", script=script, path=path, error=str(e))
        return err(f"could not read {script}: {e}", 502)

    name = "HOOK_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", caller.upper())
    token = secrets.token_urlsafe(32)
    _client("ssm").put_parameter(Name=f"{ENV_PATH}/{name}", Value=token, Type="SecureString",
                                 Overwrite=True)
    record = {"key": script, "args": body.get("params") or {}, "caller": {"bearer": name}}
    s3.put_object(Bucket=BUCKET, Key=f"{ROUTES_PREFIX}{path}.json", Body=json.dumps(record).encode(),
                  ContentType="application/json")
    url = f"{HOOKS_BASE_URL.rstrip('/')}/hooks/{path}" if HOOKS_BASE_URL else f"/hooks/{path}"
    return ok({"path": path, "url": url, "script": script, "caller": caller, "secret_name": name,
               "token": token,
               "note": "the token is shown once — hand it to the caller; publishing again for "
                       "this caller rotates it"})


def _unpublish(body):
    path = (body.get("path") or "").strip().strip("/")
    if not path:
        return err("path is required, e.g. 'customers/upsert'")
    s3 = _client("s3")
    key = f"{ROUTES_PREFIX}{path}.json"
    try:
        record = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except Exception as e:  # noqa: BLE001
        if isinstance(e, ClientError) and e.response["Error"]["Code"] == "NoSuchKey":
            return err(f"no hook at {path}", 404)
        log.error("route record unreadable", path=path, key=key, error=str(e))
        return err(f"could not read the hook at {path}: {e}", 502)
    s3.delete_object(Bucket=BUCKET, Key=key)
    name = (record.get("caller") or {}).get("bearer")
    removed = []
    if name:
        try:
            _client("ssm").delete_parameter(Name=f"{ENV_PATH}/{name}")
            removed.append(name)
        except Exception as e:  # noqa: BLE001 — already gone is gone
            if not (isinstance(e, ClientError) and e.response["Error"]["Code"] == "ParameterNotFound"):
                log.warning("hook secret not deleted", path=path, secret_name=name, error=str(e))
    return ok({"path": path, "unpublished": True, "secrets_removed": removed})
