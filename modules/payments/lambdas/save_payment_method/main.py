"""save_payment_method — the landing half: read the finished session, store the two ids.

The browser comes back from Stripe with a session id and nothing else. This reads that session,
takes the customer (`cus_…`) and the payment method it saved (`pm_…`), and writes both onto the
contact. Those are the two values `charge_stripe_saved_method` needs: which payer, and which of
their methods.

**Not the webhook.** `ingest_stripe` dispatches on `transform_stripe_<event>` and every one of those
turns an event into debits and credits. A saved card moves no money and posts no entry, so
`checkout.session.completed` sent there would dead-letter as "no transform". A payer who closes the
tab before the redirect never gets stored — a webhook backstop for that needs a landing place that
is not a transform, and does not exist yet.

    in    {session_id} — a Checkout session, or {setup_intent_id} — the card page's SetupIntent
    out   {contact_id, stripe_customer_id, stripe_payment_method_id}

Both land on the CUSTOMER bucket of the contact registry. `preferred_payment_method` is not the
home for the second one despite the name — it is a vendor field, an enum of rails
(ach|wire|check|stripe|paypal|other) saying how we pay a supplier.

The contact id comes from the SESSION's metadata, not from the caller. The caller is a redirect the
payer's browser followed, so anything it carries is theirs to edit; the session was created server
side by `create_setup_link` and Stripe hands it back unchanged.

Design: modules/payments/AGENTS.md § connecting a processor.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

import stripe_api

from aws import client as _aws, log as alog, Failure
from _kinds import STRIPE_CALL_FAILED, INVOKE_FAILED, CONTACT_WRITE_FAILED

log = logging.getLogger()
log.setLevel(logging.INFO)

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)
STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
STRIPE_KEY_SECRET = os.environ.get("STRIPE_KEY_SECRET", "stripe_billing")
CONTACTS_UPDATE_FN = os.environ.get("CONTACTS_UPDATE_FN", "")
CONTACTS_PUT_FN = os.environ.get("CONTACTS_PUT_FN", "")


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
                      code=err.get("code", "")) from None


def _invoke(fn, payload):
    resp = _aws("lambda").invoke(
        FunctionName=fn, InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        raise Failure(INVOKE_FAILED, fn=fn, error=raw[:400].decode(errors="replace"))
    return json.loads(raw or b"{}")


def _create_contact(contact_id, name, legal_name="", legal=None):
    """First sight of this buyer. `manage_contacts` put is create-or-replace and needs `name` on an
    organization — which is why the name rides the session metadata: at this point the card is
    the only thing we have, and the request that knew the business name ended at the redirect.
    `legal_name` rides the same way: the person running the business, whom the invoice names."""
    result = _invoke(CONTACTS_PUT_FN, {
        "op": "put",
        "contact_id": contact_id, "entity_type": "organization",
        "is_customer": True, "name": name or contact_id,
        **({"legal_name": legal_name} if legal_name else {}),
        **_contact_fields(legal),
    })
    if result.get("statusCode") not in (200, 201, 202):
        raise Failure(CONTACT_WRITE_FAILED, contact_id=contact_id, op="put", status=result.get("statusCode"), error=result.get("body"))
    return result


def _legal_of(meta):
    """The legal business profile the setup link put on the session, in its two metadata values."""
    out = {}
    for key in ("legal", "legal_address"):
        try:
            out.update(json.loads(meta.get(key) or "{}"))
        except ValueError:
            pass
    return out


def _contact_fields(legal):
    """The contact's email, phone and business address off the legal business profile the create
    screen collected. The form takes one street line; the contact splits number and name."""
    legal = legal or {}
    out = {}
    if legal.get("email"):
        out["email"] = legal["email"]
    if legal.get("phone"):
        out["phone"] = legal["phone"]
    if legal.get("tax_id"):
        out["tax_id"] = legal["tax_id"]   # the business's GSTIN / VAT id, which the invoice names
    if any(legal.get(k) for k in ("street", "city", "state", "zip", "country")):
        street = (legal.get("street") or "").strip()
        m = re.match(r"^(\S*\d\S*)\s+(.+)$", street)
        addr = {"address_type": "business",
                "street_number": m.group(1) if m else "", "street_name": m.group(2) if m else street,
                "unit": legal.get("unit") or "", "city": legal.get("city") or "",
                "state": legal.get("state") or "", "postal_code": legal.get("zip") or "",
                "country": legal.get("country") or ""}
        out["addresses"] = [{k: v for k, v in addr.items() if v}]
    return out


def _update_contact(contact_id, updates):
    """MERGE the two ids onto the contact. `manage_contacts` update, not put — put is
    create-or-replace and requires `name` on an organization, so writing two fields through it
    would blank everything else the contact holds.

    The status code is checked, not just FunctionError: a lambda returning `{"statusCode": 400}`
    is a SUCCESSFUL invocation, so a rejected write looks identical to a stored one from here.
    """
    result = _invoke(CONTACTS_UPDATE_FN, {"op": "update", "contact_id": contact_id, "updates": updates})
    if result.get("statusCode") not in (200, 201, 202):
        raise Failure(CONTACT_WRITE_FAILED, contact_id=contact_id, op="update", status=result.get("statusCode"), error=result.get("body"))
    return result


def _ok(body, code=200):
    return {"statusCode": code, "body": json.dumps(body)}


def handler(event, context):
    body = event if isinstance(event, dict) and ("session_id" in event or "setup_intent_id" in event) else json.loads(
        event.get("body") or "{}"
    )
    session_id = (body.get("session_id") or "").strip()
    setup_intent_id = (body.get("setup_intent_id") or "").strip()
    if not session_id and not setup_intent_id:
        return _ok({"error": "session_id or setup_intent_id required"}, 400)

    try:
        api_key = _read_secret(STRIPE_KEY_SECRET)
        if not api_key:
            return _ok({"error": f"no '{STRIPE_KEY_SECRET}' secret; needs Checkout Sessions: read "
                                 "and Customers: write"}, 400)

        if session_id:
            session = _get(api_key, f"/v1/checkout/sessions/{session_id}")
            if session.get("status") != "complete":
                # they cancelled, or came back before Stripe finished. Nothing saved, nothing to store.
                return _ok({"status": session.get("status"), "stored": False})
            meta, customer = session.get("metadata") or {}, session.get("customer")
            # the payment method lives on the SetupIntent the session created, not on the session
            setup_intent_id = session.get("setup_intent")
            if not setup_intent_id:
                return _ok({"error": "session has no setup_intent"}, 400)
            setup_intent = _get(api_key, f"/v1/setup_intents/{setup_intent_id}")
        else:
            # the card page's own SetupIntent (an Indian business, the e-mandate registered with the
            # card). The same trust: whose card it is comes off the intent the server created.
            setup_intent = _get(api_key, f"/v1/setup_intents/{setup_intent_id}")
            if setup_intent.get("status") != "succeeded":
                return _ok({"status": setup_intent.get("status"), "stored": False})
            meta, customer = setup_intent.get("metadata") or {}, setup_intent.get("customer")

        contact_id = meta.get("contact_id")
        if not contact_id:
            return _ok({"error": "the setup carries no contact_id"}, 400)
        payment_method = setup_intent.get("payment_method")
        if not payment_method:
            return _ok({"error": "setup_intent saved no payment method"}, 400)

        # NOT `preferred_payment_method` — that is a VENDOR field, an enum of rails
        # (ach|wire|check|stripe|paypal|other) meaning how WE pay THEM.
        # Two fields, one value, on purpose. `stripe_customer_id` is the SELECTED pair's customer and
        # `select` may point it at someone else's — a gerp billed to a card its account owner saved.
        # `stripe_own_customer_id` is this party's own, and nothing but this write ever sets it, so
        # choosing another payer's card cannot lose the way back to their own.
        fields = {"stripe_customer_id": customer,
                  "stripe_own_customer_id": customer,
                  "stripe_payment_method_id": payment_method}
        try:
            _update_contact(contact_id, fields)
        except Failure as e:
            if e.fields.get("status") != 404:
                alog.exception("could not write the saved card onto the contact",
                               contact_id=contact_id, session_id=session_id or setup_intent_id)
                return _ok({"error": str(e), **e.fields}, 502)
            # nothing knew this buyer before now — saving a card IS the first record of them
            _create_contact(contact_id, meta.get("name"), meta.get("legal_name") or "", _legal_of(meta))
            _update_contact(contact_id, fields)

        log.info(f"saved payment method for contact {contact_id}")
        # Brand and last4 for whoever has to SHOW this. They are returned, not stored: the caller holds
        # a payer-facing surface and we hold the two ids, and a card's display text going stale on a
        # contact row is a second copy of something Stripe already owns. The key lives here, so nobody
        # upstream can ask Stripe for them.
        return _ok({"contact_id": contact_id, "stored": True, **fields, **_card_display(api_key, payment_method)})
    except Exception as e:  # noqa: BLE001 — a raise here is a FunctionError the BFF forwards verbatim
        if getattr(e, "status", None) == 404:
            return _ok({"error": str(e)}, 404)
        alog.exception("saving the payment method failed", session_id=session_id or setup_intent_id)
        return _ok({"error": str(e), **getattr(e, "fields", {})}, 502)


def _card_display(api_key, payment_method_id):
    """`{card_brand, card_last4, card_exp, fingerprint}`, or `{}` if Stripe will not say.
    Best-effort on purpose: the card IS saved by this point, so failing the whole call over a
    label would throw away a completed setup and make the payer do it again.

    `fingerprint` is Stripe's `card.fingerprint` — one value per card number across customers.
    The gerp-cloud BFF checks it against gerp-priors before the card vends a gerp."""
    try:
        pm = _get(api_key, f"/v1/payment_methods/{payment_method_id}")
        card = pm.get("card") or {}
        if not card:
            return {}
        return {"card_brand": card.get("brand", ""), "card_last4": card.get("last4", ""),
                "card_exp": f"{card.get('exp_month', 0):02d}/{str(card.get('exp_year', ''))[-2:]}",
                "fingerprint": card.get("fingerprint", "")}
    except Exception as e:  # noqa: BLE001
        log.warning(f"payment method {payment_method_id} saved but not describable: {e}")
        return {}
