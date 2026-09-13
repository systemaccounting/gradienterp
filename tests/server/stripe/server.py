"""A Stripe stand-in for local dev: the paths our lambdas call, a hosted checkout page, and the
webhook a charge produces. In memory; `--restart` wipes it.

    :4242   uvicorn tests.server.stripe.server:app

Exactly the surface `payment_links`, `save_payment_method`, `manage_saved_cards` and
`charge_saved_method` use, and nothing else — a request to a path Stripe has and this does not is a
404 that says so. Bodies arrive form-encoded with bracket keys (`metadata[contact_id]`), the way
Stripe takes them; responses are the JSON fields the lambdas read.

The hosted page is the point. `GET /c/{session}` is a form; submitting it creates the payment method
on the session's customer, completes the setup intent and the session, and sends the browser to the
session's `success_url` — :3000, where the SPA already calls `save-card`. The number typed decides
the card: 4… Visa, 5… Mastercard, 3… Amex; a number ending 0002 saves but declines when charged.

A charge posts `charge.succeeded` to `$LOCAL_GATEWAY_URL/webhooks/stripe`, signed with the local
signing secret, so `ingest_stripe` runs its signature check and the invoice moves to `paid` the way
it does in production.
"""

import hashlib
import hmac
import json
import os
import pathlib
import secrets as _secrets
import threading
import time
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

REPO = pathlib.Path(__file__).resolve().parents[3]
SIGNING_SECRET = "whsec_local"
WEBHOOK_URL = os.environ.get("LOCAL_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/") + "/webhooks/stripe"

app = FastAPI(title="stripe · local stand-in")

DB: dict = {"customers": {}, "sessions": {}, "setup_intents": {}, "payment_methods": {},
            "payment_intents": {}, "idempotency": {}}


def _id(prefix):
    return f"{prefix}_{_secrets.token_hex(12)}"


def _form(raw: bytes) -> dict:
    """`metadata[contact_id]=x` → {"metadata": {"contact_id": "x"}}."""
    out: dict = {}
    for k, vs in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items():
        v = vs[-1]
        if "[" in k and k.endswith("]"):
            head, sub = k[:-1].split("[", 1)
            out.setdefault(head, {})[sub] = v
        else:
            out[k] = v
    return out


def _not_found(what):
    return JSONResponse({"error": {"type": "invalid_request_error", "message": f"No such {what}"}}, 404)


# ── customers ──

@app.post("/v1/customers")
async def create_customer(request: Request):
    body = _form(await request.body())
    c = {"id": _id("cus"), "object": "customer", "email": body.get("email"),
         "name": body.get("name"), "metadata": body.get("metadata") or {}, "created": int(time.time())}
    DB["customers"][c["id"]] = c
    return c


@app.delete("/v1/customers/{customer_id}")
def delete_customer(customer_id: str):
    """Deleting a customer detaches every method on it — the cards go with the customer."""
    c = DB["customers"].pop(customer_id, None)
    if not c:
        return _not_found("customer")
    for pm in DB["payment_methods"].values():
        if pm.get("customer") == customer_id:
            pm["customer"] = None
    return {"id": customer_id, "object": "customer", "deleted": True}


@app.get("/v1/customers/{customer_id}/payment_methods")
def list_payment_methods(customer_id: str):
    pms = [pm for pm in DB["payment_methods"].values() if pm.get("customer") == customer_id]
    pms.sort(key=lambda pm: pm["created"], reverse=True)
    return {"object": "list", "data": pms, "has_more": False}


# ── checkout sessions (setup mode) + the hosted page ──

@app.post("/v1/checkout/sessions")
async def create_session(request: Request):
    body = _form(await request.body())
    if body.get("mode") != "setup":
        return JSONResponse({"error": {"message": "this stand-in models mode=setup only"}}, 400)
    if body.get("customer") not in DB["customers"]:
        return _not_found("customer")
    sid = _id("cs")
    seti = {"id": _id("seti"), "object": "setup_intent", "status": "requires_payment_method",
            "customer": body["customer"], "payment_method": None, "metadata": body.get("metadata") or {}}
    DB["setup_intents"][seti["id"]] = seti
    s = {"id": sid, "object": "checkout.session", "mode": "setup", "status": "open",
         "customer": body["customer"], "setup_intent": seti["id"],
         "success_url": (body.get("success_url") or "").replace("{CHECKOUT_SESSION_ID}", sid),
         "cancel_url": body.get("cancel_url") or "", "metadata": body.get("metadata") or {},
         "url": f"{str(request.base_url).rstrip('/')}/c/{sid}", "created": int(time.time())}
    DB["sessions"][sid] = s
    return s


