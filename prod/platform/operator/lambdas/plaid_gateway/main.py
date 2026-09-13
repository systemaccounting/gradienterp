"""plaid_gateway — the operator-account Plaid I/O boundary (see modules/accounting/AGENTS.md § bank-feed reconciliation).

Holds the shared Plaid `client_id`/`secret` (operator SSM) so the credential never sprawls
into customer accounts, and makes the outbound Plaid calls. Per-gerp `connect_bank`/`reconcile`
invoke this cross-account carrying only that gerp's `access_token`.

Ops (dispatched on `event["op"]`):
  create_link      — /link/token/create (Hosted Link) → {link_token, hosted_link_url}
  complete         — /link/token/get → exchange a finished session's public_token, record item→gerp
  exchange         — /item/public_token/exchange → {access_token, item_id}
  pull             — /transactions/sync (paged) → {added, modified, removed, next_cursor}
  verification_key — /webhook_verification_key/get → the JWK for the Node shim to verify ES256
  webhook_route    — item_id → gerp → cross-account invoke that gerp's reconcile (the verified poke)

The public webhook endpoint + ES256 verification live in the Node shim (`plaid_webhook`), which calls
verification_key + webhook_route here — Python can't verify ECDSA.
"""

import json
import os
import urllib.error
import urllib.request

from aws import client as _aws_client, resource as _aws_resource, log


PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox")
BASE = f"https://{PLAID_ENV}.plaid.com"
STACK_PREFIX = os.environ.get("STACK_PREFIX", "gerp")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_ssm = _aws_client("ssm")
_creds = None

# routing clients (item→gerp map + customers registry + cross-account invoke) — created lazily so
# importing the gateway for the Plaid ops (pull/create_link/exchange/complete) needs no routing env.
_routing = None


def _routing_clients():
    global _routing
    if _routing is None:
        ddb = _aws_resource("dynamodb")
        _routing = {
            "items": ddb.Table(os.environ["PLAID_ITEMS_TABLE"]),
            "customers": ddb.Table(os.environ["CUSTOMERS_TABLE"]),
            "lam": _aws_client("lambda"),
        }
    return _routing


def _creds_pair():
    """(client_id, secret) — env first (local dev), else operator SSM (SecureString)."""
    global _creds
    if _creds:
        return _creds
    cid, sec = os.environ.get("PLAID_CLIENT_ID"), os.environ.get("PLAID_SECRET")
    if not (cid and sec):
        prefix = os.environ["PLAID_SSM_PREFIX"]
        cid = _ssm.get_parameter(Name=f"{prefix}/client_id")["Parameter"]["Value"]
        sec = _ssm.get_parameter(Name=f"{prefix}/secret", WithDecryption=True)["Parameter"]["Value"]
    _creds = (cid, sec)
    return _creds


