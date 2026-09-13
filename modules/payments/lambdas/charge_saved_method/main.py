"""charge_saved_method — take the money from a card the payer saved earlier.

The other half of collection. `create_payment_link` asks; this takes, and it may only take where the
payer already said it could: `save_payment_method` wrote `stripe_customer_id` and
`stripe_payment_method_id` onto their contact when they completed a hosted setup page. No saved
card, no charge — and the refusal says to send a link instead, because that is the thing to do next.

**This is the module's first outbound call that MOVES MONEY.** Everything before it either ingested a
webhook, configured one, or made a test payment. Two consequences shape the code:

- **It is idempotent on the invoice.** A retry, a re-fired sequence step or two schedules racing must
  not charge a payer twice. Stripe's `Idempotency-Key` is the only place that can be guaranteed, and
  the key is derived from the invoice rather than generated, so every path that could double-charge
  collapses onto one response.
- **It books nothing.** The charge fires the same `charge.succeeded` webhook a human clicking a link
  would, and `ingest_stripe` books it exactly as it always has. Money moving and money being
  recorded stay separate, which is why a failed booking cannot lose a real payment.

An `authentication_required` decline is called out on its own: the card wants a challenge and nobody
is present to answer it. That is not retryable and the caller is told to send a link instead of
trying again.

TWO CALLERS THAT WANT OPPOSITE THINGS FROM A FAILURE. The agent calls this directly and wants the
error back as a value it can read. A firm's invoice-transition rule announces instead, and
EventBridge invokes this ASYNCHRONOUSLY — nothing reads the return value, only whether the
invocation threw. So on that path a failure another attempt could win has to RAISE: that is what
spends Lambda's async retries and then puts the message on the on-failure destination. Returning 502
there would look like success and the invoice would sit issued, uncollected, with no trace anyone
tried. `announced` is where the two part company.
"""

import hashlib
import json
import logging
import os
import urllib.error

import _helpers as h
import _providers
import provider_stripe
from aws import client as _aws, log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)

ADAPTERS = {a.NAME: a for a in (provider_stripe,)}

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)
GET_INVOICES_FN = os.environ.get("GET_INVOICES_FN", "")
CONTACTS_GET_FN = os.environ.get("CONTACTS_GET_FN", "")
MARK_UNPAID_FN = os.environ.get("MARK_UNPAID_FN", "")
PAYABLE = ("issued", "overdue", "partial")


def _err(message, status=400, **extra):
    return {"statusCode": status, "body": json.dumps({"error": message, **extra})}


def _incident(outcome, invoice_id, **fields):
    """The line `create_inc_from_log` files on: one incident stream per invoice's collection,
    whichever half of it broke. A success closes whatever is open for that invoice."""
    print(json.dumps({"event": f"collection_{outcome}", "incident": outcome,
                      "subject": f"collection:{invoice_id}", "category": "collection",
                      "label": f"Collection for invoice {invoice_id}", **fields}))


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


def _invoke(fn, payload):
    resp = _aws("lambda").invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    if out.get("statusCode") != 200:
        return None
    return json.loads(out["body"])


def _invoice(invoice_id):
    """Read it fresh — the amount charged has to be what is owed now, not what was owed when
    something last looked."""
    body = _invoke(GET_INVOICES_FN, {"op": "get", "invoice_id": invoice_id})
    return next(iter((body or {}).get("invoices", [])), None)


def _contact(contact_id):
    body = _invoke(CONTACTS_GET_FN, {"op": "get", "contact_id": contact_id})
    return (body or {}).get("contact") or body


def _idempotency_key(invoice_id, total, method):
    """Derived, not generated. Keyed on the invoice, the amount AND the method, so a re-run of the
    same sequence step against the same card replays the first response rather than charging again —
    while a genuinely different amount (a partial payment landed, the invoice was revised) is a
    different charge and is allowed to be one.

    The METHOD is in the key because a caller falling back to a second card is making a different
    charge and must reach the processor. Without it Stripe replays the FIRST card's decline for
    every card after it, so a fallback loop looks like it ran and never charges anything."""
    return hashlib.sha256(f"{CUSTOMER_ID}:{invoice_id}:{total}:{method}".encode()).hexdigest()


def _payload(event):
    """The three ways this is called: an EventBridge delivery, an API-Gateway proxy, a direct invoke.
    Returns (body, announced) — `announced` says a raise is the only failure the caller can see."""
    if event.get("detail-type") and isinstance(event.get("detail"), dict):
        return event["detail"], True
    if isinstance(event.get("body"), str):
        return json.loads(event["body"]), False
    return event, False


class Undelivered(Exception):
    """Raised instead of returned, so Lambda retries and then writes the on-failure record.

    Only for failures a later attempt could plausibly win: a processor 5xx, a timeout, a dependency
    that was not answering. A decline is not one — retrying a declined card declines it again, and
    its real destination is a person.
    """


