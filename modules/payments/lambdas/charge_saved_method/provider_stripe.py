"""Stripe: an off-session PaymentIntent against a card the payer saved earlier.

`off_session=true` tells Stripe the customer is not present, which changes what it does with a card
that wants a challenge: instead of showing one it declines with `authentication_required`, and the
only cure is the payer coming back and doing it themselves. That is not a retryable failure and the
adapter says so, because retrying it forever is how a firm annoys a customer into leaving.
"""

import io
import json
import urllib.error
import urllib.parse
import urllib.request

import stripe_api
from aws import log

NAME = "stripe"
API_BASE_ENV = "STRIPE_API_BASE"
API_BASE_DEFAULT = "https://api.stripe.com"
STANDING_SECRET = "stripe_billing"

# the fields save_payment_method writes onto the payer's contact
CUSTOMER_FIELD = "stripe_customer_id"
METHOD_FIELD = "stripe_payment_method_id"


def credentials(read_secret, secret_name):
    key = read_secret(secret_name)
    if not key:
        return None, (f"no '{secret_name}' secret for stripe — charging a saved card needs a "
                      "standing key with Payment Intents: write")
    return {"api_key": key}, None


def saved_method(contact, want=""):
    """What this payer saved, or a reason there is nothing to charge.

    `want` aims it at one saved method — a caller walking a payer's cards after a decline. Empty
    means the one the payer is billed to, which is what the rule instance, the agent and the
    EventBridge target all do.

    The customer comes off the CONTACT either way, never from the caller: a method is chargeable
    only against the customer it hangs off, and letting a caller supply both halves would let it
    pair a method with a customer that never saved it."""
    customer, method = contact.get(CUSTOMER_FIELD), contact.get(METHOD_FIELD)
    if want:
        method = want
    if not (customer and method):
        return None, ("this customer has no saved card — send them a payment link, or a setup link "
                      "if they want autopay")
    return {"customer": customer, "method": method}, None