def _plaid(path, body):
    cid, sec = _creds_pair()
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps({"client_id": cid, "secret": sec, **body}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Plaid's error body carries error_code/error_message; it never echoes the secret.
        text = e.read().decode()[:300]
        if e.code < 500:   # INVALID_*, ITEM_LOGIN_REQUIRED — the owner's bank link, not this call
            log.info("plaid refused the call", path=path, status=e.code, response=text)
        else:
            log.error("plaid call failed", path=path, status=e.code, response=text)
        raise RuntimeError(f"plaid {path} -> {e.code} {text}")
    except OSError as e:   # URLError, a connect refusal, a timeout
        log.error("plaid unreachable", path=path, error=str(e))
        raise


def pull(access_token, cursor=None):
    """`/transactions/sync`, paged to completion. Returns the merged update set + the new cursor.
    The caller (the gerp's reconcile) persists `next_cursor` per Item and passes it back next time."""
    added, modified, removed, has_more = [], [], [], True
    while has_more:
        body = {"access_token": access_token}
        if cursor:
            body["cursor"] = cursor
        r = _plaid("/transactions/sync", body)
        added += r["added"]
        modified += r["modified"]
        removed += r["removed"]
        cursor, has_more = r["next_cursor"], r["has_more"]
    return {"added": added, "modified": modified, "removed": removed, "next_cursor": cursor}


def create_link(client_user_id, webhook=None, completion_redirect_uri=None):
    """`/link/token/create` for Hosted Link → the owner opens `hosted_link_url` and authenticates
    with their bank (credentials never touch us). Returns `{link_token, hosted_link_url}`; the caller
    stores `link_token` to look the session up later (via `complete`) and relays the URL to the owner."""
    body = {
        "client_name": "gradientERP",
        "country_codes": ["US"],
        "language": "en",
        "products": ["transactions"],
        "user": {"client_user_id": client_user_id},
        "transactions": {"days_requested": 90},
        "hosted_link": {},
    }
    webhook = webhook or os.environ.get("PLAID_WEBHOOK_URL")  # default to the operator webhook shim
    if webhook:
        body["webhook"] = webhook
    if completion_redirect_uri:
        body["hosted_link"]["completion_redirect_uri"] = completion_redirect_uri
    r = _plaid("/link/token/create", body)
    return {"link_token": r["link_token"], "hosted_link_url": r["hosted_link_url"], "expiration": r.get("expiration")}


def exchange(public_token):
    """`/item/public_token/exchange` → the durable `{access_token, item_id}`. The access_token is the
    gerp's — returned to the gerp's caller, which stores it in its own account. The operator brokers
    the call (it holds the app secret) but never persists the access_token."""
    r = _plaid("/item/public_token/exchange", {"public_token": public_token})
    return {"access_token": r["access_token"], "item_id": r["item_id"]}


def _record_item(item_id, gerp_id):
    """Map item_id → gerp so a later SYNC_UPDATES_AVAILABLE webhook (keyed by item_id) routes to the
    right gerp's reconcile. Written here at connect time — the one place item_id and gerp meet."""
    if item_id and gerp_id:
        _routing_clients()["items"].put_item(Item={"item_id": item_id, "gerp_id": gerp_id})


def complete(link_token, gerp_id=None):
    """Poll `/link/token/get` for a finished Hosted Link session; on success, exchange its public_token
    and record the item→gerp mapping. Returns `{status:"linked", access_token, item_id, institution}`
    or `{status:"pending"}`. Webhookless completion path — SESSION_FINISHED is the low-latency replacement."""
    r = _plaid("/link/token/get", {"link_token": link_token})
    for sess in r.get("link_sessions", []) or []:
        results = sess.get("results") or {}
        for add in results.get("item_add_results", []) or []:  # preferred (multi-Item)
            if add.get("public_token"):
                ex = exchange(add["public_token"])
                _record_item(ex["item_id"], gerp_id)
                return {"status": "linked", "institution": (add.get("institution") or {}).get("name"), **ex}
        legacy = (sess.get("on_success") or {}).get("public_token")  # legacy single-Item
        if legacy:
            ex = exchange(legacy)
            _record_item(ex["item_id"], gerp_id)
            return {"status": "linked", **ex}
    return {"status": "pending"}


def webhook_route(item_id):
    """Resolve item_id → gerp → aws_account_id and cross-account-invoke that gerp's reconcile (async).
    The webhook shim calls this after verifying Plaid's signature. Mirror of the cross-firm dispatcher."""
    rc = _routing_clients()
    item = rc["items"].get_item(Key={"item_id": item_id}).get("Item")
    if not item:
        return {"routed": False, "reason": "unknown item_id"}
    gerp_id = item["gerp_id"]
    cust = rc["customers"].get_item(Key={"gerp_id": gerp_id}).get("Item") or {}
    acct = cust.get("aws_account_id")
    if not acct:
        return {"routed": False, "reason": "unresolved gerp account"}
    fn = f"arn:aws:lambda:{REGION}:{acct}:function:{STACK_PREFIX}-accounting-{gerp_id.replace('_', '-')}-reconcile"
    rc["lam"].invoke(FunctionName=fn, InvocationType="Event", Payload=b"{}")
    return {"routed": True, "gerp_id": gerp_id}


def verification_key(kid):
    """`/webhook_verification_key/get` → the JWK (EC P-256 public key) for a webhook's `kid`. The Node
    webhook shim calls this to verify Plaid's ES256 signature; the shim never holds the Plaid secret."""
    r = _plaid("/webhook_verification_key/get", {"key_id": kid})
    return {"key": r["key"]}


def handler(event, context):
    """Cross-account entry point. `event = {op, ...}`; the per-gerp caller carries its access_token."""
    op = event.get("op")
    if op == "pull":
        return {"ok": True, **pull(event["access_token"], event.get("cursor"))}
    if op == "create_link":
        return {"ok": True, **create_link(event["client_user_id"], event.get("webhook"), event.get("completion_redirect_uri"))}
    if op == "exchange":
        return {"ok": True, **exchange(event["public_token"])}
    if op == "complete":
        return {"ok": True, **complete(event["link_token"], event.get("gerp_id"))}
    if op == "verification_key":
        return {"ok": True, **verification_key(event["kid"])}
    if op == "webhook_route":
        return {"ok": True, **webhook_route(event["item_id"])}
    return {"ok": False, "error": f"unknown op {op!r}"}
