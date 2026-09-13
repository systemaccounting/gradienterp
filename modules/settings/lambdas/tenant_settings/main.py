"""tenant_settings — a gerp's settings, in a per-gerp config DDB table.

One low-volume **config table** `gerp-settings-<gerp_id>` (`pk = gerp_id`, `sk`), heterogeneous items:
  - `GERP#<key>`         — instance-wide settings; the value lives under attribute `value`.
    `GERP#openly_operated` (bool) gates publication — the agent + accounting/treasury/schemas
    read it at cold start, so a flip propagates at the readers' next cold start.
  - `USER#<account_id>`  — per-user settings; `notification_email` is where that user's "email me"
    lands. Verify status is derived live from SES (not stored — SES is source of truth, the confirm
    link is clicked out-of-band, a cached flag would go stale).
  - `GERP#timezone`      — the business's IANA zone (`America/Los_Angeles`). Every civil boundary
    (which month a sale lands in, which week a pay period covers) resolves against it; see
    `modules/clock`. Validated on write so a typo fails here rather than at period close.
  - `INSTRUCTION#<ms>#<hash>` — the firm's standing instructions, one single-line directive per row
    under attribute `text`. Written here from the gerp screen and by the agent's `instruct` tool;
    read by the agent container every turn as a system-prompt section. The ms in the sk gives the
    list a stable oldest-first order; the hash keeps two same-millisecond writes apart.

Owner-authed via the gerp API's JWT authorizer; the caller's `account_id` is the JWT `sub`
(the gerp-website BFF forwards the caller's bearer token, which the authorizer validates).
Provisioning metadata (`business_name`/`owner_email`) stays in the SSM tenant blob — this lambda
reads `owner_email` only to report the agent mailbox's verify status.

GET  /settings → {openly_operated, notification_email, notification_email_verified, agent_email,
                  instructions: [{id, text}], ...}
PUT  /settings   {openly_operated?: bool, notification_email?: str, timezone?: str,
                  instruction?: str, remove_instruction?: str} → the updated view
"""

import hashlib
import json
import logging
import os
import time

log = logging.getLogger()
log.setLevel(logging.INFO)

from botocore.exceptions import ClientError

from aws import client as _aws, table as _ddb_table, log as alog, refuse_non_owner
from events import publish

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
TENANT_PARAM = os.environ.get("TENANT_PARAM", f"/gradienterp/customers/{CUSTOMER_ID}")
AGENT_EMAIL_PARENT_DOMAIN = os.environ.get("AGENT_EMAIL_PARENT_DOMAIN", "")  # empty ⇒ email front door off

GERP_OPENLY_OPERATED = "GERP#openly_operated"
GERP_TIMEZONE_KEY = "GERP#timezone"

# ─── config-table access (GERP#<key> + USER#<account_id>) ───


# Resolved at call time so a harness can point at a scratch table between cases.
def _table():
    return _ddb_table(
        os.environ.get("SETTINGS_TABLE", f"gerp-settings-{CUSTOMER_ID.replace('_', '-')}"))


def _get_item(sk: str) -> dict:
    return _table().get_item(Key={"gerp_id": CUSTOMER_ID, "sk": sk}).get("Item") or {}


def _put_attrs(sk: str, attrs: dict) -> None:
    """Upsert the named attributes onto the `sk` row (creates the row if absent)."""
    _table().update_item(
        Key={"gerp_id": CUSTOMER_ID, "sk": sk},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in attrs),
        ExpressionAttributeNames={f"#{k}": k for k in attrs},
        ExpressionAttributeValues={f":{k}": v for k, v in attrs.items()},
    )


