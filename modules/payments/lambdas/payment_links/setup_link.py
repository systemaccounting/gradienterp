"""create_setup_link — a hosted page where a customer saves a card, without us seeing it.

A Checkout Session in `setup` mode. Stripe hosts the form, takes the card, and sends the browser
back; the card number never enters our DOM, our logs or our process. That is the whole reason for
hosted checkout over Stripe Elements — no Stripe.js in the SPA, no card fields to style or maintain,
and the same shape exists at PayPal and Square when those are needed.

Nothing is charged here. `setup` mode saves a payment method for LATER use, which is what an
off-session charge needs (`charge_saved_method`). Saving a card and being allowed to charge it are
different things: the card is the instrument, and the rule instance on `INVOICE_STATUS#<status>` is
the permission.

    in    {contact_id, return_url, name?, email?, legal?, mandate?}
    out   {url, session_id}                          — Checkout, every payer but one
          {setup_intent_id, client_secret}           — `mandate: "india"`: a SetupIntent registering
                                                       the e-mandate, confirmed on our card page

**This does not store anything.** The two values the session produces — a Stripe customer id
(`cus_…`) and a payment method id (`pm_…`) — are written by the route the browser RETURNS to, which
reads the session and puts them on the contact. Not by the webhook: `ingest_stripe` dispatches on
`transform_stripe_<event>` and every one of those turns an event into debits and credits, but a
saved card moves no money and posts no entry, so `checkout.session.completed` would dead-letter as
"no transform".

**One Stripe customer per contact, reused.** A customer holds every card its payer saved, so the
contact's `stripe_own_customer_id` is read first and a new customer is created only for a payer nothing
knows yet. A second customer for the same payer would strand the first card: a payment method cannot
be moved between customers and detaching one is terminal, so the card could never be listed,
selected or charged again.

The key needs `Checkout Sessions: write`, `Customers: write` and, for the India page,
`Setup Intents: write`. The `stripe_setup` restricted key
used by `configure_webhook` is scoped to Webhook Endpoints and will 403 here.

Design: modules/payments/AGENTS.md § connecting a processor.
"""

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request

import stripe_api

from aws import client as _aws, log as alog, Failure
from _kinds import STRIPE_CALL_FAILED, CONTACT_READ_FAILED

log = logging.getLogger()
log.setLevel(logging.INFO)

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)
STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
# the owner-submitted key that can create sessions and customers. Named separately from
# `stripe_setup` because that one is scoped to webhook endpoints and discarded after use;
# this one persists and can reach a customer's saved card.
STRIPE_KEY_SECRET = os.environ.get("STRIPE_KEY_SECRET", "stripe_billing")
# `setup` mode has no line items, so Stripe cannot infer a currency and rejects the session
# without one. It only fixes what the saved card will later be charged in.
BILLING_CURRENCY = os.environ.get("BILLING_CURRENCY", "usd")
CONTACTS_GET_FN = os.environ.get("CONTACTS_GET_FN", "")


