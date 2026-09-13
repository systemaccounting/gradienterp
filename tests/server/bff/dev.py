"""`/dev/*` — put the local stack in a state, by URL. Local server only; the prod BFF has none of
these. `GET /dev` is the manual: it renders from this router, so it cannot drift from the verbs.

A spec, a person and an agent use the same verbs (the e2e helpers' local branches call these), so
there is one implementation of "an account with a card" rather than three.
"""

import base64
import json
import time
import urllib.request
import uuid

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/dev")

# the record the create and publish gates require — the same one the e2e fixtures carry
FULL_RECORD = {"first": "Ada", "last": "Lovelace", "phone": "+1 555 0100", "street": "1 Analytical Way",
               "city": "London", "state": "LDN", "zip": "N1", "country": "GB"}
RECORD_COLUMNS = ["first_name", "middle_name", "last_name", "phone", "street", "unit", "city", "state", "zip", "country"]
VERBS = []   # (method, path, what) — the index


def verb(method, path, what):
    def deco(fn):
        VERBS.append((method, path, what))
        return getattr(router, method.lower())(path)(fn)
    return deco


def _ddb():
    from aws import client
    return client("dynamodb")


def _self(request: Request, sub: str, method: str, path: str, body=None):
    """The BFF's own routes, as the signed-in account would call them."""
    req = urllib.request.Request(str(request.base_url).rstrip("/") + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "x-debug-sub": sub}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _token(sub: str, email: str) -> str:
    b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': sub, 'email': email, 'token_use': 'id', 'exp': int(time.time()) + 86400})}.local"


@router.get("")
def index(request: Request):
    base = str(request.base_url).rstrip("/")
    lines = ["the local stack, put into a state by url. every verb answers with what it did.", ""]
    for method, path, what in VERBS:
        lines.append(f"{method:6} {base}/dev{path:32} {what}")
    lines += ["", "signing in: open the login url in a browser. the rest are curls:",
              f"  curl -X POST {base}/dev/account/local-dev/complete",
              f"  curl -X POST {base}/dev/account/local-dev/card -d '{{\"number\": \"4242\"}}'",
              "", "the stack is moto: --restart wipes everything, which is the way to start clean."]
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@verb("GET", "/login", "a page that signs the browser in as {sub} and goes to /")
def login(sub: str, email: str = ""):  # every verb is sync on purpose: the self-calls need the loop free
    tok = _token(sub, email or f"{sub}@localhost")
    return HTMLResponse(f"<script>sessionStorage.setItem('id_token', {json.dumps(tok)}); location.replace('/');</script>")


@verb("POST", "/account", "a fresh account row: {{sub?, email?}} → the sub and email it has")
def create_account(request: Request, body: dict = Body(default={})):
    sub = body.get("sub") or str(uuid.uuid4())
    email = body.get("email") or f"{body.get('tag', 'dev')}+{sub[:8]}@localhost"
    _ddb().put_item(TableName="gerp-accounts", Item={"account_id": {"S": sub}, "email": {"S": email}})
    return {"sub": sub, "email": email}


@verb("GET", "/account/{sub}", "the row, and what is missing")
def show_account(request: Request, sub: str):
    out = _self(request, sub, "GET", "/api/account")[1]
    # the seller gerp's side of the record: the contact its customers/upsert hook wrote
    row = _ddb().get_item(TableName="gerp-contacts-gradienterp", Key={"contact_id": {"S": sub}}).get("Item")
    out["contact"] = {k: v.get("S", v.get("BOOL")) for k, v in row.items() if "S" in v or "BOOL" in v} if row else None
    return JSONResponse(out)


@verb("POST", "/account/{sub}/complete", "the full record, so the account can create and publish")
def complete_account(request: Request, sub: str):
    status, out = _self(request, sub, "POST", "/api/account", FULL_RECORD)
    return JSONResponse({"status": status, **out, "record": FULL_RECORD})


@verb("POST", "/account/{sub}/wipe", "the record cleared; the email stays")
def wipe_account(sub: str):
    _ddb().update_item(TableName="gerp-accounts", Key={"account_id": {"S": sub}},
                       UpdateExpression="REMOVE " + ", ".join(f"#{c}" for c in RECORD_COLUMNS),
                       ExpressionAttributeNames={f"#{c}": c for c in RECORD_COLUMNS})
    return {"sub": sub, "wiped": RECORD_COLUMNS}