@app.get("/v1/checkout/sessions/{session_id}")
def get_session(session_id: str):
    return DB["sessions"].get(session_id) or _not_found("checkout.session")


@app.get("/c/{session_id}", response_class=HTMLResponse)
def hosted_page(session_id: str):
    s = DB["sessions"].get(session_id)
    if not s:
        return HTMLResponse("<h1>No such session</h1>", 404)
    cust = DB["customers"].get(s["customer"]) or {}
    return f"""<!doctype html><title>local stripe</title>
<style>body{{font:15px system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem}}
input{{display:block;width:100%;margin:.4rem 0 1rem;padding:.6rem;font-size:1rem}}
button{{padding:.7rem 1.2rem;font-size:1rem}}small{{color:#666}}</style>
<h2>Save a card <small>(local stand-in)</small></h2>
<p><small>customer {s['customer']} · {cust.get('email') or 'no email'}</small></p>
<form method="post" action="/c/{session_id}">
<label>Card number<input name="number" value="4242 4242 4242 4242" autofocus></label>
<label>Expiry (MM/YY)<input name="exp" value="12/30"></label>
<button>Save card</button>
<a href="{s['cancel_url'] or '/'}" style="margin-left:1rem">Cancel</a>
</form>
<p><small>4… Visa · 5… Mastercard · 3… Amex · a number ending 0002 saves but declines when charged</small></p>"""


@app.post("/c/{session_id}")
async def hosted_submit(session_id: str, request: Request):
    s = DB["sessions"].get(session_id)
    if not s:
        return HTMLResponse("<h1>No such session</h1>", 404)
    body = _form(await request.body())
    number = "".join(ch for ch in (body.get("number") or "") if ch.isdigit()) or "4242424242424242"
    exp = (body.get("exp") or "12/30").split("/")
    brand = {"4": "visa", "5": "mastercard", "3": "amex"}.get(number[0], "visa")
    import hashlib
    pm = {"id": _id("pm"), "object": "payment_method", "type": "card", "customer": s["customer"],
          "card": {"brand": brand, "last4": number[-4:],
                   "exp_month": int(exp[0] or 12), "exp_year": 2000 + int((exp[1] if len(exp) > 1 else "30")[-2:]),
                   # one value per card NUMBER across customers, the way Stripe's is
                   "fingerprint": hashlib.sha256(number.encode()).hexdigest()[:16]},
          "created": int(time.time()), "declines": number.endswith("0002")}
    DB["payment_methods"][pm["id"]] = pm
    seti = DB["setup_intents"][s["setup_intent"]]
    seti.update({"status": "succeeded", "payment_method": pm["id"]})
    s["status"] = "complete"
    return RedirectResponse(s["success_url"] or s["cancel_url"] or "/", status_code=303)


# ── setup intents, payment methods ──

@app.get("/v1/setup_intents/{seti_id}")
def get_setup_intent(seti_id: str):
    return DB["setup_intents"].get(seti_id) or _not_found("setup_intent")


@app.get("/v1/payment_methods/{pm_id}")
def get_payment_method(pm_id: str):
    return DB["payment_methods"].get(pm_id) or _not_found("payment_method")


@app.post("/v1/payment_methods/{pm_id}/detach")
def detach(pm_id: str):
    pm = DB["payment_methods"].get(pm_id)
    if not pm:
        return _not_found("payment_method")
    pm["customer"] = None
    return pm


# ── charges ──