def _announce_published(val: bool) -> bool:
    """The flag reaches the operator: `gerp.published` / `gerp.unpublished` on the shared bus, so the
    directory, the read api and the publisher learn it without asking this gerp per request. The row
    is written first; a bus that is not wired (local) or refuses is logged, and the write stands —
    the operator row stays stale until the next flip, so the response says it was not announced."""
    try:
        publish("settings", "gerp.published" if val else "gerp.unpublished",
                {"gerp_id": CUSTOMER_ID, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return True
    except Exception as e:  # noqa: BLE001 — a notice that did not go out must not fail the setting
        alog.error("publish flip not announced", event="gerp.published" if val else "gerp.unpublished", error=str(e))
        return False


def _openly_operated() -> bool:
    return bool(_get_item(GERP_OPENLY_OPERATED).get("value", False))


# ─── the business's clock (GERP#timezone) ───
#
# An IANA name — "America/Los_Angeles", not "Pacific". Only IANA carries the daylight-saving table,
# and every civil boundary the books depend on (which month a sale falls in, which week a pay period
# covers) is resolved against it by modules/clock.
#
# Validated HERE, on write. `ZoneInfo(<bad name>)` raises at call time, so an unvalidated typo would
# surface at period close rather than at the moment someone typed it.

def _timezone() -> str:
    return _get_item(GERP_TIMEZONE_KEY).get("value", "") or "UTC"


def _valid_zone(name: str) -> bool:
    if name == "UTC":
        return True
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 — unknown zone, or no tz database
        return False


# ─── standing instructions (INSTRUCTION#<ms>#<hash>) ───
#
# The firm's own additions to how its agent behaves — "schedule the highest performers on rush
# shifts", "always quote in gross". They are UNCONDITIONAL: every instruction is loaded into the
# system prompt on every turn, with no retrieval step deciding which ones apply. That is the whole
# reason this is a prefix Query and not a semantic search — an instruction that a retriever misses
# on a given turn is an instruction the owner silently doesn't have.
#
# Caps exist so the block stays a list of directives and not a second persona doc: the size of this
# section is a cost every turn pays.

INSTRUCTION_PREFIX = "INSTRUCTION#"
INSTRUCTION_MAX = 64      # rows — past this, an instruction is being used as a document
INSTRUCTION_TEXT_MAX = 300  # chars — one line, not a paragraph


def _query_prefix(prefix: str) -> list:
    """Every row under an sk prefix, in ONE call."""
    from boto3.dynamodb.conditions import Key
    return _table().query(
        KeyConditionExpression=Key("gerp_id").eq(CUSTOMER_ID) & Key("sk").begins_with(prefix)
    ).get("Items", [])


def list_instructions() -> list:
    """[{id, text}] oldest first — the sk's leading ms is the order, so new ones land at the bottom."""
    rows = sorted(_query_prefix(INSTRUCTION_PREFIX), key=lambda r: r.get("sk", ""))
    return [{"id": r["sk"][len(INSTRUCTION_PREFIX):], "text": r.get("text", "")} for r in rows]


def _next_ms(ids) -> int:
    """The sk's leading millisecond, forced monotonic. Two adds inside the same millisecond would
    otherwise be separated only by their hash — i.e. ordered arbitrarily — and this list's order is
    the order the owner typed it in."""
    last = max((int(h) for h in (str(i).split("#")[0] for i in ids) if h.isdigit()), default=0)
    return max(int(time.time() * 1000), last + 1)


def write_instruction(text: str) -> None:
    """Add one. Identical text is a no-op — the agent may write the same instruction the owner
    already typed, and a duplicate would be billed to every turn forever. Raises ValueError on a
    spec the caller should fix."""
    text = " ".join((text or "").split())  # a directive is one line; collapse any pasted newlines
    if not text:
        raise ValueError("instruction is empty")
    if len(text) > INSTRUCTION_TEXT_MAX:
        raise ValueError(f"instruction is {len(text)} chars, cap is {INSTRUCTION_TEXT_MAX}")
    existing = list_instructions()
    if any(i["text"] == text for i in existing):
        return
    if len(existing) >= INSTRUCTION_MAX:
        raise ValueError(f"{INSTRUCTION_MAX} instructions is the cap — remove one first")
    sk = f"{INSTRUCTION_PREFIX}{_next_ms(i['id'] for i in existing)}#{hashlib.sha1(text.encode()).hexdigest()[:8]}"
    item = {"gerp_id": CUSTOMER_ID, "sk": sk, "text": text}
    _table().put_item(Item=item)


def remove_instruction(instruction_id: str) -> None:
    """Delete by the id `list_instructions` returned (the sk minus its prefix)."""
    sk = f"{INSTRUCTION_PREFIX}{(instruction_id or '').strip()}"
    if sk == INSTRUCTION_PREFIX:
        raise ValueError("no instruction id given")
    _table().delete_item(Key={"gerp_id": CUSTOMER_ID, "sk": sk})


from _locations import list_locations as _list_locations, write_location as _write_location  # noqa: E402 — bundled at zip root


# ─── SES verify status (live — same pattern for the notification address + the agent mailbox) ───

def _ses_verified(address: str) -> bool:
    if not address:
        return False
    try:
        attrs = _aws("ses").get_identity_verification_attributes(Identities=[address])["VerificationAttributes"]
        return attrs.get(address, {}).get("VerificationStatus") == "Success"
    except Exception:  # noqa: BLE001
        alog.exception("ses verification check failed", address=address)
        raise


def _verify(address: str) -> bool:
    """Fire SES verify-email-identity for a newly set/changed address (sandbox: sends bounce until
    the owner clicks the confirm link). Idempotent — re-verifying an already-verified address no-ops.
    False when SES refused: verification only fires on an address change, so the caller says so."""
    if address:
        try:
            _aws("ses").verify_email_identity(EmailAddress=address)
        except Exception:  # noqa: BLE001
            alog.exception("ses verify-email-identity failed", address=address)
            return False
    return True


def _agent_email_status() -> dict:
    """The gerp's agent mailbox + whether the owner's reply identity is verified (sandbox).
    owner_email is provisioning metadata in the SSM blob. Off (no parent domain) ⇒ empty/unverified."""
    if not AGENT_EMAIL_PARENT_DOMAIN:
        return {"agent_email": "", "agent_email_verified": False, "verify_recipient": ""}
    subdomain = f"{CUSTOMER_ID.replace('_', '-')}.{AGENT_EMAIL_PARENT_DOMAIN}"
    owner_email = ""
    try:
        blob = json.loads(_aws("ssm").get_parameter(Name=TENANT_PARAM)["Parameter"]["Value"])
        owner_email = blob.get("owner_email", "")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            alog.exception("tenant blob read failed", param=TENANT_PARAM)
            raise
        alog.info("no tenant blob; agent mailbox reported unverified", param=TENANT_PARAM)
    except Exception:  # noqa: BLE001
        alog.exception("tenant blob read failed", param=TENANT_PARAM)
        raise
    return {
        "agent_email": f"agent@{subdomain}",
        "agent_email_verified": _ses_verified(owner_email),
        "verify_recipient": owner_email,
    }


def _view(account_id: str) -> dict:
    """The full settings view: the GERP-scoped flag + the caller's per-user row + agent-email status."""
    notification_email = _get_item(f"USER#{account_id}").get("notification_email", "")
    return {
        "openly_operated": _openly_operated(),
        "notification_email": notification_email,
        "notification_email_verified": _ses_verified(notification_email),
        "locations": _list_locations(),
        "instructions": list_instructions(),
        "timezone": _timezone(),
        **_agent_email_status(),
    }


def _ok(obj):
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps(obj)}


def _caller(event) -> str:
    """The caller's account_id (JWT sub), forwarded by the BFF + validated by the gerp authorizer.
    Empty in AWS_IAM/standalone mode — per-user reads/writes fall back to a `local` row."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return claims.get("sub") or ""


def handler(event, context):
    refused = refuse_non_owner(event)
    if refused:
        return refused
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    account_id = _caller(event)

    if method == "GET":
        try:
            return _ok(_view(account_id))
        except Exception as e:  # noqa: BLE001 — logged where it raised
            return {"statusCode": 502, "body": json.dumps({"error": f"settings read failed: {type(e).__name__}"})}

    if method in ("PUT", "POST"):
        body = json.loads(event.get("body") or "{}") if isinstance(event.get("body"), str) else (event.get("body") or {})
        notes = {}  # what the write could not do — the row stands, the response says so
        try:
            if "openly_operated" in body:
                val = bool(body["openly_operated"])
                _put_attrs(GERP_OPENLY_OPERATED, {"value": val})
                log.info("openly_operated set to %s for %s", val, CUSTOMER_ID)
                if not _announce_published(val):
                    notes["announced"] = False
            if "location" in body:  # add (no ordinal) or rewrite (explicit ordinal keeps it)
                try:
                    _write_location(body["location"] or {})
                except ValueError as ve:
                    return {"statusCode": 400, "body": json.dumps({"error": str(ve)})}
            if "timezone" in body:
                tz = (body.get("timezone") or "").strip()
                if not _valid_zone(tz):
                    return {"statusCode": 400, "body": json.dumps(
                        {"error": f"'{tz}' is not an IANA timezone name (e.g. America/Los_Angeles)"})}
                _put_attrs(GERP_TIMEZONE_KEY, {"value": tz})
                log.info("timezone set to %s for %s", tz, CUSTOMER_ID)
            if "instruction" in body:
                try:
                    write_instruction(body["instruction"])
                except ValueError as ve:
                    return {"statusCode": 400, "body": json.dumps({"error": str(ve)})}
            if "remove_instruction" in body:
                try:
                    remove_instruction(body["remove_instruction"])
                except ValueError as ve:
                    return {"statusCode": 400, "body": json.dumps({"error": str(ve)})}
            if "notification_email" in body and account_id:
                new_addr = (body.get("notification_email") or "").strip()
                prior = _get_item(f"USER#{account_id}").get("notification_email", "")
                _put_attrs(f"USER#{account_id}", {"notification_email": new_addr})
                if new_addr and new_addr != prior:
                    # re-verify on change (sandbox); status surfaces on the next GET
                    if not _verify(new_addr):
                        notes["verification_started"] = False
                log.info("notification_email updated for %s / %s", CUSTOMER_ID, account_id)
        except Exception as e:  # noqa: BLE001
            alog.exception("tenant_settings write failed", account_id=account_id)
            return {"statusCode": 502, "body": json.dumps({"error": f"write failed: {type(e).__name__}"})}
        try:
            return _ok({**_view(account_id), **notes})
        except Exception as e:  # noqa: BLE001 — logged where it raised
            return {"statusCode": 502, "body": json.dumps({"error": f"settings read failed: {type(e).__name__}"})}

    return {"statusCode": 405, "body": json.dumps({"error": "method not allowed"})}