def handler(event, context):
    body, announced = _payload(event)
    invoice_id = (body.get("invoice_id") or "").strip()
    if not invoice_id:
        return _err("invoice_id is required — the invoice to charge for")
    if not (GET_INVOICES_FN and CONTACTS_GET_FN):
        if announced:
            # A deploy problem, not a payment problem. Raising keeps the invoice in the undelivered
            # queue, where fixing the wiring and replaying it collects the money; returning would
            # retire it and the invoice would sit issued and unattempted forever.
            raise Undelivered("GET_INVOICES_FN / CONTACTS_GET_FN not configured")
        return _err("GET_INVOICES_FN / CONTACTS_GET_FN not configured", 500)

    try:
        provider = _providers.resolve((body.get("provider") or "").strip().lower())
    except _providers.NoProvider as e:
        return _err(str(e), 409)

    adapter = ADAPTERS.get(provider)
    if not adapter:
        return _err(f"this firm is set up with {provider}, and charging a saved card is only built "
                    f"for {', '.join(sorted(ADAPTERS))} so far", 501)

    inv = _invoice(invoice_id)
    if not inv:
        # `_invoke` cannot tell a missing invoice from a failed read — both come back None. That is
        # harmless to a caller asking about an invoice it is unsure of, and wrong here: the rule only
        # ran because the invoice reached a status, so it provably exists and None means the read
        # failed. A failed read is worth another attempt.
        if announced:
            raise Undelivered(f"could not read invoice {invoice_id} to charge it")
        return _err(f"no invoice {invoice_id}", 404)
    if inv.get("status") not in PAYABLE:
        return _err(f"invoice {invoice_id} is {inv.get('status')}, so there is nothing to charge",
                    409)

    contact = _contact(inv.get("customer"))
    if not contact:
        return _err(f"no contact {inv.get('customer')} to charge", 404)

    saved, problem = adapter.saved_method(contact, body.get("payment_method_id") or "")
    if problem:
        return _err(problem, 409, invoice_id=invoice_id)

    secret_name = os.environ.get(f"{provider.upper()}_KEY_SECRET", adapter.STANDING_SECRET)
    creds, problem = adapter.credentials(_read_secret, secret_name)
    if problem:
        return _err(problem, 404)

    api_base = os.environ.get(adapter.API_BASE_ENV, adapter.API_BASE_DEFAULT)
    try:
        made = adapter.charge(creds, saved, inv, api_base,
                              _idempotency_key(invoice_id, inv["total"], saved["method"]))
    except Exception as e:  # noqa: BLE001 — surface the reason without leaking the credential
        # an adapter's own refusal before any charge carries its detail; a processor's comes off the body
        detail = getattr(e, "detail", None) or _providers.provider_error(provider, e)
        # a 402 decline, or an address Stripe Tax cannot place: the processor's no about the payer,
        # and another attempt gets the same no
        declined = hasattr(e, "detail") or (isinstance(e, urllib.error.HTTPError) and (
            e.code == 402 or "customer_tax_location_invalid" in detail))
        if declined:
            log.info("%s declined the charge for %s: %s", provider, invoice_id, detail)
        else:
            alog.exception("charge failed", provider=provider, invoice_id=invoice_id, detail=detail)
        _incident("fail", invoice_id, provider=provider, error=detail)
        # The invoice says it is owed; this says someone tried to take it and could not, which is
        # what a chase attaches to. Marked on EVERY failure, retryable or not: `unpaid → paid` is
        # permitted, so a retry that later wins settles it through the ordinary webhook path with
        # nothing to undo. Best-effort — the incident above is the record that matters, and failing
        # to update a status must not swallow the reason the charge failed.
        if MARK_UNPAID_FN:
            try:
                _invoke(MARK_UNPAID_FN, {"invoice_id": invoice_id, "reason": detail})
            except Exception as mark_e:  # noqa: BLE001
                log.warning("could not mark %s unpaid: %s", invoice_id, mark_e)
        if adapter.needs_the_payer_back(detail):
            return _err(
                "the card needs the payer to confirm it and they are not here — send a payment "
                "link instead; charging again will fail the same way",
                409, invoice_id=invoice_id, retryable=False,
            )
        if declined:
            return _err(detail, 409, invoice_id=invoice_id, retryable=False)
        if announced:
            raise Undelivered(f"{provider} charge for {invoice_id} failed: {detail}") from e
        return _err(detail, 502, invoice_id=invoice_id)

    _incident("ok", invoice_id, provider=provider)
    # The processor accepted the card; the BOOKS move only when its webhook arrives. Ask to be
    # reminded, so a delivery that never comes is noticed here rather than by an owner eventually
    # wondering why a paid invoice still reads issued. See check_collection.
    h.watch_collection({"invoice_id": invoice_id, "provider": provider,
                        "reference": made.get("charge") or made.get("payment_intent") or "",
                        # India: the charge completes after the pre-debit notice, not in minutes
                        **({"hold_until": made["completes_at"]} if made.get("completes_at") else {})})
    return {"statusCode": 200, "body": json.dumps({
        "invoice_id": invoice_id, "provider": provider, "amount": inv["total"], **made,
        "note": "the processor delivers the payment event to the webhook; the books catch up from "
                "there rather than from this call.",
    })}