def _stripe_post(creds, api_base, path, fields, idempotency_key=None):
    req = urllib.request.Request(f"{api_base}{path}", data=urllib.parse.urlencode(fields).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Bearer {creds['api_key']}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    if idempotency_key:
        req.add_header("Idempotency-Key", idempotency_key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _error_code(raw: bytes) -> str:
    try:
        return (json.loads(raw.decode()).get("error") or {}).get("code") or ""
    except Exception:  # noqa: BLE001
        return ""


class NeedsThePayer(Exception):
    """Refused before any PaymentIntent: the card cannot be charged with nobody present, and only
    the payer coming back to a link fixes it. `detail` carries a PAYER_BACK code."""

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def _stripe_get(creds, api_base, path):
    req = urllib.request.Request(f"{api_base}{path}")
    req.add_header("Authorization", f"Bearer {creds['api_key']}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def india_mandate(creds, saved, api_base):
    """The e-mandate an India-issued card is charged under, read off the card and its setup.

    Returns the mandate id for an `IN` card whose succeeded SetupIntent registered one, `None` for
    any other card. An `IN` card with no mandate — saved through Checkout, which cannot register one —
    raises: RBI declines every off-session charge on it, so it is the payer-back case before a
    PaymentIntent exists rather than a decline after one."""
    pm = _stripe_get(creds, api_base, f"/v1/payment_methods/{saved['method']}")
    if ((pm.get("card") or {}).get("country") or "").upper() != "IN":
        return None
    query = urllib.parse.urlencode({"payment_method": saved["method"], "customer": saved["customer"], "limit": "10"})
    intents = _stripe_get(creds, api_base, f"/v1/setup_intents?{query}").get("data") or []
    mandate = next((si.get("mandate") for si in intents
                    if si.get("status") == "succeeded" and si.get("mandate")), None)
    if not mandate:
        raise NeedsThePayer(f"{NO_INDIA_MANDATE}: an India-issued card saved without an e-mandate "
                            "cannot be charged off-session")
    return mandate


def tax_calculation(creds, saved, invoice, api_base):
    """Stripe Tax's answer for this invoice at this customer's address: `(calculation_id, tax_cents,
    total_cents)`. The address is the one the customer typed on the setup page (it lives on the
    Stripe customer), the tax code is the account's preset, and where gradienterp holds no
    registration the answer is zero tax and the invoice's own total.

    An account with Stripe Tax not yet set up answers the calculation call with an error that is
    about the account, not the customer; the charge then goes out untaxed rather than not at all.
    `customer_tax_location_invalid` is about the customer — an address Stripe cannot resolve — and
    fails the charge closed the way a decline does, so the notice says what to fix."""
    cents = int(round(float(invoice["total"]) * 100))
    fields = [
        ("currency", invoice.get("currency", "usd").lower()),
        ("customer", saved["customer"]),
        ("line_items[0][amount]", str(cents)),
        ("line_items[0][reference]", invoice["invoice_id"]),
    ]
    try:
        calc = _stripe_post(creds, api_base, "/v1/tax/calculations", fields)
    except urllib.error.HTTPError as e:
        raw = e.read()   # a body reads once; the re-raise carries a copy the caller can read
        code = _error_code(raw)
        if code == "customer_tax_location_invalid" or e.code >= 500 or e.code == 429:
            raise urllib.error.HTTPError(e.url, e.code, e.msg, e.headers, io.BytesIO(raw)) from e
        # Stripe Tax not enabled / no settings on the account: not this payer's fault
        log.warning("stripe tax calculation refused; charging untaxed",
                    invoice_id=invoice["invoice_id"], status=e.code, code=code)
        return None, 0, cents
    return calc.get("id"), int(calc.get("tax_amount_exclusive") or 0), int(calc.get("amount_total") or cents)


def charge(creds, saved, invoice, api_base, idempotency_key):
    """Confirm a PaymentIntent immediately, off-session.

    `confirm=true` with `off_session=true` makes this one call rather than create-then-confirm —
    there is no browser to hand back to in between.

    The idempotency key is the invoice's, not a fresh uuid: a retry, a re-fire of the same sequence
    step, or two schedules racing must not charge a payer twice, and Stripe replaying the first
    response is the only place that can be guaranteed. Tax rides the key too, so a calculation that
    comes back different is a different charge.

    Sales tax: the calculation runs first; when it owes anything the intent is for the total with
    tax and carries the calculation, so Stripe records the tax transaction on success and the
    reversal on a refund. The tax rides `metadata[tax_amount]` (cents) for the webhook, which posts
    it to SALES_TAX_PAYABLE beside the receivable.
    """
    mandate = india_mandate(creds, saved, api_base)
    calc_id, tax_cents, total_cents = tax_calculation(creds, saved, invoice, api_base)
    fields = [
        ("amount", str(total_cents)),
        ("currency", invoice.get("currency", "usd").lower()),
        ("customer", saved["customer"]),
        ("payment_method", saved["method"]),
        ("off_session", "true"),
        ("confirm", "true"),
        # our id on the intent, so the webhook books the payment against the right invoice with
        # nothing having to remember this call happened
        ("metadata[invoice_id]", invoice["invoice_id"]),
    ]
    if mandate:
        # India: the charge sits `processing` for 26 hours while the bank sends the pre-debit notice
        fields.append(("mandate", mandate))
    if tax_cents > 0:
        fields += [
            ("hooks[inputs][tax][calculation]", calc_id),
            ("metadata[tax_amount]", str(tax_cents)),
            ("metadata[tax_calculation]", calc_id),
        ]
        idempotency_key = f"{idempotency_key}-tax{tax_cents}"
    req = urllib.request.Request(
        f"{api_base}/v1/payment_intents",
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {creds['api_key']}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Idempotency-Key", idempotency_key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        pi = json.loads(resp.read().decode())

    notice = ((pi.get("processing") or {}).get("card") or {}).get("customer_notification") or {}
    return {"status": pi.get("status"), "payment_intent": pi.get("id"),
            "tax": tax_cents / 100, "charged": total_cents / 100,
            "charge": pi.get("latest_charge"),
            **({"completes_at": notice["completes_at"]} if notice.get("completes_at") else {}),
            **({"approval_requested": True} if notice.get("approval_requested") else {})}


# the declines that mean the card itself has to be saved again — a challenge nobody is here to
# answer, or an India e-mandate the cardholder has cancelled, paused, or never registered
NO_INDIA_MANDATE = "india_card_without_mandate"   # ours, raised before a PaymentIntent
PAYER_BACK = ("authentication_required", "transaction_not_approved",
              "india_recurring_payment_mandate_canceled", "payment_intent_mandate_invalid",
              NO_INDIA_MANDATE)


def needs_the_payer_back(body: str) -> bool:
    """A decline retrying cannot fix: the card wants a challenge and nobody is there to answer
    it, or the card's mandate is gone. The payer has to come back to a link."""
    return any(code in (body or "") for code in PAYER_BACK)