@app.post("/v1/tax/calculations")
async def create_tax_calculation(request: Request):
    """Stripe Tax, as far as a charge needs it: zero tax, or `LOCAL_STRIPE_TAX_RATE` (e.g. 0.0725)
    applied to every line — the shape the adapter reads (`amount_total`, `tax_amount_exclusive`)."""
    raw = await request.body()
    body = _form(raw)
    if body.get("customer") not in DB["customers"]:
        return _not_found("customer")
    rate = float(os.environ.get("LOCAL_STRIPE_TAX_RATE") or 0)
    # `line_items[0][amount]` is two brackets deep — past what _form nests — so read the amounts
    # off the raw form
    import re
    from urllib.parse import parse_qs
    subtotal = sum(int(v[0]) for k, v in parse_qs(raw.decode()).items()
                   if re.fullmatch(r"line_items\[\d+\]\[amount\]", k))
    tax = int(round(subtotal * rate))
    return {"id": _id("taxcalc"), "object": "tax.calculation", "currency": body.get("currency", "usd"),
            "customer": body["customer"], "amount_total": subtotal + tax, "tax_amount_exclusive": tax,
            "tax_amount_inclusive": 0, "tax_breakdown": [], "expires_at": int(time.time()) + 90 * 86400}


@app.post("/v1/payment_intents")
async def create_payment_intent(request: Request):
    key = request.headers.get("idempotency-key")
    if key and key in DB["idempotency"]:
        return DB["idempotency"][key]
    body = _form(await request.body())
    pm = DB["payment_methods"].get(body.get("payment_method") or "")
    if not pm or pm.get("customer") != body.get("customer"):
        out = JSONResponse({"error": {"type": "invalid_request_error", "code": "resource_missing",
                                      "message": "No such payment_method on this customer"}}, 400)
    elif pm.get("declines"):
        out = JSONResponse({"error": {"type": "card_error", "code": "card_declined",
                                      "decline_code": "generic_decline", "message": "Your card was declined."}}, 402)
    else:
        pi = {"id": _id("pi"), "object": "payment_intent", "status": "succeeded",
              "amount": int(body.get("amount") or 0), "currency": body.get("currency") or "usd",
              "customer": body["customer"], "payment_method": pm["id"], "latest_charge": _id("ch"),
              "metadata": body.get("metadata") or {}, "created": int(time.time())}
        DB["payment_intents"][pi["id"]] = pi
        threading.Thread(target=_webhook_charge_succeeded, args=(pi,), daemon=True).start()
        out = JSONResponse(pi)
    if key:
        DB["idempotency"][key] = out
    return out


def _webhook_charge_succeeded(pi: dict):
    """What Stripe would send a moment later. The template is the recorded event under testdata,
    with this charge's ids, amount and metadata in place of the recording's."""
    time.sleep(0.2)
    template = json.loads((REPO / "tests/testdata/stripe/charge.succeeded.json").read_text())
    obj = template["data"]["object"]
    obj.update({"id": pi["latest_charge"], "payment_intent": pi["id"], "customer": pi["customer"],
                "amount": pi["amount"], "amount_captured": pi["amount"], "currency": pi["currency"],
                "metadata": pi["metadata"], "payment_method": pi["payment_method"],
                "created": pi["created"], "livemode": False})
    template.update({"id": _id("evt"), "created": pi["created"], "livemode": False})
    payload = json.dumps(template).encode()
    ts = int(time.time())
    sig = hmac.new(SIGNING_SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    req = urllib.request.Request(WEBHOOK_URL, data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Stripe-Signature": f"t={ts},v1={sig}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[stripe] charge.succeeded → {WEBHOOK_URL} {r.status}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[stripe] charge.succeeded → {WEBHOOK_URL} failed: {e}", flush=True)


@app.get("/")
def root():
    return {"stand_in": "stripe", "customers": len(DB["customers"]),
            "payment_methods": len(DB["payment_methods"]), "webhook_url": WEBHOOK_URL}


@app.api_route("/v1/{rest:path}", methods=["GET", "POST", "DELETE"])
def unmodelled(rest: str):
    return JSONResponse({"error": {"message": f"/v1/{rest} is a Stripe path this stand-in does not model "
                                              "(tests/server/stripe/server.py)"}}, 404)
