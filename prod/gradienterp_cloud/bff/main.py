"""gerp website BFF — plain Lambda handler (operator account; single front door).

gradienterp.cloud's backend-for-frontend. One front door for all owners; it hides
the per-customer gateway topology and is the **authorization gate**: an account
owns N gerp instances, and this handler enforces that a request only touches a gerp
the caller actually owns.

Auth split:
  - **authn** is done by the API Gateway JWT authorizer (operator Cognito pool) in
    front of this lambda — the validated claims arrive at
    `requestContext.authorizer.jwt.claims`. We never validate the token ourselves.
  - **authz** is here: `claims.sub` → the account's owned gerps (gerp-customers) →
    the requested `gerp_id` must be in that set, or 403.

Routes:
  GET  /api/gerps           → list the account's owned gerps (the switcher)
  GET/POST /api/gerp-info   → the gerp's business info: label, legal and public profiles
  GET  / (and unknown)      → serve the SPA (web/index.html)

Forwarding (scaffold): we forward the caller's `Authorization: Bearer <id_token>`
to the gerp gateway, whose own JWT authorizer validates it. The BFF's ownership
check is the cross-tenant guard on this path. HARDENING (TODO): lock the per-customer
routes to the BFF's IAM principal so they can't be hit browser-direct with any pool
token — see AGENTS.md.

Repo-standard plain handler; its AWS clients come from `modules/aws` like every other lambda.
The one thing that still keys off being in Lambda is the `x-debug-sub` header (see `_sub`) — an
identity-trust decision, not an endpoint one.
"""

import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger()
log.setLevel(logging.INFO)

CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
MEMBERS_TABLE = os.environ.get("MEMBERS_TABLE", "gerp-members")  # operator account↔gerp membership (hash account_id, range gerp_id, attr role)
# the vends queue (tower-vends, same operator account): a card landing sends the provisioning
# payload here and tower's provisioner consumes it four at a time — Control Tower runs five
# account operations at once. Empty = no vend: the row is stubbed active (the local stack)
PROVISION_QUEUE = os.environ.get("PROVISION_QUEUE", "")
# a changed login reaches the Identity Center user and each owned gerp's tenant blob (tower, same
# account). Empty = the row is the only copy (the local stack)
OWNER_EMAIL_FN = os.environ.get("OWNER_EMAIL_FN", "")
# tower's update_business_info: an edited business profile reaches the gerp's tenant blob
BUSINESS_INFO_FN = os.environ.get("BUSINESS_INFO_FN", "")
# the card-saving pair — these two live in the SELLER's account, not this one
SETUP_LINK_FN = os.environ.get("SETUP_LINK_FN", "")
SAVE_CARD_FN = os.environ.get("SAVE_CARD_FN", "")
CARD_METHODS_FN = os.environ.get("CARD_METHODS_FN", "")
# the seller's off-session charge — Pay now on a hosting invoice the monthly charge missed
CHARGE_FN = os.environ.get("CHARGE_FN", "")
CLOSES_AFTER_DAYS = 15   # the chase's deadline: closure/begin.py runs this many days after unpaid_at
PROFILES_TABLE = os.environ.get("PROFILES_TABLE", "gerp-profiles")  # operator-account hub profile registry (pk gerp_profile_id)
# where a gerp can be built (config.json REGIONS, served to the create screen): id → {label,
# profile, status, countries}. `status` offered = built end to end; not yet = grayed with a note
REGIONS = json.loads(os.environ.get("REGIONS") or "{}")
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNTS_TABLE = os.environ.get("ACCOUNTS_TABLE", "gerp-accounts")  # operator-account private account profiles (key=account_id=sub)
# Every vend is an AWS account and a full stack, and it runs before any invoice does: one account holds
# this many live gerps unless its account row carries a higher `gerp_limit` (the operator sets it).
GERP_LIMIT = int(os.environ.get("GERP_LIMIT", "3"))
LIVE_STATUSES = ("queued", "provisioning", "active", "stopped")
# what a closed account left behind: one row per identifier (card#<fingerprint> / email#<hash> /
# phone#<hash>), written when an account is deleted, read at create-gerp and at provisioning
PRIORS_TABLE = os.environ.get("PRIORS_TABLE", "gerp-priors")
# the pool the account's Cognito user is deleted from. Empty = no user to delete (the local stack)
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
AGENT_EMAIL_PARENT_DOMAIN = os.environ.get("AGENT_EMAIL_PARENT_DOMAIN", "")  # gerp-config derives agent_email = agent@<gerp_id>.<parent>
# per-customer resources are `<prefix>-<module>-<gerp_id>-<name>`; the prefix is config-driven
# (`STACK_PREFIX` in config.json), so an arn into a customer account is derivable, not configured.
STACK_PREFIX = os.environ.get("STACK_PREFIX", "gerp")
# The CodeBuild project that closes a gerp — export the firm's records, then destroy
# prod/per_customer (.codebuild/per-customer.yml, TF_ACTION=destroy). EMPTY = the closure is
# recorded and nothing is torn down, which is how this ships until the path has been run against a
# real gerp. Same shape as PROVISION_QUEUE: the off switch is the absence of a name.
# A requested closure is handed to the seller gerp's closure scripts, which run the same sequence an
# unpaid invoice ends in: export, destroy, fifteen days of notices, close the account. Empty records
# the request on the row and hands nothing on — the same off posture the build switch had.
CLOSURE_BEGIN_FN = os.environ.get("CLOSURE_BEGIN_FN", "")
# What the owner types to confirm a closure. One string, defined once, so the dialog and the check
# cannot drift into disagreeing about what counts as confirmation.
CLOSE_PHRASE = "I understand"
# What the account types to delete itself. Required on the request, the same way as the close.
DELETE_PHRASE = "delete my account"
# With no provisioner configured, this is how long the stand-in "takes" before reporting done, and
# the host it stamps as the gateway. `.invalid` is reserved and can never resolve, so a stub gerp
# is inert rather than pointed at something real.
STUB_PROVISION_SECONDS = float(os.environ.get("STUB_PROVISION_SECONDS", "5"))
STUB_GATEWAY_URL = "https://provisioning-disabled.invalid"
WEB_DIR = Path(os.environ.get("WEB_DIR", str(Path(__file__).resolve().parent.parent / "web")))

# public-profile fields (gerp-profiles). 'verified' is owned by the future verification
# flow, not the owner — never written on an owner save (so a verified profile survives edits).
PUBLIC_FIELDS = ["first", "middle", "last", "street", "unit", "city", "state", "zip", "country", "email", "phone"]

REGION = os.environ.get("AWS_REGION", "us-east-1")

from aws import IN_LAMBDA, client as _aws  # noqa: E402

# geo-places (address autocomplete + geocode): the BFF signs with its own IAM role (SigV4), no
# browser API key. There is no local stand-in for it, so it always talks to the real service —
# a read-only lookup, nothing of the caller's is written anywhere by it.
import boto3  # noqa: E402

geo = boto3.client("geo-places", region_name=REGION)


# ── identity + ownership ────────────────────────────────────────────────────

def _sub(event) -> str | None:
    """The Cognito sub. In prod the ONLY source is the APIGW JWT authorizer's
    validated claims. `x-debug-sub` is a LOCAL-dev affordance (serve.py / curl) and
    is NEVER trusted under Lambda — otherwise a caller reaching a non-JWT-routed
    /api path via the public $default could spoof identity with a header."""
    claims = (event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})) or {}
    sub = claims.get("sub")
    if sub:
        return sub
    if not IN_LAMBDA:  # local only — prod trusts validated claims and nothing else
        return {k.lower(): v for k, v in (event.get("headers") or {}).items()}.get("x-debug-sub")
    return None


def _member_gerps(sub: str) -> list[dict]:
    """Gerps this account is a MEMBER of (owner | employee | …): Query gerp-members by
    account_id, then join gerp-customers for the instance details (gateway_url / chat_url /
    label). The member row is the one read of ownership; role=owner is what owning means."""
    ddb = _aws("dynamodb")
    resp = ddb.query(
        TableName=MEMBERS_TABLE,
        KeyConditionExpression="account_id = :a",
        ExpressionAttributeValues={":a": {"S": sub}},
    )
    out = []
    for m in resp.get("Items", []):
        gid = m["gerp_id"]["S"]
        c = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gid}}).get("Item", {})
        out.append({
            "gerp_id":     gid,
            "role":        m.get("role", {}).get("S", "member"),
            "gateway_url": c.get("gateway_url", {}).get("S", ""),
            "chat_url":    c.get("chat_url", {}).get("S", ""),
            "label":       c.get("label", {}).get("S", gid),
            "status":      c.get("status", {}).get("S", ""),
            "queued_at":   c.get("queued_at", {}).get("S", ""),
            # a closed gerp's download window, stamped by the closure build before its destroy
            "download_until": c.get("download_until", {}).get("S", ""),
            # where the gerp's own lambdas live. Never sent to the browser — it is here so
            # `_invoke_gerp` can derive an arn without a second read.
            "aws_account_id": c.get("aws_account_id", {}).get("S", ""),
            # an ending the card behind this gerp carried (a purchase held on a balance owed)
            "prior": _prior_of_row(c),
            # why a paid-for gerp is not vending: `limit` (the account holds its live gerps) or
            # `capacity` (the organization has no account left)
            "held": c.get("held", {}).get("S", ""),
            # the seller's receivable state, kept by tower: the hosting invoices still open
            "billing": _billing_of_row(c),
            # how a closed gerp ended and what it left owed — tower's close_account stamps these
            "closed_how": c.get("closed_how", {}).get("S", ""),
            "closed_at": c.get("closed_at", {}).get("S", ""),
            "balance_owed": float(c.get("balance_owed", {}).get("N", "0")),
        })
    return out


# ── create a gerp (account-level capability action) ─────────────────────────

def _email(event) -> str:
    claims = (event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})) or {}
    return claims.get("email", "")


# The longest a gerp_id may be. Every resource in the gerp's account is named
# `gerp-<module>-<gerp_id>-<thing>` and Lambda caps a function name at 64; the longest function
# by convention (`gerp-invoicing-<gerp_id>-record_invoice_paid`) leaves room for 26.
# `tests/tower/local/test_resource_names.py` holds this against every function, bucket and
# queue name in the repo, so a new long name fails there before a vend fails on it.
GERP_ID_MAX = 28
_GERP_ID_SUFFIX = 6   # hex characters: 16.7 million ids per slug


