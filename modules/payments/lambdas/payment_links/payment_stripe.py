"""Stripe: a fresh Checkout Session per link.

Sessions expire within 24 hours of creation, so a link created when the invoice was issued is dead
long before a day-3 dunning notice, let alone day 12. There is nothing to store and nothing to go
stale: the notice is composed at send time anyway, and creating the session then also re-reads the
amount, so a link is never wrong about what is owed.
"""

import json
import urllib.parse
import urllib.request

import stripe_api

NAME = "stripe"
API_BASE_ENV = "STRIPE_API_BASE"
API_BASE_DEFAULT = "https://api.stripe.com"
STANDING_SECRET = "stripe_billing"   # env-overridable by the capability


def credentials(read_secret, secret_name):
    """The STANDING key, not the one-shot setup key. Creating a payment link is ongoing work, so
    it needs a credential that outlives setup — the same one create_setup_link uses."""
    key = read_secret(secret_name)
    if not key:
        return None, (f"no '{secret_name}' secret for stripe — it needs a standing key with "
                      "Checkout Sessions: write")
    return {"api_key": key}, None


# every provider needs somewhere to send the payer afterwards; Stripe rejects a session without it
NEEDS_RETURN_URL = True


def create_link(creds, invoice, api_base, return_url):
    """One Checkout Session for the invoice total.

    `price_data` rather than a Price object: the amount is this invoice's, it is used once, and a
    Price would be a second Stripe object per invoice to create and never reuse.
    """
    cents = int(round(float(invoice["total"]) * 100))
    fields = [
        ("mode", "payment"),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", invoice.get("currency", "usd").lower()),
        ("line_items[0][price_data][unit_amount]", str(cents)),
        ("line_items[0][price_data][product_data][name]",
         invoice.get("memo") or f"Invoice {invoice['invoice_id']}"),
        # our id on the session, so a webhook can match the payment back to the invoice without
        # anything having to remember the session
        ("metadata[invoice_id]", invoice["invoice_id"]),
        ("client_reference_id", invoice["invoice_id"]),
    ]
    # REQUIRED by Stripe in payment mode — a session without it is a 400, so the capability
    # refuses before calling rather than sending a request that cannot work
    joiner = "&" if "?" in return_url else "?"
    fields += [("success_url", f"{return_url}{joiner}paid={invoice['invoice_id']}"),
               ("cancel_url", return_url)]

    req = urllib.request.Request(
        f"{api_base}/v1/checkout/sessions",
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {creds['api_key']}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        session = json.loads(resp.read().decode())
    return session["url"], {"session_id": session["id"], "expires_within_hours": 24}