def _read_secret(name):
    from botocore.exceptions import ClientError
    try:
        return _aws("ssm").get_parameter(
            Name=f"{SECRET_PARAM_PREFIX}/{name}", WithDecryption=True
        )["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def _post(api_key, path, body):
    req = urllib.request.Request(
        f"{STRIPE_API_BASE}{path}",
        data=urllib.parse.urlencode(body, doseq=True).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Stripe puts the reason in the BODY; urllib raises before anyone reads it, so a
        # validation error arrives as a bare stack trace naming the line, not the problem.
        err = (json.loads(e.read() or b"{}").get("error") or {})
        raise Failure(STRIPE_CALL_FAILED, path=path, status=e.code, error=err.get("message", ""),
                      code=err.get("code", ""), param=err.get("param", "")) from None


def _contact(contact_id):
    """The payer's contact row, or `{}` when nothing knows them yet.

    A 404 is the ordinary first-time case and means create. Any other failure RAISES rather than
    falling through to create: a transient read error would silently make a second customer, and
    that strands whatever card the payer already saved.
    """
    if not CONTACTS_GET_FN:
        return {}
    resp = _aws("lambda").invoke(
        FunctionName=CONTACTS_GET_FN, Payload=json.dumps({"op": "get", "contact_id": contact_id}).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    if out.get("statusCode") == 404:
        return {}
    if out.get("statusCode") != 200:
        raise Failure(CONTACT_READ_FAILED, contact_id=contact_id, status=out.get("statusCode"), error=out.get("body"))
    return json.loads(out["body"]).get("contact") or {}


def _ok(body, code=200):
    return {"statusCode": code, "body": json.dumps(body)}


# India's e-mandate: the most one monthly charge may be under it, in the billing currency's minor
# unit. A charge above it — or above ₹15,000 whatever the mandate says — is still made, and the
# cardholder authenticates it through the bank's pre-debit notice.
INDIA_MANDATE_MAX = int(os.environ.get("INDIA_MANDATE_MAX", "100000"))
INDIA_MANDATE_DESCRIPTION = "gradientERP hosting, billed monthly at AWS cost plus 20%"
_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _get(api_key, path):
    req = urllib.request.Request(f"{STRIPE_API_BASE}{path}")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = (json.loads(e.read() or b"{}").get("error") or {})
        raise Failure(STRIPE_CALL_FAILED, path=path, status=e.code, error=err.get("message", ""),
                      code=err.get("code", ""), param=err.get("param", "")) from None


def _iso2(country):
    """The ISO 3166 alpha-2 code Stripe wants: a code as given, India by its name, else nothing."""
    c = (country or "").strip().upper()
    if c in ("INDIA", "IND"):
        return "IN"
    return c if len(c) == 2 else ""


def _customer_profile(legal):
    """The Stripe customer's name and address off the legal business profile — what Checkout's
    `customer_update` writes on every other path, and what Stripe Tax reads (the Indian state
    among it)."""
    legal = legal or {}
    fields = [(f"address[{stripe_key}]", legal[ours]) for ours, stripe_key in
              (("street", "line1"), ("unit", "line2"), ("city", "city"), ("state", "state"),
               ("zip", "postal_code")) if legal.get(ours)]
    # Stripe takes the two-letter code; the create form writes the name Places returns ("India"),
    # and Stripe refuses a name outright
    country = _iso2(legal.get("country"))
    if country:
        fields.append(("address[country]", country))
    if legal.get("name"):
        fields.append(("name", legal["name"]))
    return fields


def _add_gstin(api_key, customer_id, gstin):
    """The business's GSTIN as an `in_gst` tax id on the customer, once: the list is read first and
    a value already there is not added again. With it the sale is reverse charge; without, IGST."""
    gstin = (gstin or "").strip().upper()
    if not gstin:
        return
    existing = _get(api_key, f"/v1/customers/{customer_id}/tax_ids").get("data") or []
    if any(t.get("type") == "in_gst" and t.get("value") == gstin for t in existing):
        return
    _post(api_key, f"/v1/customers/{customer_id}/tax_ids", [("type", "in_gst"), ("value", gstin)])


def _india_setup_intent(api_key, body, contact_id, customer_id):
    """A SetupIntent registering the India e-mandate with the card. Checkout cannot: its card
    options carry no `mandate_options`. The card page confirms it in the payer's browser; the
    contact and the profile ride its metadata exactly as they ride a Checkout session's."""
    now = int(time.time())
    legal = body.get("legal") or {}
    # a gerp's card: the business's profile; the account card: the person's own (`profile`)
    profile = _customer_profile(body.get("profile") or legal)
    if profile:
        _post(api_key, f"/v1/customers/{customer_id}", profile)
    _add_gstin(api_key, customer_id, legal.get("tax_id"))
    return _post(api_key, "/v1/setup_intents", [
        ("customer", customer_id),
        ("usage", "off_session"),
        ("payment_method_types[]", "card"),
        ("payment_method_options[card][mandate_options][amount]", str(INDIA_MANDATE_MAX)),
        ("payment_method_options[card][mandate_options][amount_type]", "maximum"),
        ("payment_method_options[card][mandate_options][currency]", BILLING_CURRENCY),
        ("payment_method_options[card][mandate_options][interval]", "month"),
        ("payment_method_options[card][mandate_options][interval_count]", "1"),
        # unique per mandate, as Stripe asks: a payer who saves a second card registers a second one
        ("payment_method_options[card][mandate_options][reference]", f"gerp-{contact_id}"[:69] + f"-{now}"),
        ("payment_method_options[card][mandate_options][start_date]", str(now)),
        ("payment_method_options[card][mandate_options][description]", INDIA_MANDATE_DESCRIPTION),
        ("payment_method_options[card][mandate_options][supported_types][]", "india"),
        ("metadata[contact_id]", contact_id),
        *([("metadata[name]", body["name"])] if body.get("name") else []),
        *([("metadata[legal_name]", body["legal_name"])] if body.get("legal_name") else []),
        *_legal_metadata(legal),
    ])


def _legal_metadata(legal):
    if not legal:
        return []
    contact = {k: legal[k] for k in ("name", "email", "phone", "tax_id") if legal.get(k)}
    address = {k: legal[k] for k in ("street", "unit", "city", "state", "zip", "country") if legal.get(k)}
    return [("metadata[legal]", json.dumps(contact)), ("metadata[legal_address]", json.dumps(address))]


def handler(event, context):
    body = event if isinstance(event, dict) and "contact_id" in event else json.loads(
        event.get("body") or "{}"
    )
    contact_id = (body.get("contact_id") or "").strip()
    return_url = (body.get("return_url") or "").strip()
    mandate = (body.get("mandate") or "").strip()
    if not contact_id or (not return_url and not mandate):
        return _ok({"error": "contact_id and return_url required"}, 400)
    if mandate and mandate != "india":
        return _ok({"error": "mandate must be india or absent"}, 400)
    tax_id = ((body.get("legal") or {}).get("tax_id") or "").strip().upper()
    if mandate and tax_id and not _GSTIN.match(tax_id):
        return _ok({"error": "the GSTIN is 15 characters: 2 digits, the PAN, an entity digit, Z, a check character",
                    "field": "tax_id"}, 400)

    try:
        api_key = _read_secret(STRIPE_KEY_SECRET)
        if not api_key:
            return _ok({"error": f"no '{STRIPE_KEY_SECRET}' secret; needs Checkout Sessions: write "
                                 "and Customers: write"}, 400)

        # One customer per contact, holding every card that payer saves. `metadata[contact_id]` makes
        # the customer legible in Stripe's dashboard; what actually keeps it stable is reading the id
        # back off the contact, since POST creates and Stripe has no upsert on metadata.
        # Their OWN customer, not `stripe_customer_id` — that one holds whichever customer the SELECTED
        # method belongs to, and after a `select` it can be someone else's. Reusing it here would attach
        # this payer's new card to that other payer. Falls back for contacts written before the split.
        _c = _contact(contact_id)
        customer_id = _c.get("stripe_own_customer_id") or _c.get("stripe_customer_id")
        if not customer_id:
            customer_id = _post(api_key, "/v1/customers", [
                ("metadata[contact_id]", contact_id),
                *( [("email", body["email"])] if body.get("email") else [] ),
            ])["id"]

        if mandate == "india":
            intent = _india_setup_intent(api_key, body, contact_id, customer_id)
            log.info(f"india setup intent {intent['id']} for contact {contact_id}")
            return _ok({"setup_intent_id": intent["id"], "client_secret": intent["client_secret"],
                        "stripe_customer_id": customer_id})

        # `session_id` rides the return url so the landing route can read the session back and
        # learn which payment method got saved — the session is the only thing that knows.
        joiner = "&" if "?" in return_url else "?"
        session = _post(api_key, "/v1/checkout/sessions", [
            ("mode", "setup"),
            ("currency", BILLING_CURRENCY),
            ("customer", customer_id),
            ("success_url", f"{return_url}{joiner}session_id={{CHECKOUT_SESSION_ID}}"),
            ("cancel_url", return_url),
            # the billing address, once, onto the Stripe customer: sales tax on what this card pays
            # for is computed from it, and no table of ours holds a business address
            ("billing_address_collection", "required"),
            ("customer_update[address]", "auto"),
            # the buyer's VAT id, when they have one, onto the Stripe customer: Stripe Tax then
            # applies the reverse charge on what this card pays for. Collecting it on a customer
            # that already exists (a second setup for the same contact) needs the name update too
            ("customer_update[name]", "auto"),
            ("tax_id_collection[enabled]", "true"),
            ("metadata[contact_id]", contact_id),
            # the buyer has no contact row yet — this is the first thing we ever knew about them,
            # so the name rides along for the landing half to create it with
            *([("metadata[name]", body["name"])] if body.get("name") else []),
            # the party liable — the person running the business the invoice bills
            *([("metadata[legal_name]", body["legal_name"])] if body.get("legal_name") else []),
            # the legal business profile, for the contact the landing half creates: contact and
            # address as two values, since Stripe caps a metadata value at 500 characters
            *_legal_metadata(body.get("legal")),
        ])

        log.info(f"setup session {session['id']} for contact {contact_id}")
        return _ok({"url": session["url"], "session_id": session["id"],
                    "stripe_customer_id": customer_id})
    except Exception as e:  # noqa: BLE001
        # `exception` writes a Failure whole — its kind and fields, Stripe's own message among them
        alog.exception("setup link failed", contact_id=contact_id)
        detail = getattr(e, "fields", {}) if isinstance(e, Failure) else {}
        return _ok({"error": str(e), **{k: v for k, v in detail.items() if k in ("status", "error", "code", "param", "path")}}, 502)