def _new_gerp_id(label: str) -> str:
    """A fresh, resource-safe gerp_id per gerp (an account owns many). Slug of the label + a
    short random suffix — used as a resource-name suffix + SSM path key, so lowercase [a-z0-9-]
    only, uniqueness from the suffix, the whole at most GERP_ID_MAX."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:GERP_ID_MAX - _GERP_ID_SUFFIX - 1].rstrip("-") or "gerp"
    return f"{slug}-{uuid.uuid4().hex[:_GERP_ID_SUFFIX]}"


_lambda_clients: dict = {}


def _lambda_in(region: str):
    """A Lambda client for a gerp's region (a function is invoked through its own region's
    endpoint); the shared client for this one."""
    if not region or region == REGION:
        return _aws("lambda")
    if region not in _lambda_clients:
        _lambda_clients[region] = boto3.client("lambda", region_name=region)
    return _lambda_clients[region]


def _invoke_gerp(gerp: dict, suffix: str, payload: dict, wait: bool = True, module: str = "export") -> dict:
    """Call a lambda in a CUSTOMER's own account, by convention rather than configuration.

    Every per-customer function is `<prefix>-<module>-<gerp_id>-<name>` in that gerp's account, and
    the row already carries `aws_account_id` — so the arn is derivable and there is no per-gerp
    config to keep in step with provisioning. `module` names which module's function: export's
    (the default, the door that outlives closure) or mcp's (the consent landing).

    Unlike `_forward`, this does not go through the gerp's HTTP gateway. That gateway is torn down
    at closure and the export lambda deliberately is not, so a call that has to keep working after
    the instance is gone cannot route through it.

    `wait=False` invokes asynchronously and returns immediately. API Gateway gives this handler ~30s
    and an export of a real firm takes minutes, so anything that MAKES data is fired and polled for;
    only reads and credential issuance answer inline.
    """
    account = gerp.get("aws_account_id") or ""
    if not account:
        return {"error": f"gerp {gerp.get('gerp_id')} has no aws_account_id on its row"}
    region = gerp.get("region") or REGION   # the gerp's region is on its row; own region for a row without one
    fn = (f"arn:aws:lambda:{region}:{account}:function:"
          f"{STACK_PREFIX}-{module}-{gerp['gerp_id'].replace('_', '-')}-{suffix}")
    resp = _lambda_in(region).invoke(
        FunctionName=fn,
        InvocationType="RequestResponse" if wait else "Event",
        Payload=json.dumps(payload).encode(),
    )
    if not wait:
        return {"started": 200 <= resp["StatusCode"] < 300}
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        return {"error": raw[:400].decode(errors="replace")}
    result = json.loads(raw or b"{}")
    body = result.get("body")
    out = json.loads(body) if isinstance(body, str) else (body or result)
    if isinstance(out, dict) and isinstance(result.get("statusCode"), int):
        out["_status"] = result["statusCode"]
    return out


def _invoke_seller(fn: str, payload: dict) -> dict:
    """Call a lambda in the seller gerp's account and return its parsed body.

    Cross-account by resource policy, not assume-role: the callee names this role, so there is
    no session to create and the grant is readable from the side that grants it.

    `fn` must be a full ARN. boto3 resolves an unqualified function name against the CALLER's
    account, so a bare name silently looks for these in operator and fails on an arn that was
    never going to exist.
    """
    if not fn:
        return {"error": "seller function not configured"}
    resp = _aws("lambda").invoke(
        FunctionName=fn, InvocationType="RequestResponse", Payload=json.dumps(payload).encode()
    )
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        return {"error": raw[:400].decode(errors="replace")}
    result = json.loads(raw or b"{}")
    body = result.get("body")
    return json.loads(body) if isinstance(body, str) else (body or result)


def _invoke_seller_status(fn: str, payload: dict) -> tuple[int, dict]:
    """`_invoke_seller`, keeping the callee's status code.

    The callee already says whether it refused and why — a 409 for a card another gerp is billed
    to, a 404 for one this contact cannot charge — and that is worth passing through rather than
    reconstructing from the message text here.
    """
    if not fn:
        return 502, {"error": "seller function not configured"}
    resp = _aws("lambda").invoke(
        FunctionName=fn, InvocationType="RequestResponse", Payload=json.dumps(payload).encode()
    )
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        return 502, {"error": raw[:400].decode(errors="replace")}
    result = json.loads(raw or b"{}")
    body = result.get("body")
    return int(result.get("statusCode", 200)), (
        json.loads(body) if isinstance(body, str) else (body or result))


def _regions_out() -> list:
    return [{"id": rid, "label": r.get("label", rid), "status": r.get("status", "not yet")}
            for rid, r in REGIONS.items()]


def _region_for(country: str) -> str:
    """The offered region nearest a country, by the REGIONS map's `countries` lists (ISO2 codes and
    the names Places returns); this one when nothing matches."""
    c = (country or "").strip()
    for rid, r in REGIONS.items():
        if r.get("status") == "offered" and c and c.upper() in [x.upper() for x in r.get("countries", [])]:
            return rid
    return DEFAULT_REGION


def _create_gerp(sub: str, gerp_id: str, label: str, email: str, openly_operated: bool = False,
                 terms_version: str = "", legal: dict = None, public: dict = None, region: str = "") -> None:
    """Record ownership, then kick off provisioning. Account-level: it acts for the
    authed account (no gerp-ownership check — there's no gerp yet). Writes the
    gerp-customers row (owner_sub → this gerp; gateway_url filled later, when
    provisioning completes — see AGENTS.md).

    **It does NOT provision.** Payment info is required to spin up a gerp, and the card is saved
    on a hosted Stripe page the browser is redirected to — so the row is written first, at
    `awaiting_payment`, and `_provision_gerp` runs once the card lands. The provisioning payload
    is stashed on the row because by then this request is long gone."""
    ddb = _aws("dynamodb")
    ddb.put_item(
        TableName=CUSTOMERS_TABLE,
        Item={
            "gerp_id": {"S": gerp_id},
            # who created it. Ownership is the gerp-members row; this rides to the tower, which
            # stashes it in the gerp's own SSM for the chat lambda's owner check.
            "owner_sub":   {"S": sub},
            "label":       {"S": label},
            "owner_email": {"S": email},
            "status":      {"S": "awaiting_payment"},
            # WHAT was agreed and when. A row saying "accepted" without saying accepted what
            # cannot answer the only question anyone ever asks of it, and the checkbox is
            # browser-side — this row is the acceptance, not the tick.
            "terms_version":     {"S": terms_version},
            "terms_accepted_at": {"N": str(int(time.time() * 1000))},
            # stashed for _provision_gerp — the request that knows them ends at the redirect
            "openly_operated": {"BOOL": openly_operated},
            # the legal business profile (private) and the public one (the profile row's seed)
            "legal":  _attr(legal or {}),
            "public": _attr(public or {}),
            # where it is built: the create screen's choice, the address's country's region by
            # default. Every arn built for the gerp reads it off the row from here on
            "region": {"S": region or DEFAULT_REGION},
        },
        ConditionExpression="attribute_not_exists(gerp_id)",
    )
    # owner membership — owner is a member with role=owner (the account↔gerp spine the BFF
    # reads for /api/gerps). gerp-customers stays the gerp's instance record.
    ddb.put_item(
        TableName=MEMBERS_TABLE,
        Item={"account_id": {"S": sub}, "gerp_id": {"S": gerp_id}, "role": {"S": "owner"}},
    )


def _provision_gerp(gerp_id: str) -> bool:
    """Queue the vend, once the card is on file. Reads what it needs off the row.

    Guarded on `awaiting_payment` so a second save-card — a refresh of the return page, a
    re-submitted session — cannot vend a second account for the same gerp. Control Tower takes
    ~15 minutes and there is no undo, so the guard is the whole point.

    The row moves to `queued` and the payload goes on tower-vends; the provisioner writes
    `provisioning` when it takes the message. Control Tower runs five account operations at
    once and the queue's consumer runs four, so a burst of signups waits in line instead of
    failing on the sixth.

    With no queue configured everything here still happens EXCEPT the vend: the row moves to
    `provisioning` and this returns true. The switch turns off Control Tower, not the state machine
    — a gerp left at `awaiting_payment` with a card on file makes the screen ask for a card the
    payer already gave, and makes every caller downstream read a paid purchase as an unpaid one."""
    ddb = _aws("dynamodb")
    row = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}).get("Item")
    if not row or row.get("status", {}).get("S") != "awaiting_payment":
        log.info(f"provision skipped for {gerp_id}: status={row and row.get('status')}")
        return False
    taken = "queued" if PROVISION_QUEUE else "provisioning"
    ddb.update_item(
        TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET #s = :new, queued_at = :t",
        ConditionExpression="#s = :old",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":new": {"S": taken}, ":old": {"S": "awaiting_payment"},
                                   ":t": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}},
    )
    # the business's public profile row, from what the create screen collected — the row the
    # measured industry lands on and the public page reads
    _save_business_profile(gerp_id, row.get("label", {}).get("S", gerp_id), _plain(row.get("public")) or {})
    if not PROVISION_QUEUE:
        # STAND IN for the provisioner rather than skip it: the transition above, a pause where
        # Control Tower's ~15 minutes would be, then the completion a finished one writes. Lets
        # create → pay → provision → ready be exercised without a real sub-account left behind,
        # and is what local dev runs against.
        #
        # Parking the row at `provisioning` is not the cheaper version of this — it spins a card
        # promising "ready in a few minutes" that never is, and gives the owner nothing to click.
        time.sleep(STUB_PROVISION_SECONDS)
        ddb.update_item(
            TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
            UpdateExpression="SET #s = :new, gateway_url = :g, provision_stub = :stub",
            ConditionExpression="#s = :old",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":new": {"S": "active"}, ":old": {"S": "provisioning"},
                                       ":g": {"S": STUB_GATEWAY_URL}, ":stub": {"BOOL": True}},
        )
        log.info(f"provisioning disabled; {gerp_id} stubbed active, no account vended")
        return True
    _aws("sqs").send_message(
        QueueUrl=PROVISION_QUEUE,
        MessageBody=json.dumps({
            "customer_id": gerp_id,  # provision_customer's event contract field is still customer_id
            "region": row.get("region", {}).get("S", DEFAULT_REGION),
            "owner_sub": row.get("owner_sub", {}).get("S", ""),
            "owner_email": row.get("owner_email", {}).get("S", ""),
            "business_name": row.get("label", {}).get("S", gerp_id),
            "business_category": "",
            # private by default (README: "private by default") — the owner opts into
            # publishing via the gerp's openly-operated toggle (modules/settings).
            "openly_operated": row.get("openly_operated", {}).get("BOOL", False),
            # the business's legal profile and its public one, into the tenant blob
            "legal": _plain(row.get("legal")) or {},
            "public": _plain(row.get("public")) or {},
        }),
    )
    log.info(f"queued {gerp_id} for provisioning")
    return True


def _queued_ahead(queued_at: str) -> int:
    """How many gerps are in line before this one: the `queued` rows with an earlier
    `queued_at`. The provisioner takes them in that order, four at a time."""
    ddb = _aws("dynamodb")
    resp = ddb.scan(
        TableName=CUSTOMERS_TABLE, Select="COUNT",
        FilterExpression="#s = :q AND queued_at < :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":q": {"S": "queued"}, ":t": {"S": queued_at}},
    )
    return int(resp.get("Count", 0))


# ── public profile (account-level capability action) ────────────────────────

def _clean_links(body: dict) -> list[dict]:
    """Sanitize the profile's links (socials/website): a list of {type, url}. Drops empties,
    bounds count/length, and prefixes a scheme so stored urls are always clickable."""
    out = []
    for it in (body.get("links") or [])[:12]:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()[:300]
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        out.append({"type": ((it.get("type") or "other").strip()[:30] or "other"), "url": url})
    return out


def _get_public_user(sub: str) -> dict:
    """The account's public profile: PUBLIC_FIELDS + verified (bool) + lat/lng (float|None) + links."""
    it = _aws("dynamodb").get_item(
        TableName=PROFILES_TABLE, Key={"gerp_profile_id": {"S": sub}}).get("Item") or {}
    out = {k: it.get(k, {}).get("S", "") for k in PUBLIC_FIELDS}
    out["verified"] = it.get("verified", {}).get("BOOL", False)
    out["lat"] = float(it["lat"]["N"]) if "lat" in it else None
    out["lng"] = float(it["lng"]["N"]) if "lng" in it else None
    out["links"] = [
        {"type": m.get("M", {}).get("type", {}).get("S", ""), "url": m.get("M", {}).get("url", {}).get("S", "")}
        for m in it.get("links", {}).get("L", [])
    ]
    # measured, never the owner's: what they do, read off their work across gerps (empty until a
    # count has run)
    out["soc"] = _measured(it, "soc", "hours")
    out["soc_window"] = it.get("soc_window", {}).get("S", "")
    return out


def _measured(item: dict, field: str, weight: str) -> list[dict]:
    """A measured distribution off a profile row: `[{code, share, <weight>}]`, sorted by share."""
    out = []
    for m in item.get(field, {}).get("L", []):
        v = m.get("M", {})
        out.append({"code": v.get("code", {}).get("S", ""),
                    "share": float(v.get("share", {}).get("N", "0")),
                    weight: float(v.get(weight, {}).get("N", "0"))})
    return sorted(out, key=lambda x: -x["share"])


def _put_public_user(sub: str, body: dict) -> None:
    """Write the account's own public profile (`gerp-profiles`, key=gerp_profile_id=sub) — a person row
    (kind=person, edges=the account_id). Platform-wide; read for public-profile pages. display_name is
    derived (first+last). update_item (not put) preserves `verified`, which the future verification flow
    owns. Same-account write (BFF is operator).

    Scope: an account editing ITS OWN profile. Creating a gerp_profile_id for someone *referenced*
    by a business (no account) is a separate identity concern (TODO)."""
    clean = {k: (body.get(k) or "").strip() for k in PUBLIC_FIELDS}
    display_name = f"{clean['first']} {clean['last']}".strip() or clean.get("email", "")
    geo_vals = {k: float(body[k]) for k in ("lat", "lng") if isinstance(body.get(k), (int, float))}  # only when geocoded
    links = _clean_links(body)
    # alias every field name (#k) — 'state' (and others) are DDB reserved words. kind/edges = the
    # polymorphic identity stamped on every write (a person profile edges its account_id = sub).
    sets = ["#display_name = :display_name", "#links = :links", "#kind = :kind", "#edges = :edges"] + [f"#{k} = :{k}" for k in PUBLIC_FIELDS]
    names = {"#display_name": "display_name", "#links": "links", "#kind": "kind", "#edges": "edges", **{f"#{k}": k for k in PUBLIC_FIELDS}}
    vals = {
        ":display_name": {"S": display_name},
        ":links": {"L": [{"M": {"type": {"S": lk["type"]}, "url": {"S": lk["url"]}}} for lk in links]},
        ":kind": {"S": "person"},
        ":edges": {"S": sub},
        **{f":{k}": {"S": clean[k]} for k in PUBLIC_FIELDS},
    }
    for k, v in geo_vals.items():  # lat/lng as numbers, only when an address was resolved
        sets.append(f"#{k} = :{k}"); names[f"#{k}"] = k; vals[f":{k}"] = {"N": str(v)}
    _aws("dynamodb").update_item(
        TableName=PROFILES_TABLE,
        Key={"gerp_profile_id": {"S": sub}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


def _save_business_profile(gerp_id: str, label: str, public: dict) -> None:
    """Write the business's public profile (`gerp-profiles`, key=gerp_profile_id=gerp_id) — a
    business row (kind=business, edges=the gerp_id). `label` is the published name, the instance
    label when none was given; `display_name` derives from it. update_item, like the person's,
    so what later writers add (the measured `naics`, `verified`) survives a re-run."""
    label = public.get("name") or label
    fields = {k: public.get(k, "") for k in PUBLIC_BUSINESS_FIELDS if k != "name"}
    sets = ["#label = :label", "#display_name = :display_name", "#links = :links", "#kind = :kind", "#edges = :edges"] + [f"#{k} = :{k}" for k in fields]
    names = {"#label": "label", "#display_name": "display_name", "#links": "links", "#kind": "kind", "#edges": "edges", **{f"#{k}": k for k in fields}}
    vals = {
        ":label": {"S": label},
        ":display_name": {"S": label},
        ":links": {"L": [{"M": {"type": {"S": lk["type"]}, "url": {"S": lk["url"]}}} for lk in public.get("links") or []]},
        ":kind": {"S": "business"},
        ":edges": {"S": gerp_id},
        **{f":{k}": {"S": v} for k, v in fields.items()},
    }
    for k in ("lat", "lng"):
        if isinstance(public.get(k), (int, float)):
            sets.append(f"#{k} = :{k}"); names[f"#{k}"] = k; vals[f":{k}"] = {"N": str(public[k])}
    _aws("dynamodb").update_item(
        TableName=PROFILES_TABLE,
        Key={"gerp_profile_id": {"S": gerp_id}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


# ── account info (account-level; private profile in gerp-accounts, key=account_id=sub) ──
#
# Convention: Cognito does auth (the login + email); the app's account/profile data lives
# in our own table keyed by the Cognito sub. The account row is seeded at signup by the
# cognito_post_confirmation trigger and read/written here.

# The operator's full private record — what stands behind a published feed or profile. Column
# name on the row → key on the wire. `middle` and `unit` are the two a person may genuinely not
# have; everything else is required before the account can create a gerp or publish a profile.
ACCOUNT_FIELDS = {"first_name": "first", "middle_name": "middle", "last_name": "last",
                  "phone": "phone", "street": "street", "unit": "unit", "city": "city",
                  "state": "state", "zip": "zip", "country": "country"}
ACCOUNT_REQUIRED = ("first", "last", "phone", "street", "city", "state", "zip", "country")

# The business's legal profile — who it is, where it is, how to reach it. Asked on Create a gerp,
# held on the gerp-customers row, never served; it goes to the tenant blob and to the gerp's
# contact in gradienterp's books. `unit` is the one a business may not have.
LEGAL_FIELDS = ("name", "email", "phone", "street", "unit", "city", "state", "zip", "country", "tax_id")
LEGAL_REQUIRED = ("name", "email", "phone", "street", "city", "state", "zip", "country")
# The business's public profile: the name (the label; it stands in when blank), the legal fields
# the owner ticked as public, links. Optional as a whole; it seeds the business's `gerp-profiles`
# row at provisioning. The screen sends copies of the legal values, so the map stands on its own.
PUBLIC_BUSINESS_FIELDS = ("name", "street", "unit", "city", "state", "zip", "country", "email", "phone")


_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _legal_record(body: dict) -> tuple:
    """The legal business profile off a create body, and which required fields it lacks. The
    `tax_id` is the GSTIN and only an Indian business carries one: dropped for any other country,
    and one that is not a GSTIN is named as missing, so the form says so before the card step."""
    src = body.get("legal") if isinstance(body.get("legal"), dict) else {}
    rec = {k: v for k in LEGAL_FIELDS if (v := str(src.get(k) or "").strip())}
    if "tax_id" in rec:
        if not _is_india(rec.get("country")):
            rec.pop("tax_id")
        else:
            rec["tax_id"] = rec["tax_id"].upper()
    missing = [k for k in LEGAL_REQUIRED if k not in rec]
    if rec.get("tax_id") and not _GSTIN.match(rec["tax_id"]):
        missing.append("tax_id")
    return rec, missing


def _public_record(body: dict) -> dict:
    """The public business profile off a create body: the string fields, the links, lat/lng."""
    src = body.get("public") if isinstance(body.get("public"), dict) else {}
    rec = {k: v for k in PUBLIC_BUSINESS_FIELDS if (v := str(src.get(k) or "").strip())}
    links = _clean_links(src)
    if links:
        rec["links"] = links
    for k in ("lat", "lng"):
        if isinstance(src.get(k), (int, float)):
            rec[k] = float(src[k])
    return rec


def _attr(v):
    """A plain value as a DynamoDB attribute (strings, numbers, bools, dicts, lists)."""
    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, (int, float)):
        return {"N": str(v)}
    if isinstance(v, dict):
        return {"M": {k: _attr(x) for k, x in v.items()}}
    if isinstance(v, list):
        return {"L": [_attr(x) for x in v]}
    return {"S": str(v)}


def _plain(attr):
    """A DynamoDB attribute as a plain value — the inverse of `_attr`."""
    if not isinstance(attr, dict) or not attr:
        return None
    (t, v), = attr.items()
    if t == "S":
        return v
    if t == "N":
        return float(v)
    if t == "BOOL":
        return bool(v)
    if t == "M":
        return {k: _plain(x) for k, x in v.items()}
    if t == "L":
        return [_plain(x) for x in v]
    return None


def _gerp_row(gerp_id: str) -> dict:
    return _aws("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}).get("Item") or {}


def _is_india(country) -> bool:
    """India, as the create form and Places write it: the ISO code or the name."""
    return (country or "").strip().upper() in ("IN", "IND", "INDIA")


def _legal_of(gerp_id: str) -> dict:
    """The legal business profile off a gerp's row; empty for a row written before it was asked."""
    return _plain(_gerp_row(gerp_id).get("legal")) or {}


def _business_info(row: dict) -> dict:
    return {"gerp_id": row.get("gerp_id", {}).get("S", ""), "label": row.get("label", {}).get("S", ""),
            "legal": _plain(row.get("legal")) or {}, "public": _plain(row.get("public")) or {}}


def _write_business_info(gerp_id: str, label: str, legal: dict, public: dict) -> None:
    _aws("dynamodb").update_item(
        TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET #l = :l, #legal = :legal, #public = :public",
        ExpressionAttributeNames={"#l": "label", "#legal": "legal", "#public": "public"},
        ExpressionAttributeValues={":l": {"S": label}, ":legal": _attr(legal), ":public": _attr(public)})


def _business_info_changed(gerp_id: str, label: str, legal: dict, public: dict) -> None:
    """The row is written; this is the copy in the gerp's own account — tower rewrites the tenant
    blob. Best effort after the row, like the owner-email change: a failure is logged, the row
    stands."""
    if not BUSINESS_INFO_FN:
        return
    try:
        resp = _aws("lambda").invoke(
            FunctionName=BUSINESS_INFO_FN, InvocationType="RequestResponse",
            Payload=json.dumps({"gerp_id": gerp_id, "business_name": label, "legal": legal, "public": public}).encode())
        raw = resp["Payload"].read()
        result = json.loads(raw or b"{}")
        if resp.get("FunctionError") or int(result.get("statusCode", 200)) >= 300:
            log.error("business info for %s did not reach tower: %s", gerp_id, raw[:400])
        else:
            log.info("business info for %s reached tower: %s", gerp_id, result.get("body"))
    except Exception:  # noqa: BLE001
        log.exception("business info invoke failed")


def _account_missing(acct: dict) -> list:
    """Which required fields the record lacks — what the gates refuse on and the screen names."""
    return [k for k in ACCOUNT_REQUIRED if not (acct.get(k) or "").strip()]


def _get_account(sub: str) -> dict:
    """The caller's private account profile: the record's fields, `email`, `missing`, and the
    optional default card as `payment_method`.

    `payment_method` is the account's optional DEFAULT card — what a gerp with no card of its own
    bills. Absent means none on file, which is a real state and the one the panel starts in."""
    it = _aws("dynamodb").get_item(
        TableName=ACCOUNTS_TABLE, Key={"account_id": {"S": sub}}).get("Item") or {}
    g = lambda k: it.get(k, {}).get("S", "")  # noqa: E731
    out = {wire: g(col) for col, wire in ACCOUNT_FIELDS.items()}
    out["email"] = g("email")
    out["missing"] = _account_missing(out)
    if _prior_of_row(it):
        out["prior"] = _prior_of_row(it)
    # the ids stay server-side; only what a person needs to recognise their own card goes out
    if g("card_last4"):
        out["payment_method"] = {"brand": g("card_brand"), "last4": g("card_last4"),
                                 "exp": g("card_exp")}
    return out


def _save_account_card(sub: str, card: dict) -> None:
    """The account's default payment method, from a finished account-scoped checkout.

    Written HERE rather than by the seller's lambda: `gerp-accounts` is the operator's table and the
    seller runs in a different account, so the alternative is a cross-account write for a row this
    service already owns. The seller keeps the vault ids on its own contact either way."""
    _aws("dynamodb").update_item(
        TableName=ACCOUNTS_TABLE,
        Key={"account_id": {"S": sub}},
        UpdateExpression="SET stripe_customer_id = :c, stripe_payment_method_id = :p, "
                         "card_brand = :b, card_last4 = :l, card_exp = :e",
        ExpressionAttributeValues={
            ":c": {"S": card.get("stripe_customer_id", "")},
            ":p": {"S": card.get("stripe_payment_method_id", "")},
            ":b": {"S": card.get("card_brand", "")},
            ":l": {"S": card.get("card_last4", "")},
            ":e": {"S": card.get("card_exp", "")},
        },
    )


def _owner_email_changed(sub: str, old: str, new: str) -> None:
    """The row is written; this is the rest — tower renames the Identity Center user and rewrites
    `owner_email` on each gerp this account OWNS (a member's change is theirs alone, and the blob
    names the owner). Best effort after the row: a failure is logged, the row stands, and the
    next read with a differing claim does not run again — so the log line is the signal."""
    if not OWNER_EMAIL_FN:
        return
    owned = [g["gerp_id"] for g in _member_gerps(sub) if g.get("role") == "owner" and g.get("status") != "closed"]
    try:
        resp = _aws("lambda").invoke(
            FunctionName=OWNER_EMAIL_FN, InvocationType="RequestResponse",
            Payload=json.dumps({"old_email": old, "new_email": new, "gerp_ids": owned}).encode())
        raw = resp["Payload"].read()
        result = json.loads(raw or b"{}")
        if resp.get("FunctionError") or int(result.get("statusCode", 200)) >= 300:
            log.error("owner email change did not reach tower: %s", raw[:400])
        else:
            log.info("owner email %s -> %s reached tower: %s", old, new, result.get("body"))
    except Exception:  # noqa: BLE001
        log.exception("owner email change invoke failed")


def _legal_name(acct: dict) -> str:
    """The party liable for a gerp's invoices: the owner's legal name off the private record. A
    gerp is not a legal entity; the person running it is, and the invoice names them."""
    return " ".join(p for p in ((acct.get("first") or "").strip(), (acct.get("last") or "").strip()) if p)


def _sync_account_email(sub: str, email: str) -> None:
    """Write the login email onto the row — creating the row when a signup seed never landed.
    One upsert; the name columns are untouched, because the row owns those."""
    _aws("dynamodb").update_item(
        TableName=ACCOUNTS_TABLE,
        Key={"account_id": {"S": sub}},
        UpdateExpression="SET email = :e",
        ExpressionAttributeValues={":e": {"S": email}},
    )


# the org's account count against its quota, read through a role in the management account
# (prod/platform/management capacity_read_role.tf); the create screen says how many accounts
# are left. Organizations caps an org's accounts (L-E619E033) and every account
# it lists counts, closed ones until they leave
CAPACITY_READ_ROLE = os.environ.get("CAPACITY_READ_ROLE", "")
ORG_ACCOUNTS_QUOTA_CODE = "L-E619E033"
_capacity_cache: dict = {}   # {"at": epoch, "body": {...}}
CAPACITY_TTL_S = 60
# the card page (web/card.html): Stripe.js mounts with this, the account's publishable key
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
# private support: the /support page posts here with no login; the message goes out through
# SES from the operator's sender to SUPPORT_EMAIL, which no page and no response ever carries
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
SUPPORT_SUBJECT_MAX, SUPPORT_MESSAGE_MAX = 200, 5000
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
CUSTOMER_HOOK_PARAM = os.environ.get("CUSTOMER_HOOK_PARAM", "")
# the same door in reverse — customers/erase, called when the account is deleted
CUSTOMER_ERASE_HOOK_PARAM = os.environ.get("CUSTOMER_ERASE_HOOK_PARAM", "")
_customer_hook_cache: dict = {}   # param name → {url, token}


def _hook(param: str) -> dict:
    """`{url, token}` from this stack's own SSM. The seller gerp published a hook for its web app
    (`manage_hooks` publish) and the operator stored what it returned. Read once per container;
    empty when the stack has no hook configured or the parameter is unreadable."""
    if not param:
        return {}
    if param not in _customer_hook_cache:
        try:
            raw = _aws("ssm").get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
            _customer_hook_cache[param] = json.loads(raw)
        except Exception:  # noqa: BLE001
            log.exception("hook parameter %s unreadable", param)
            return {}
    return _customer_hook_cache[param]


def _support(body: dict) -> dict:
    """A message from the /support page, sent through SES. Refused with the field named when
    the email is not one, the subject or message is empty or over its cap, or the hidden
    `website` field is filled (a form-filler, not a person). A send SES refuses is a 502 and
    the page keeps the text."""
    email = (body.get("email") or "").strip()
    subject = (body.get("subject") or "").strip()
    message = (body.get("message") or "").strip()
    if body.get("website"):
        return _json(400, {"error": "refused", "field": "website"})
    if not _EMAIL_RE.fullmatch(email) or len(email) > 254:
        return _json(400, {"error": "an email address is needed", "field": "email"})
    if not subject or len(subject) > SUPPORT_SUBJECT_MAX:
        return _json(400, {"error": f"a subject is needed, up to {SUPPORT_SUBJECT_MAX} characters", "field": "subject"})
    if not message or len(message) > SUPPORT_MESSAGE_MAX:
        return _json(400, {"error": f"a message is needed, up to {SUPPORT_MESSAGE_MAX} characters", "field": "message"})
    if not (SENDER_EMAIL and SUPPORT_EMAIL):
        log.error("support form: SENDER_EMAIL / SUPPORT_EMAIL not set")
        return _json(502, {"error": "not sent, try again"})
    text = f"from: {email}\nat: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n{message}\n"
    try:
        _aws("ses").send_email(
            Source=SENDER_EMAIL, Destination={"ToAddresses": [SUPPORT_EMAIL]}, ReplyToAddresses=[email],
            Message={"Subject": {"Data": f"[support] {subject}"}, "Body": {"Text": {"Data": text}}})
    except Exception:  # noqa: BLE001
        log.exception("support form: send failed")
        return _json(502, {"error": "not sent, try again"})
    log.info("support form: sent, reply-to %s", email)
    return _json(200, {"sent": True})


def _capacity() -> dict:
    """`accounts`, `quota`, `available` (quota less every account the org lists) and
    `measured_at`, counted now through the management role. `{}` when the role is not wired or
    the reads fail; at most once a minute per container."""
    if not CAPACITY_READ_ROLE:
        return {}
    now = time.time()
    if _capacity_cache.get("body") is not None and now - _capacity_cache.get("at", 0) < CAPACITY_TTL_S:
        return _capacity_cache["body"]
    try:
        creds = _aws("sts").assume_role(RoleArn=CAPACITY_READ_ROLE, RoleSessionName="capacity")["Credentials"]
        kw = dict(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                  aws_session_token=creds["SessionToken"], region_name="us-east-1")
        orgs = boto3.client("organizations", **kw)
        accounts = sum(len(page.get("Accounts", [])) for page in orgs.get_paginator("list_accounts").paginate())
        quota = int(boto3.client("service-quotas", **kw).get_service_quota(
            ServiceCode="organizations", QuotaCode=ORG_ACCOUNTS_QUOTA_CODE)["Quota"]["Value"])
        body = {"accounts": accounts, "quota": quota, "available": max(quota - accounts, 0),
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    except Exception:  # noqa: BLE001
        log.exception("capacity not read through %s", CAPACITY_READ_ROLE)
        body = {}
    _capacity_cache.update(at=now, body=body)
    return body


def _customer_hook() -> dict:
    return _hook(CUSTOMER_HOOK_PARAM)


def _post_json(url: str, token: str, payload: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _post_hook(param: str, payload: dict, what: str) -> int | None:
    """One post to a hook the seller published. None when no hook is configured or the post
    itself failed; otherwise the status. A 401 drops the cached parameter — the seller rotated
    the secret — so the next call reads it again."""
    hook = _hook(param)
    if not hook.get("url") or not hook.get("token"):
        return None
    try:
        status = _post_json(hook["url"], hook["token"], payload)
    except Exception:  # noqa: BLE001
        log.exception("%s post failed", what)
        return None
    if status == 401:
        _customer_hook_cache.pop(param, None)
    if status != 200:
        log.warning("%s post returned %s", what, status)
    return status


def _post_customer_contact(sub: str) -> None:
    """The seller gerp keeps its customers as contacts. The account record goes out through the
    hook the seller published — a url and a bearer, the door any outside caller gets. The save
    this follows has already landed: a failed post is logged and the save stands."""
    if not _hook(CUSTOMER_HOOK_PARAM):
        return
    acct = _get_account(sub)
    payload = {"account_id": sub, "email": acct.get("email") or ""}
    payload.update({wire: acct.get(wire) or "" for wire in ACCOUNT_FIELDS.values()})
    # the gerps this account owns from before the business's own legal profile was asked: their
    # contacts carry the owner's name as `legal_name`, and a corrected name has to land on each.
    # A gerp with a legal profile names itself, and this leaves it alone.
    payload["gerp_ids"] = [g["gerp_id"] for g in _member_gerps(sub)
                           if g.get("role") == "owner" and g.get("status") != "closed" and not _legal_of(g["gerp_id"])]
    _post_hook(CUSTOMER_HOOK_PARAM, payload, "customer contact")


def _post_gerp_contact(gerp_id: str, label: str, legal: dict) -> None:
    """The gerp's contact in the seller's books — the payer its invoices bill — takes the edited
    business profile through the same hook, as an organization: `gerp_id`, `name` (the label),
    `legal_name` and the legal record's email, phone and address. A contact not born yet (no card
    saved) is written when the card lands."""
    if not _hook(CUSTOMER_HOOK_PARAM):
        return
    payload = {"gerp_id": gerp_id, "name": label, "legal_name": legal.get("name") or ""}
    # the hook's fields are the contact card's; the GSTIN reaches the contact when the card is saved
    payload.update({k: legal.get(k) or "" for k in LEGAL_FIELDS if k not in ("name", "tax_id")})
    _post_hook(CUSTOMER_HOOK_PARAM, payload, "gerp contact")


def _post_customer_erase(sub: str) -> bool:
    """The seller's contact for a deleted account, made a shell (`customers/erase`). True when
    the seller confirmed, or when there is no hook to call; False when the post did not land,
    which fails the deletion so it is run again."""
    if not _hook(CUSTOMER_ERASE_HOOK_PARAM):
        return True
    return _post_hook(CUSTOMER_ERASE_HOOK_PARAM, {"account_id": sub}, "customer erase") == 200


# ── priors: what a closed account left behind ───────────────────────────────

def _hash(value: str) -> str:
    return hashlib.sha256((value or "").strip().lower().encode()).hexdigest()


def _phone_key(phone: str) -> str:
    """The phone's digits, hashed — however it was typed."""
    digits = re.sub(r"\D", "", phone or "")
    return f"phone#{_hash(digits)}" if digits else ""


def _prior(key: str) -> dict:
    """The prior row for one identifier, flattened: `{how, balance_owed, closed_at}` or `{}`."""
    if not key:
        return {}
    it = _aws("dynamodb").get_item(TableName=PRIORS_TABLE, Key={"id": {"S": key}}).get("Item")
    if not it:
        return {}
    return {"how": it.get("how", {}).get("S", "requested"),
            "balance_owed": float(it.get("balance_owed", {}).get("N", "0")),
            "closed_at": it.get("closed_at", {}).get("S", ""),
            "gerps": [e.get("M", {}).get("gerp_id", {}).get("S", "") for e in it.get("endings", {}).get("L", [])]}


def _owed_invoices(prior: dict) -> list[str]:
    """The closed gerps a prior names that still owe, by their closing invoice."""
    out = []
    for gid in prior.get("gerps", []):
        row = _aws("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gid}}).get("Item") or {}
        inv = row.get("closed_invoice_id", {}).get("S", "")
        if inv and float(row.get("balance_owed", {}).get("N", "0")) > 0:
            out.append(inv)
    return out


def _prior_of_row(item: dict) -> dict | None:
    m = item.get("prior", {}).get("M")
    if not m:
        return None
    return {"how": m.get("how", {}).get("S", ""), "balance_owed": float(m.get("balance_owed", {}).get("N", "0"))}


def _billing_of_row(item: dict) -> list[dict]:
    """The hosting invoices still open against a gerp — tower's `billing` list on the row."""
    out = []
    for m in item.get("billing", {}).get("L", []):
        v = m.get("M", {})
        out.append({"invoice_id": v.get("invoice_id", {}).get("S", ""),
                    "total": float(v.get("total", {}).get("N", "0")),
                    "period": v.get("period", {}).get("S", ""),
                    "unpaid_at": v.get("unpaid_at", {}).get("S", "")})
    return out


def _unpaid(billing: list[dict]) -> dict | None:
    """What the screen shows: the oldest unpaid hosting invoice, the total across all of them, and
    the closure date the chase runs to."""
    open_ = sorted((b for b in billing if b.get("unpaid_at")), key=lambda b: b["period"])
    if not open_:
        return None
    first = open_[0]
    try:
        unpaid_ms = int(float(first["unpaid_at"]))
    except ValueError:
        unpaid_ms = 0
    closes_on = (time.strftime("%Y-%m-%d", time.gmtime(unpaid_ms / 1000 + CLOSES_AFTER_DAYS * 86400))
                 if unpaid_ms else "")
    return {"invoice_id": first["invoice_id"], "total": round(sum(b["total"] for b in open_), 2),
            "invoices": [b["invoice_id"] for b in open_], "unpaid_at": first["unpaid_at"], "closes_on": closes_on}


def _prior_attr(prior: dict) -> dict:
    return {"M": {"how": {"S": prior["how"]}, "balance_owed": {"N": str(prior["balance_owed"])}}}


def _write_priors(keys: list[str], endings: list[dict], stripe_customer_id: str) -> None:
    """One row per identifier. A key already present (the same card under an earlier account)
    keeps its endings and gains these — the balance is the sum, `how` is unpaid if any is."""
    ddb = _aws("dynamodb")
    now = _iso_now()
    for key in keys:
        have = ddb.get_item(TableName=PRIORS_TABLE, Key={"id": {"S": key}}).get("Item") or {}
        kept = [{"gerp_id": e["M"]["gerp_id"]["S"], "how": e["M"]["how"]["S"], "closed_at": e["M"]["closed_at"]["S"],
                 "balance_owed": float(e["M"].get("balance_owed", {}).get("N", "0"))}
                for e in have.get("endings", {}).get("L", [])]
        seen = {e["gerp_id"] for e in kept}
        merged = kept + [e for e in endings if e["gerp_id"] not in seen]
        balance = round(sum(e["balance_owed"] for e in merged), 2)
        ddb.put_item(TableName=PRIORS_TABLE, Item={
            "id": {"S": key},
            "closed_at": {"S": now},
            "how": {"S": "unpaid" if any(e["how"] == "unpaid" for e in merged) else "requested"},
            "balance_owed": {"N": str(balance)},
            "stripe_customer_id": {"S": stripe_customer_id or have.get("stripe_customer_id", {}).get("S", "")},
            "endings": {"L": [{"M": {"gerp_id": {"S": e["gerp_id"]}, "how": {"S": e["how"]},
                                     "closed_at": {"S": e["closed_at"]},
                                     "balance_owed": {"N": str(e["balance_owed"])}}} for e in merged]},
        })


def _card_prior(gerp_id: str, fingerprint: str) -> dict | None:
    """Before a card vends a gerp: the prior behind this card, if any. A hit is stamped on the
    gerp row either way; an unpaid one is returned, and the caller does not provision."""
    prior = _prior(f"card#{fingerprint}") if fingerprint else {}
    if not prior:
        return None
    _aws("dynamodb").update_item(
        TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
        UpdateExpression="SET #p = :p", ExpressionAttributeNames={"#p": "prior"},
        ExpressionAttributeValues={":p": _prior_attr(prior)})
    return ({"how": prior["how"], "balance_owed": prior["balance_owed"]}
            if prior["how"] == "unpaid" and prior["balance_owed"] > 0 else None)


def _vend_hold(sub: str, gerp_id: str) -> str:
    """Why a card landing must not vend now, or "": `limit` when the account already holds its live
    gerps (GERP_LIMIT, or the account row's `gerp_limit`), `capacity` when the organization has no
    account left to vend."""
    row = _aws("dynamodb").get_item(TableName=ACCOUNTS_TABLE, Key={"account_id": {"S": sub}}).get("Item") or {}
    limit = int(row.get("gerp_limit", {}).get("N", GERP_LIMIT))
    live = [g for g in _member_gerps(sub)
            if g.get("role") == "owner" and g["gerp_id"] != gerp_id and g.get("status") in LIVE_STATUSES]
    if len(live) >= limit:
        return "limit"
    capacity = _capacity()
    if capacity and capacity.get("available", 1) <= 0:
        return "capacity"
    return ""


def _provision_after_card(gerp_id: str, out: dict, sub: str) -> None:
    """The card landed (saved, or selected). Provision unless the card carries an unpaid ending, the
    account already holds its live gerps, or the organization has no account left."""
    blocked = _card_prior(gerp_id, out.get("fingerprint") or "")
    if blocked:
        log.info(f"provision held for {gerp_id}: card carries an unpaid ending, {blocked['balance_owed']} owed")
        out["provisioning"] = False
        out["prior"] = blocked
        return
    held = _vend_hold(sub, gerp_id)
    ddb = _aws("dynamodb")
    if held:
        log.info(f"provision held for {gerp_id}: {held}")
        ddb.update_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}},
                        UpdateExpression="SET held = :h", ExpressionAttributeValues={":h": {"S": held}})
        out["provisioning"] = False
        out["held"] = held
        return
    ddb.update_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}, UpdateExpression="REMOVE held")
    out["provisioning"] = _provision_gerp(gerp_id)


def _update_account(sub: str, fields: dict) -> None:
    """Write the record's fields (wire keys) onto the gerp-accounts row. Never Cognito, never the
    email — the email follows the token. A field absent from `fields` is left as it was."""
    cols = {col: (fields.get(wire) or "").strip() for col, wire in ACCOUNT_FIELDS.items() if wire in fields}
    if not cols:
        return
    _aws("dynamodb").update_item(
        TableName=ACCOUNTS_TABLE,
        Key={"account_id": {"S": sub}},
        UpdateExpression="SET " + ", ".join(f"#{c} = :{c}" for c in cols),
        ExpressionAttributeNames={f"#{c}": c for c in cols},
        ExpressionAttributeValues={f":{c}": {"S": v} for c, v in cols.items()},
    )


# ── forwarding ──────────────────────────────────────────────────────────────

def _forward(method: str, gateway_url: str, path: str, auth_header: str, body: bytes | None = None) -> tuple[int, str]:
    """Forward a request to a gerp's own gateway, passing the caller's bearer token through
    (the gerp gateway's JWT authorizer validates it). The BFF's ownership check guards this path."""
    url = gateway_url.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 502, json.dumps({"error": f"forward failed: {type(e).__name__}"})


def _owned_target(sub: str, gerp_id: str):
    """Resolve a gerp the caller is a member of → (target, None), else (None, error_response).
    The membership gate for any gerp-scoped forward (secrets, settings): the member row, the
    same read /api/gerps makes."""
    if not gerp_id:
        return None, _json(400, {"error": "gerp_id required"})
    target = {g["gerp_id"]: g for g in _member_gerps(sub)}.get(gerp_id)
    if not target:
        log.warning("ownership denied: sub=%s gerp_id=%s", sub, gerp_id)
        return None, _json(403, {"error": "not your gerp"})
    if not target.get("gateway_url"):
        return None, _json(500, {"error": f"no gateway_url for gerp {gerp_id}"})
    return target, None


# ── address autocomplete (AWS Location Places V2, via the BFF's IAM role) ─────
#
# Two-step typeahead: autocomplete(partial) → suggestions; on select, get_place(id) →
# structured components + coords. SigV4 via the BFF role — no browser API key / SDK.

def _places_autocomplete(q: str) -> list[dict]:
    """Partial address text → [{label, place_id}] suggestions (lean — label only)."""
    resp = geo.autocomplete(QueryText=q, MaxResults=6)
    out = []
    for it in resp.get("ResultItems", []):
        label = (it.get("Address") or {}).get("Label") or it.get("Title") or ""
        pid = it.get("PlaceId")
        if pid and label:
            out.append({"label": label, "place_id": pid})
    return out


def _place_details(place_id: str) -> dict:
    """Resolve a place_id → the form's address fields + lat/lng (for proximity)."""
    p = geo.get_place(PlaceId=place_id)
    a = p.get("Address") or {}
    pos = p.get("Position") or []  # [lng, lat]
    region = a.get("Region") or {}
    subregion = a.get("SubRegion") or {}   # the county, where a country has no first-level region here (Ireland)
    country = a.get("Country") or {}
    return {
        "street": f"{a.get('AddressNumber', '')} {a.get('Street', '')}".strip(),
        "city": a.get("Locality") or a.get("Municipality") or a.get("District") or "",
        "state": region.get("Code") or region.get("Name") or subregion.get("Name") or "",
        "zip": a.get("PostalCode") or "",
        "country": country.get("Name") or country.get("Code2") or "",
        "lat": pos[1] if len(pos) > 1 else None,
        "lng": pos[0] if len(pos) > 1 else None,
    }


# ── http helpers ──────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json(status: int, obj) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(obj)}


_EXT_CT = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json", ".txt": "text/plain"}
_EXT_BIN = {".gif": "image/gif", ".png": "image/png", ".ico": "image/x-icon"}


def _llms() -> str:
    """llms.txt, HTML-escaped, inlined into the shell's hidden agents-note at serve time — ONE
    source of truth, so the in-page copy can't drift from /llms.txt. Cached at cold start (the
    file only changes with a push, which recycles the environment)."""
    global _LLMS_CACHE
    if _LLMS_CACHE is None:
        import html as _h
        _LLMS_CACHE = _h.escape((WEB_DIR / "llms.txt").read_text())
    return _LLMS_CACHE


_LLMS_CACHE = None
_TERMS_CACHE = None


def _purchase_terms() -> tuple[str, str]:
    """The purchase disclosure and its version, inlined into the shell at serve time.

    Same shape as `_llms()` and for the same reason: `web/purchase-terms.txt` is ONE source of
    truth, plain enough to read on GitHub or at /purchase-terms.txt, and the page cannot drift
    from it.

    The version is the file's CONTENT hash, in git's blob form — `sha1("blob <len>\0" + bytes)`,
    the value `git hash-object` gives — so once the repo is published the version resolves to the
    exact wording in history without anything storing a copy. Content, not a commit: a repo SHA
    would churn on every unrelated change, and the thing being versioned is the text. Until the
    repo is public, an old version resolves out of a `./zip.sh` archive.
    """
    global _TERMS_CACHE
    if _TERMS_CACHE is None:
        import html as _h
        raw = (WEB_DIR / "purchase-terms.txt").read_bytes()
        version = hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()[:12]
        _TERMS_CACHE = (_h.escape(raw.decode().strip()), version)
    return _TERMS_CACHE


def _html(path: str, inm: str = "") -> dict:
    f = WEB_DIR / ("index.html" if path in ("/", "") else path.lstrip("/"))
    if not f.suffix and f.with_suffix(".html").is_file():
        f = f.with_suffix(".html")   # a page of its own under web/ (`/support` → support.html)
    # serve a real static asset under web/ (app.js, vendor/lit-html.js, …) with the right MIME so ES
    # modules load; otherwise fall back to the SPA shell (client routes). path-traversal guarded.
    if not (f.is_file() and (f.resolve() == (WEB_DIR / "index.html").resolve() or WEB_DIR.resolve() in f.resolve().parents)):
        f = WEB_DIR / "index.html"
    if f.suffix in _EXT_BIN:
        # binary asset (the login-page demo gifs) — base64 through the lambda shape, immutable-ish
        raw = f.read_bytes()
        etag = '"%s"' % hashlib.blake2b(raw, digest_size=10).hexdigest()
        if inm == etag:
            return {"statusCode": 304, "headers": {"cache-control": "no-cache", "etag": etag}}
        return {"statusCode": 200, "headers": {"content-type": _EXT_BIN[f.suffix], "cache-control": "no-cache", "etag": etag},
                "body": base64.b64encode(raw).decode(), "isBase64Encoded": True}
    body = f.read_text()
    if "<!--LLMS-->" in body:
        body = body.replace("<!--LLMS-->", _llms())
    if "<!--STRIPE-PUBLISHABLE-KEY-->" in body:
        # the card page's Stripe.js key: publishable, the account's public half
        import html as _h
        body = body.replace("<!--STRIPE-PUBLISHABLE-KEY-->", _h.escape(STRIPE_PUBLISHABLE_KEY))
    if "<!--PURCHASE-TERMS-->" in body:
        text, version = _purchase_terms()
        body = body.replace("<!--PURCHASE-TERMS-VERSION-->", version).replace("<!--PURCHASE-TERMS-->", text)
    # cache-control:no-cache + ETag — the browser caches but revalidates each load, getting a tiny
    # 304 (no body) when unchanged and the full body only after a redeploy. Fresh-on-deploy without
    # the per-load refetch that bare no-cache forces. (No content-hashed filenames here, so the
    # asset changes in place; the ETag is the validator.)
    etag = '"%s"' % hashlib.blake2b(body.encode(), digest_size=10).hexdigest()
    if inm == etag:
        return {"statusCode": 304, "headers": {"cache-control": "no-cache", "etag": etag}}
    ct = _EXT_CT.get(f.suffix, "text/html") + "; charset=utf-8"
    return {"statusCode": 200, "headers": {"content-type": ct, "cache-control": "no-cache", "etag": etag}, "body": body}


# ── handler ───────────────────────────────────────────────────────────────────

def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # static SPA for non-/api paths
    if not path.startswith("/api/"):
        return _html(path, headers.get("if-none-match", ""))

    # the one /api path with no subject: the /support page has no login
    if method == "POST" and path == "/api/support":
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError:
            return _json(400, {"error": "a JSON body is needed"})
        return _support(body if isinstance(body, dict) else {})

    sub = _sub(event)
    if not sub:
        return _json(401, {"error": "no authenticated subject"})

    if method == "GET" and path == "/api/capacity":
        # the create screen's line: what a create takes one of
        cap = _capacity()
        return _json(200, cap) if cap else _json(404, {"error": "capacity not readable"})

    if method == "GET" and path == "/api/gerps":
        gerps = _member_gerps(sub)
        # never leak gateway_url to the browser — the BFF routes. chat_url IS for the
        # browser (the chat deep-link). `status` is the row's — awaiting_payment, queued,
        # provisioning, active, close_requested, closing, closed, stopped — and the card renders
        # one line per value. `role` (owner | employee | …) drives the card's destination (hub vs
        # chat). A queued gerp carries `ahead`: the gerps in line before it.
        return _json(200, {"gerps": [
            {"gerp_id": g["gerp_id"], "label": g["label"], "chat_url": g.get("chat_url", ""),
             "role": g.get("role", "member"), "status": g.get("status", ""),
             "download_until": g.get("download_until", ""),
             **({"ahead": _queued_ahead(g["queued_at"])} if g.get("status") == "queued" else {}),
             # a purchase held because the card carries an unpaid ending: the screen shows the
             # balance where it showed "add a card"
             **({"prior": g["prior"]} if g.get("prior") and g["prior"]["how"] == "unpaid"
                and g.get("status") == "awaiting_payment" else {}),
             # a hosting invoice the monthly charge missed: the amount and the closure date
             **({"unpaid": _unpaid(g["billing"])} if _unpaid(g.get("billing") or []) else {})}
            for g in gerps
        ]})

    if method == "POST" and path == "/api/gerps":
        # account-level: create a new gerp for the authed account (no ownership
        # check — no gerp exists yet). Vends a real sub-account in prod.
        body = json.loads(event.get("body") or "{}")
        label = (body.get("business_name") or "").strip()
        if not label:
            return _json(400, {"error": "business_name required"})
        openly_operated = bool(body.get("openly_operated", False))  # default private (README: private by default)
        # The disclosure is what makes the card legible — it is the only place a buyer is told
        # they are getting an AWS account, billed on usage, at cost plus 20%. Enforced here and
        # not only by the checkbox, because a tick in a browser is not a record of anything.
        terms_version = (body.get("terms_version") or "").strip()
        if not terms_version:
            return _json(400, {"error": "terms_version required — the purchase disclosure "
                                        "must be shown and acknowledged"})
        # The person behind a gerp is known in full before the gerp exists — the platform publishes
        # what its gerps do, and a feed with nobody accountable behind it is the thing this refuses.
        # After the input checks: a malformed request is a 400 whatever the account's state.
        acct = _get_account(sub)
        missing = _account_missing(acct)
        if missing:
            return _json(409, {"error": "complete your account first", "missing": missing})
        # The business's own legal profile: name, address, email, phone — the compliance facts a
        # gerp stands behind, held on its row. The public profile is the optional half.
        legal, missing_legal = _legal_record(body)
        if missing_legal:
            return _json(400, {"error": "legal business profile incomplete", "missing": missing_legal})
        public = _public_record(body)
        # An earlier account that ended — found by the email or the phone this one carries. The
        # fact is stamped on the row either way; an unpaid ending refuses until the balance is
        # settled. The card is checked separately, when it lands.
        prior = _prior(f"email#{_hash(acct.get('email') or _email(event))}") or _prior(_phone_key(acct.get("phone")))
        if prior:
            _aws("dynamodb").update_item(
                TableName=ACCOUNTS_TABLE, Key={"account_id": {"S": sub}},
                UpdateExpression="SET #p = :p", ExpressionAttributeNames={"#p": "prior"},
                ExpressionAttributeValues={":p": _prior_attr(prior)})
            if prior["how"] == "unpaid" and prior["balance_owed"] > 0:
                # the invoices still owed, so the screen can offer a way to pay them
                return _json(409, {"error": "an earlier account closed unpaid",
                                   "balance_owed": prior["balance_owed"],
                                   "invoices": _owed_invoices(prior)})
        # the region: the screen's pick, or the address's country's; a region not offered refuses
        region = (body.get("region") or "").strip() or _region_for(legal.get("country", ""))
        if REGIONS and REGIONS.get(region, {}).get("status") != "offered":
            return _json(400, {"error": f"{region} is not offered yet", "regions": _regions_out()})
        # the suffix makes the id unique; on the one-in-sixteen-million collision, draw again
        for attempt in range(3):
            gerp_id = _new_gerp_id(label)
            try:
                _create_gerp(sub, gerp_id, label, _email(event), openly_operated, terms_version, legal, public, region)
                break
            except _aws("dynamodb").exceptions.ConditionalCheckFailedException:
                if attempt == 2:
                    raise
        # awaiting_payment, not provisioning: the card comes next, and it is what vends
        return _json(202, {"status": "awaiting_payment", "gerp_id": gerp_id, "label": label,
                           "openly_operated": openly_operated})

    if method == "POST" and path == "/api/mcp/complete":
        # The owner landed back from a vendor's consent (modules/mcp). The session id is all the
        # url carries; which gerp was waiting on it is asked of each gerp the account owns, and
        # the one holding that pending session completes it. Owners only: the grant is the firm's.
        body = json.loads(event.get("body") or "{}")
        session = (body.get("session_id") or "").strip()
        if not session:
            return _json(400, {"error": "session_id required"})
        for gerp in _member_gerps(sub):
            if gerp.get("role", "owner") != "owner" or not gerp.get("aws_account_id"):
                continue
            out = _invoke_gerp(gerp, "complete_mcp_auth", {"session_id": session, "account_id": sub}, module="mcp")
            status = out.pop("_status", 502 if "error" in out else 200)
            if status == 404:
                continue
            out["gerp_id"] = gerp["gerp_id"]
            return _json(status, out)
        return _json(404, {"error": "no consent is pending for that session on your gerps"})

    if method == "POST" and path == "/api/export":
        # Taking your data out, from the account screen rather than by asking the agent.
        #
        # This is not a convenience duplicate of the agent's tool — it is the door that still
        # exists after closure. A closed gerp keeps its bucket, its CMK, the reader role and this
        # one lambda; the agent, the gateway and the runtime are gone. So the only way to hand
        # someone a fresh link during the 15-day window is from here, in the operator account,
        # which a customer's closure cannot reach. See modules/export/TODO.md § closure.
        body = json.loads(event.get("body") or "{}")
        gerp_id = (body.get("gerp_id") or "").strip()
        if not gerp_id:
            return _json(400, {"error": "gerp_id required"})
        gerp = next((g for g in _member_gerps(sub) if g["gerp_id"] == gerp_id), None)
        if not gerp:
            return _json(403, {"error": "not your gerp"})

        # Reads answer inline; anything that MAKES a copy is fired and polled for. API Gateway
        # gives this handler ~30s and an export of a real firm takes minutes, so a synchronous
        # full export would always time out — and time out AFTER starting, leaving a half-written
        # prefix and a caller who thinks nothing happened.
        if body.get("credentials_only"):
            out = _invoke_gerp(gerp, "export_gerp", {"credentials_only": True})
            # the callee's own status passes through — its 404 is "no export yet", which is also
            # the not-ready answer while one runs, and the screen says so. Only an answer with no
            # status at all (the invoke itself failed) is this door's 502.
            return _json(out.pop("_status", 502 if "error" in out else 200), out)

        started = _invoke_gerp(gerp, "export_gerp",
                               {k: v for k, v in body.items() if k in ("include", "resume")},
                               wait=False)
        if not started.get("started"):
            return _json(502, started)
        return _json(202, {"status": "started", "gerp_id": gerp_id,
                           "poll": "POST /api/export {gerp_id, credentials_only: true}"})

    if path == "/api/gerp-info" and method in ("GET", "POST"):
        # gerp-level: the business info behind a gerp — the label, the legal profile, the public
        # one — read and written by its members. A save writes the row, then the copies: the
        # business's profile row here (once provisioning has made one), the tenant blob through
        # tower, the gerp's contact in the seller's books through the hook.
        body = json.loads(event.get("body") or "{}") if method == "POST" else {}
        gerp_id = (body.get("gerp_id") or (event.get("queryStringParameters") or {}).get("gerp_id") or "").strip()
        if not gerp_id:
            return _json(400, {"error": "gerp_id required"})
        if not any(g["gerp_id"] == gerp_id for g in _member_gerps(sub)):
            return _json(403, {"error": "not your gerp"})
        row = _gerp_row(gerp_id)
        if not row:
            return _json(404, {"error": "no such gerp"})
        if method == "GET":
            return _json(200, _business_info(row))
        label = (body.get("label") or "").strip()
        if not label:
            return _json(400, {"error": "label required"})
        legal, missing_legal = _legal_record(body)
        if missing_legal:
            return _json(400, {"error": "legal business profile incomplete", "missing": missing_legal})
        public = _public_record(body)
        _write_business_info(gerp_id, label, legal, public)
        if row.get("status", {}).get("S") not in ("awaiting_payment", "closed"):
            _save_business_profile(gerp_id, label, public)
        _business_info_changed(gerp_id, label, legal, public)
        _post_gerp_contact(gerp_id, label, legal)
        return _json(200, {"gerp_id": gerp_id, "label": label, "legal": legal, "public": public})

    if method == "POST" and path == "/api/gerps/close":
        # Closing a gerp destroys it. The typed confirmation is required HERE and not only in the
        # dialog, because a string typed into a browser records nothing — the request carrying it
        # is the acknowledgement, the same reason `terms_version` is required on create.
        body = json.loads(event.get("body") or "{}")
        gerp_id = (body.get("gerp_id") or "").strip()
        if not gerp_id:
            return _json(400, {"error": "gerp_id required"})
        if (body.get("confirm") or "").strip() != CLOSE_PHRASE:
            return _json(400, {"error": f"type \u201c{CLOSE_PHRASE}\u201d to confirm"})
        gerp = next((g for g in _member_gerps(sub) if g["gerp_id"] == gerp_id), None)
        if not gerp:
            return _json(403, {"error": "not your gerp"})
        if gerp.get("status") in ("closing", "closed"):
            return _json(200, {"gerp_id": gerp_id, "status": gerp["status"],
                               "note": "already closing"})
        if gerp.get("status") == "stopped":
            # the closure build exports the live tables before it destroys, and a stopped gerp has
            # none; the operator starts it, then the close runs as any other
            return _json(409, {"gerp_id": gerp_id, "status": "stopped",
                               "error": "this gerp is stopped; ask the operator to start it, then close"})

        _aws("dynamodb").update_item(
            TableName=CUSTOMERS_TABLE,
            Key={"gerp_id": {"S": gerp_id}},
            UpdateExpression="SET #s = :s, close_requested_at = :t, close_requested_by = :b",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": "close_requested"},
                ":t": {"S": _iso_now()},
                ":b": {"S": sub},
            },
        )
        if not CLOSURE_BEGIN_FN:
            # recorded, nothing handed on, nothing destroyed
            log.info(f"closure disabled; {gerp_id} left at close_requested")
            return _json(202, {"gerp_id": gerp_id, "status": "close_requested",
                               "teardown": False})

        # The typed confirmation is the approval, so the script tags the case with this account
        # and goes straight to the build. From here the sequence is the one an unpaid invoice ends
        # in — one set of scripts, whichever reason started it.
        status, out = _invoke_seller_status(CLOSURE_BEGIN_FN, {
            "script": "closure/begin.py",
            "params": {"gerp_id": gerp_id, "requested_by": sub, "requested_at": _iso_now(),
                       "aws_account_id": gerp.get("aws_account_id", "")},
        })
        result = (out or {}).get("result") or out or {}
        log.info(f"closure handed to closure/begin.py for {gerp_id}: {status} {result}")
        if status >= 300:
            return _json(502, {"gerp_id": gerp_id, "status": "close_requested",
                               "error": (out or {}).get("error") or "closure could not be started"})
        return _json(202, {"gerp_id": gerp_id, "status": "closing",
                           "teardown": bool(result.get("teardown")),
                           "closes_on": result.get("closes_on", "")})

    if method == "POST" and path == "/api/billing/setup-link":
        # An Indian payer saves the card on our own card page, where a SetupIntent registers the
        # RBI e-mandate with it (Checkout has no card mandate options); every other payer goes to
        # Checkout. The answer is a url either way, so every caller opens it the same way.
        # Step one of saving a card: get a hosted Stripe page and send the browser there.
        # The card never reaches us — no Stripe.js in the SPA, no card fields, no number in
        # our DOM or logs. Stripe returns the browser to `return_url` with a session id.
        #
        # Both this and save-card invoke into the SELLER's account: gerp creation runs here,
        # in operator, and the card is saved before the buyer's own gerp exists. The callee
        # admits this role by name (modules/payments, billing_invoker_role_arn).
        body = json.loads(event.get("body") or "{}")
        gerp_id = (body.get("gerp_id") or "").strip()
        return_url = (body.get("return_url") or "").strip()
        if not return_url:
            return _json(400, {"error": "return_url required"})
        # Two subjects, one route. A gerp is what gets BILLED — its own AWS account, its own invoice
        # — so its card hangs off it. No gerp_id means the ACCOUNT's default: the card a gerp with
        # none of its own falls back to, and the only one settable without creating a gerp.
        subject = gerp_id or sub
        # the subject is the SESSION's, and whatever card completes the checkout is written onto it.
        # Unchecked, an authed caller could create a link against someone else's gerp and replace the
        # card their invoices are paid with. Their own account needs no check — it is theirs.
        if gerp_id and not any(g["gerp_id"] == gerp_id for g in _member_gerps(sub)):
            return _json(403, {"error": "not your gerp"})
        # the contact the seller creates on the card's arrival is named by the business and
        # carries the owner's legal name — the invoice bills the business and names the human
        # running it; the account's own card hangs off the person's contact, named by the login
        gerp = next((g for g in _member_gerps(sub) if g["gerp_id"] == gerp_id), None) if gerp_id else None
        legal = _legal_of(gerp_id) if gerp else {}
        # whose country decides the page: the business's for a gerp's card, the person's own for
        # the account card
        acct = {} if gerp else _get_account(sub)
        india = _is_india(legal.get("country") if gerp else acct.get("country"))
        out = _invoke_seller(SETUP_LINK_FN, {
            "kind": "setup", "contact_id": subject, "return_url": return_url, "email": _email(event),
            "name": (gerp or {}).get("label") or _email(event),
            # the legal business profile: its name is the party liable; a row from before it was
            # asked falls back to the owner's own name
            **({"legal_name": legal.get("name") or _legal_name(_get_account(sub))} if gerp else {}),
            **({"legal": legal} if legal else {}),
            **({"mandate": "india"} if india else {}),
            # the account card's Stripe customer takes the person's own name and address — the page
            # collects none, and Stripe Tax cannot place a customer without one
            **({"profile": {"name": _legal_name(acct), **{k: acct.get(k) or "" for k in
                                                          ("street", "unit", "city", "state", "zip", "country")}}}
               if india and not gerp else {}),
        })
        if india and out.get("client_secret"):
            # the secret rides the fragment, which no request carries to a server or a log
            out = {"url": "/card#" + urllib.parse.urlencode({"cs": out["client_secret"], "return": return_url}),
                   "setup_intent_id": out.get("setup_intent_id", "")}
        return _json(200 if out.get("url") else 502, out)

    if method == "POST" and path == "/api/billing/methods":
        # list / select / delete over the cards a payer saved. Same two subjects as setup-link: a
        # gerp is what gets billed, and no gerp_id means the ACCOUNT's own cards.
        body = json.loads(event.get("body") or "{}")
        gerp_id = (body.get("gerp_id") or "").strip()
        if gerp_id and not any(g["gerp_id"] == gerp_id for g in _member_gerps(sub)):
            return _json(403, {"error": "not your gerp"})
        subject = gerp_id or sub

        payload = {"op": (body.get("action") or "").strip(), "contact_id": subject,
                   "payment_method_id": (body.get("payment_method_id") or "").strip()}
        gerps = _member_gerps(sub)
        if gerp_id and payload["op"] == "select":
            # SELECT only. A gerp may be pointed at a card its account owner saved, and this is what
            # authorises it: membership was checked above, and the account's processor customer is
            # known only here. `list` needs nothing — the contact records both customers it can be
            # charged against, so the callee returns the union on its own.
            acct = _aws("dynamodb").get_item(
                TableName=ACCOUNTS_TABLE, Key={"account_id": {"S": sub}}).get("Item") or {}
            payload["from_customers"] = [c for c in
                                         [acct.get("stripe_customer_id", {}).get("S", "")] if c]
            # a gerp created against a card the account already holds has no contact yet — the
            # callee creates it, named by the business and carrying the owner's legal name
            gerp = next((g for g in gerps if g["gerp_id"] == gerp_id), {})
            payload["name"], payload["email"] = gerp.get("label") or gerp_id, _email(event)
            legal = _legal_of(gerp_id)
            payload["legal_name"] = legal.get("name") or _legal_name(_get_account(sub))
            if legal:
                payload["legal"] = legal
        else:
            # deleting an account card has to check every gerp billed to it. Which gerps exist is
            # known HERE and nowhere else, so the guard's scope is passed rather than inferred.
            payload["used_by"] = [g["gerp_id"] for g in gerps]

        status, out = _invoke_seller_status(CARD_METHODS_FN, payload)
        if gerp_id and payload["op"] == "select" and status == 200:
            # Selecting a card for a gerp that is waiting on one IS its payment step — the same
            # moment save-card provisions. The guard is the row's status, so a select on a gerp
            # already vended is a card change and vends nothing.
            _provision_after_card(gerp_id, out, sub)
        return _json(status, out)

    if method == "POST" and path == "/api/billing/save-card":
        # Step two: the browser is back from Stripe with a session id, and the SPA hands it
        # here. Authed, so this is not a public endpoint — Stripe's redirect carries no JWT,
        # which is why it lands on an SPA route rather than straight on the API.
        #
        # WHOSE card it is comes off the session's own metadata in the callee, not from this
        # request: the caller is a redirect the payer's browser followed, so anything it
        # carries is theirs to edit.
        body = json.loads(event.get("body") or "{}")
        session_id = (body.get("session_id") or "").strip()
        setup_intent_id = (body.get("setup_intent_id") or "").strip()
        if not session_id and not setup_intent_id:
            return _json(400, {"error": "session_id or setup_intent_id required"})
        # a Checkout session, or the card page's SetupIntent — the callee reads whose card off either
        out = _invoke_seller(SAVE_CARD_FN, {"session_id": session_id} if session_id else {"setup_intent_id": setup_intent_id})
        if out.get("stored"):
            # WHICH subject the session was for decides what happens next, and it comes off the
            # session's own metadata (via the callee), never off this request.
            if out["contact_id"] == sub:
                # the account's default — no gerp to vend, just a card now on file
                _save_account_card(sub, out)
                out["scope"] = "account"
            else:
                # a gerp's own card, and the card is what gates provisioning — so this is where the
                # sub-account gets vended, not on the create request, which ends at the redirect.
                out["scope"] = "gerp"
                _provision_after_card(out["contact_id"], out, sub)
        return _json(200 if out.get("stored") is not None else 502, out)

    if method == "GET" and path == "/api/public-user":
        # account-level: the caller's own public profile (email falls back to the token)
        pu = _get_public_user(sub)
        if not pu.get("email"):
            pu["email"] = _email(event)
        return _json(200, pu)

    if method == "POST" and path == "/api/public-user":
        # account-level: create/update the caller's own public profile (no gerp).
        body = json.loads(event.get("body") or "{}")
        if not (body.get("first") or "").strip() or not (body.get("last") or "").strip():
            return _json(400, {"error": "first and last name required"})
        # a published profile is a claim about a person that goes into the feed; the private
        # record is what stands behind the claim, and it is complete first
        missing = _account_missing(_get_account(sub))
        if missing:
            return _json(409, {"error": "complete your account first", "missing": missing})
        _put_public_user(sub, body)
        return _json(200, {"status": "saved", "gerp_profile_id": sub})

    if method == "GET" and path == "/api/places/autocomplete":
        # address typeahead (any authed user). auth gates it so geo-places cost isn't anonymous.
        q = ((event.get("queryStringParameters") or {}).get("q") or "").strip()
        if len(q) < 3:
            return _json(200, {"suggestions": []})
        try:
            return _json(200, {"suggestions": _places_autocomplete(q)})
        except Exception as e:  # noqa: BLE001
            log.exception("places autocomplete failed")
            return _json(502, {"error": f"autocomplete failed: {type(e).__name__}"})

    if method == "GET" and path == "/api/places/place":
        place_id = ((event.get("queryStringParameters") or {}).get("id") or "").strip()
        if not place_id:
            return _json(400, {"error": "id required"})
        try:
            return _json(200, _place_details(place_id))
        except Exception as e:  # noqa: BLE001
            log.exception("places lookup failed")
            return _json(502, {"error": f"lookup failed: {type(e).__name__}"})

    if method == "GET" and path == "/api/account":
        # account-level: the caller's own private profile. The row's email FOLLOWS the token: the
        # claim is the login Cognito verified, so a row that is missing is created from it and a
        # row that disagrees is updated to it. That is the write path for a changed login email —
        # the client never names an email, it refreshes its tokens and reads.
        acct = _get_account(sub)
        claim = _email(event)
        if claim and acct.get("email") != claim:
            old = acct.get("email") or ""
            _sync_account_email(sub, claim)
            acct["email"] = claim
            _post_customer_contact(sub)
            if old:
                # a change, not a first read of a row seeded without one
                _owner_email_changed(sub, old, claim)
        elif not acct.get("email"):
            acct["email"] = claim
        return _json(200, acct)

    if method == "POST" and path == "/api/account":
        # account-level: the caller edits their own name.
        body = json.loads(event.get("body") or "{}")
        first = (body.get("first") or "").strip()
        last = (body.get("last") or "").strip()
        if not first or not last:
            return _json(400, {"error": "first and last required"})
        # a partial save is accepted — the record is filled in steps — and the answer says what is
        # still missing, which is what the create and publish gates will refuse on
        _update_account(sub, {k: body.get(k) for k in ACCOUNT_FIELDS.values() if k in body})
        _post_customer_contact(sub)
        acct = _get_account(sub)
        return _json(200, {"status": "saved", "first": first, "last": last, "missing": acct["missing"]})

    if method == "DELETE" and path == "/api/account":
        # Deleting the account erases the person: every row that names them, in every place the
        # platform put one. Refused while a gerp is still running — a gerp is torn down by the
        # closure sequence, which has its own gate and its own notices, and an account does not
        # skip that by deleting itself. One write first (the priors), then the deletes, the Cognito
        # user last: the request's own token stays valid until it returns, and after this there is
        # no signing in to recreate a row from the claims. Every step is idempotent, so a request
        # that failed half-way runs again from the top.
        body = json.loads(event.get("body") or "{}")
        if (body.get("confirm") or "").strip() != DELETE_PHRASE:
            return _json(400, {"error": f"type \u201c{DELETE_PHRASE}\u201d to confirm"})
        gerps = _member_gerps(sub)
        # `awaiting_payment` never vended anything — the row is all there is, and it goes here
        live = [g for g in gerps if g["status"] not in ("closed", "awaiting_payment")]
        if live:
            return _json(409, {"error": "close your gerps first",
                               "gerps": [{"gerp_id": g["gerp_id"], "label": g["label"], "status": g["status"]}
                                         for g in live]})
        acct = _get_account(sub)
        email = acct.get("email") or _email(event)
        row = _aws("dynamodb").get_item(TableName=ACCOUNTS_TABLE, Key={"account_id": {"S": sub}}).get("Item") or {}

        # 1. the priors — from the row, the closed gerps and the saved cards, before anything goes
        fingerprints: list[str] = []
        if CARD_METHODS_FN:
            status, out = _invoke_seller_status(CARD_METHODS_FN, {"op": "list", "contact_id": sub})
            if status != 200:
                return _json(502, {"error": "could not read the saved cards; nothing was deleted",
                                   "detail": (out or {}).get("error", "")})
            fingerprints = sorted({m.get("fingerprint") for m in out.get("methods", []) if m.get("fingerprint")})
        keys = [f"card#{fp}" for fp in fingerprints]
        if email:
            keys.append(f"email#{_hash(email)}")
        if _phone_key(acct.get("phone")):
            keys.append(_phone_key(acct.get("phone")))
        endings = [{"gerp_id": g["gerp_id"], "how": g["closed_how"] or "requested",
                    "closed_at": g["closed_at"], "balance_owed": g["balance_owed"]}
                   for g in gerps if g["status"] == "closed"]
        _write_priors(keys, endings, row.get("stripe_customer_id", {}).get("S", ""))

        # 2–4. the private record, the memberships, the public profile
        ddb = _aws("dynamodb")
        ddb.delete_item(TableName=ACCOUNTS_TABLE, Key={"account_id": {"S": sub}})
        for g in gerps:
            ddb.delete_item(TableName=MEMBERS_TABLE, Key={"account_id": {"S": sub}, "gerp_id": {"S": g["gerp_id"]}})
            if g["status"] == "awaiting_payment":
                ddb.delete_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": g["gerp_id"]}})
        ddb.delete_item(TableName=PROFILES_TABLE, Key={"gerp_profile_id": {"S": sub}})

        # 5. the account's own Stripe customer — the seller's key, the seller's lambda
        if CARD_METHODS_FN:
            status, out = _invoke_seller_status(CARD_METHODS_FN, {"op": "forget", "contact_id": sub})
            if status != 200:
                return _json(502, {"error": "the saved card could not be forgotten; run the deletion again",
                                   "detail": (out or {}).get("error", "")})
        # 6. the seller's person contact, made a shell
        if not _post_customer_erase(sub):
            return _json(502, {"error": "the seller's record could not be erased; run the deletion again"})
        # 7. the Cognito user
        if USER_POOL_ID:
            try:
                _aws("cognito-idp").admin_delete_user(UserPoolId=USER_POOL_ID, Username=sub)
            except _aws("cognito-idp").exceptions.UserNotFoundException:
                pass
        log.info(f"account {sub} deleted: {len(keys)} priors, {len(gerps)} memberships")
        return _json(200, {"deleted": True, "priors": len(keys)})

    if method == "POST" and path == "/api/billing/pay":
        # Pay now on a hosting invoice the monthly charge missed. The customer paying IS the seller
        # charging the card the customer saved with it: the seller's `charge_saved_method` for the
        # oldest unpaid invoice, against whatever pair the gerp selects right now. A decline comes
        # back with its reason; a success settles through the ordinary webhook path and tower's
        # next daily read clears the row.
        body = json.loads(event.get("body") or "{}")
        gerp_id = (body.get("gerp_id") or "").strip()
        gerp = next((g for g in _member_gerps(sub) if g["gerp_id"] == gerp_id), None) if gerp_id else None
        if not gerp:
            return _json(403, {"error": "not your gerp"})
        unpaid = _unpaid(gerp.get("billing") or [])
        if not unpaid:
            return _json(409, {"error": "nothing is owed on this gerp"})
        status, out = _invoke_seller_status(CHARGE_FN, {"invoice_id": unpaid["invoice_id"]})
        return _json(status, {**out, "invoice_id": unpaid["invoice_id"]})

    if method == "POST" and path == "/api/billing/pay-link":
        # A hosted Checkout link for an invoice the caller owes — after the gerp is gone, or from
        # the refusal a prior raises on create. Only an invoice that is theirs: the closing invoice
        # of a gerp their priors name, or of a gerp they are a member of.
        body = json.loads(event.get("body") or "{}")
        invoice_id = (body.get("invoice_id") or "").strip()
        if not invoice_id:
            return _json(400, {"error": "invoice_id required"})
        acct = _get_account(sub)
        owed = set()
        for key in (f"email#{_hash(acct.get('email') or _email(event))}", _phone_key(acct.get("phone"))):
            owed.update(_owed_invoices(_prior(key)))
        for g in _member_gerps(sub):
            owed.update(b["invoice_id"] for b in g.get("billing") or [])
            row = _aws("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": g["gerp_id"]}}).get("Item") or {}
            if row.get("closed_invoice_id", {}).get("S"):
                owed.add(row["closed_invoice_id"]["S"])
        if invoice_id not in owed:
            return _json(403, {"error": "not an invoice you owe"})
        out = _invoke_seller(SETUP_LINK_FN, {"kind": "payment", "invoice_id": invoice_id})
        return _json(200 if out.get("url") else 502, out)

    if method == "GET" and path == "/api/regions":
        # the create screen's region dropdown: every region with its state, and the default for
        # a country (`?country=`)
        q = event.get("queryStringParameters") or {}
        return _json(200, {"regions": _regions_out(), "default": _region_for(q.get("country", ""))})
    if method == "GET" and path == "/api/gerp-config":
        # The whole gerp page in one object: operator-registry fields + a BFF-derived agent_email
        # (no gateway needed for the address) + the live status forwarded from the gerp's gateway.
        gerp_id = (event.get("queryStringParameters") or {}).get("gerp_id")
        target, err = _owned_target(sub, gerp_id)
        if err:
            return err
        cfg = {
            "gerp_id": gerp_id,
            "label": target.get("label", ""),
            "chat_url": target.get("chat_url", ""),
            "status": target.get("status", ""),
            # a closed gerp's screen shows when it closed and how long the export stays
            "closed_at": target.get("closed_at", ""),
            "download_until": target.get("download_until", ""),
            # matches modules/agent email.tf: subdomain replaces _ with -
            "agent_email": f"agent@{gerp_id.replace('_', '-')}.{AGENT_EMAIL_PARENT_DOMAIN}" if AGENT_EMAIL_PARENT_DOMAIN else "",
            "openly_operated": False,
            "agent_email_verified": False,
            "verify_recipient": "",
            "notification_email": "",
            "notification_email_verified": False,
            "instructions": [],
            "timezone": "UTC",
            "unpaid": _unpaid(target.get("billing") or []),
        }
        # the gerp's public profile row, if one exists: its industry, measured from what it sells
        biz = _aws("dynamodb").get_item(TableName=PROFILES_TABLE, Key={"gerp_profile_id": {"S": gerp_id}}).get("Item") or {}
        cfg["naics"] = _measured(biz, "naics", "revenue")
        cfg["naics_window"] = biz.get("naics_window", {}).get("S", "")
        # live status from the gerp's own gateway (best-effort — the static fields above stand alone).
        # A gerp with no stack behind it — closing, closed, stopped — has no gateway to ask
        status, text = ((0, "") if cfg["status"] in ("close_requested", "closing", "closed", "stopped") or not target.get("gateway_url")
                        else _forward("GET", target["gateway_url"], "/settings", headers.get("authorization", "")))
        if status == 200:
            try:
                s = json.loads(text)
                cfg["openly_operated"] = bool(s.get("openly_operated", False))
                cfg["agent_email_verified"] = bool(s.get("agent_email_verified", False))
                cfg["verify_recipient"] = s.get("verify_recipient", "")
                cfg["notification_email"] = s.get("notification_email", "")
                cfg["notification_email_verified"] = bool(s.get("notification_email_verified", False))
                cfg["instructions"] = s.get("instructions", []) or []
                cfg["timezone"] = s.get("timezone", "UTC") or "UTC"
            except json.JSONDecodeError:
                pass
        return _json(200, cfg)

    if method == "POST" and path == "/api/gerp-settings":
        # gerp-scoped: update the gerp's settings via its own gateway (PUT /settings). Forwards only
        # the fields present in the body — openly_operated (GERP scope), the caller's
        # notification_email (their USER row), and instruction / remove_instruction (the standing
        # instruction list); the gerp lambda upserts whichever it's given and returns the new view.
        body = json.loads(event.get("body") or "{}")
        target, err = _owned_target(sub, body.get("gerp_id"))
        if err:
            return err
        forward = {}
        if "openly_operated" in body:
            forward["openly_operated"] = bool(body["openly_operated"])
        if "notification_email" in body:
            forward["notification_email"] = body["notification_email"]
        if "timezone" in body:               # the business's clock (IANA name)
            forward["timezone"] = body["timezone"]
        if "instruction" in body:            # add one standing instruction
            forward["instruction"] = body["instruction"]
        if "remove_instruction" in body:     # the X on a row
            forward["remove_instruction"] = body["remove_instruction"]
        status, text = _forward(
            "PUT", target["gateway_url"], "/settings",
            headers.get("authorization", ""),
            json.dumps(forward).encode(),
        )
        return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": text}

    if method == "POST" and path.startswith("/api/automate/"):
        # The owner's web app running one of the gerp's published scripts. The path after
        # /api/automate/ is the published url, forwarded verbatim — the gerp's own
        # POST /automate/{proxy+} resolves it against automations/published/ and 404s if nothing
        # is published there. Everything but gerp_id is the caller's arguments; a published record
        # decides which of them the script actually accepts, so nothing is enumerated here.
        published = path[len("/api/automate/"):]
        if not published:
            return _json(404, {"error": "not found"})
        body = json.loads(event.get("body") or "{}")
        target, err = _owned_target(sub, body.get("gerp_id"))
        if err:
            return err
        status, text = _forward(
            "POST", target["gateway_url"], f"/automate/{published}",
            headers.get("authorization", ""),
            json.dumps({k: v for k, v in body.items() if k != "gerp_id"}).encode(),
        )
        return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": text}

    return _json(404, {"error": "not found"})