@verb("POST", "/account/{sub}/card", "{{number?}} — a card saved through the Stripe stand-in, end to end")
def card(request: Request, sub: str, body: dict = Body(default={})):
    number = str(body.get("number") or "4242")
    number = {"4242": "4242 4242 4242 4242", "4444": "5555 5555 5555 4444", "0002": "4000 0000 0000 0002"}.get(number, number)
    status, out = _self(request, sub, "POST", "/api/billing/setup-link", {"return_url": str(request.base_url) + "?billing=account"})
    if status != 200:
        return JSONResponse({"step": "setup-link", "status": status, **out}, 502)
    import urllib.parse
    form = urllib.parse.urlencode({"number": number, "exp": "12/30"}).encode()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    # the stand-in answers the form with a 303 to the return url; with redirects off, urllib
    # raises that as an HTTPError, and the Location is on the error
    import urllib.error
    try:
        r = urllib.request.build_opener(NoRedirect).open(
            urllib.request.Request(out["url"], data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))
        loc = r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "") if e.code in (301, 302, 303) else ""
    session_id = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("session_id", [""])[0]
    status, saved = _self(request, sub, "POST", "/api/billing/save-card", {"session_id": session_id})
    return JSONResponse({"step": "save-card", "status": status, **saved}, 200 if status == 200 else 502)


@verb("DELETE", "/account/{sub}", "the account deleted through the route — priors, rows, card, contact")
def delete_account(request: Request, sub: str):
    """`DELETE /api/account` as the account itself, phrase and all. A 409 (a gerp still running)
    comes back as the route's own answer."""
    status, out = _self(request, sub, "DELETE", "/api/account", {"confirm": "delete my account"})
    return JSONResponse({"sub": sub, "status": status, **out}, status)


@verb("GET", "/priors", "what closed accounts left behind (gerp-priors)")
def priors():
    items = _ddb().scan(TableName="gerp-priors").get("Items", [])
    flat = lambda it: {k: next(iter(v.values())) for k, v in it.items() if "S" in v or "N" in v}  # noqa: E731
    return {"priors": [flat(it) for it in items]}


@verb("POST", "/gerp/{sub}", "{{label, gerp_id?, status?, gateway_url?, download_until?}} — a gerp row owned by {sub}")
def seed_gerp(request: Request, sub: str, body: dict = Body(default={})):
    label = body.get("label") or "Dev Co"
    gid = body.get("gerp_id") or (label.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:6])
    item = {"gerp_id": {"S": gid}, "owner_sub": {"S": sub}, "label": {"S": label}}
    for k in ("status", "gateway_url", "chat_url", "aws_account_id", "download_until"):
        if body.get(k):
            item[k] = {"S": body[k]}
    ddb = _ddb()
    ddb.put_item(TableName="gerp-customers", Item=item)
    ddb.put_item(TableName="gerp-members", Item={"account_id": {"S": sub}, "gerp_id": {"S": gid}, "role": {"S": "owner"}})
    return {"gerp_id": gid, "label": label, "owner": sub, "status": body.get("status", "")}


@verb("POST", "/gerp/{gerp_id}/unpaid", "{{total?, period?}} — a hosting invoice the monthly charge missed, on the row")
def unpaid_gerp(gerp_id: str, body: dict = Body(default={})):
    """What tower's daily read stamps when the seller's invoice is unpaid: the `billing` entry with
    `unpaid_at`. The invoice itself is not created here — the screen reads the row."""
    period = body.get("period") or "2026-08"
    entry = {"M": {"invoice_id": {"S": f"hosting-{gerp_id}-{period}"}, "total": {"N": str(body.get("total") or 12.5)},
                   "period": {"S": period}, "issued_at": {"S": "2026-09-01T00:00:00Z"},
                   "unpaid_at": {"S": str(int((time.time() - 4 * 86400) * 1000))}}}
    _ddb().update_item(TableName="gerp-customers", Key={"gerp_id": {"S": gerp_id}},
                       UpdateExpression="SET billing = :b", ExpressionAttributeValues={":b": {"L": [entry]}})
    return {"gerp_id": gerp_id, "unpaid": entry["M"]["invoice_id"]["S"], "total": body.get("total") or 12.5}


@verb("GET", "/gerp/{gerp_id}", "the customer row + the seller's contact for it")
def show_gerp(gerp_id: str):
    ddb = _ddb()
    flat = lambda it: {k: next(iter(v.values())) for k, v in (it or {}).items()}  # noqa: E731
    row = flat(ddb.get_item(TableName="gerp-customers", Key={"gerp_id": {"S": gerp_id}}).get("Item"))
    try:
        contact = flat(ddb.get_item(TableName="gerp-contacts-gradienterp", Key={"contact_id": {"S": gerp_id}}).get("Item"))
    except Exception:  # noqa: BLE001
        contact = {}
    return {"gerp": row, "seller_contact": contact}
