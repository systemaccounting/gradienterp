"""create_payment_link — a link the payer can click, for one invoice.

A notice that says "you owe $840" and nothing else makes the reader go and find how to pay. This
is what puts the link in it, and it is the same capability whichever processor the firm uses: the
agent asks for a link for an invoice, and never has to know the firm runs Square to name a tool.

The link is created FRESH each time it is asked for. Stripe's sessions expire within 24 hours, so
one created when the invoice was issued is dead long before a day-3 dunning notice — and creating
it at send time re-reads the amount, so a link is never wrong about what is owed. Nothing here
stores a link.

Adapters, same shape as configure_webhook:

    NAME, API_BASE_ENV, API_BASE_DEFAULT, STANDING_SECRET
    credentials(read_secret, secret_name) -> (creds, error_message)
    create_link(creds, invoice, api_base, return_url) -> (url, extra)

The credential is the STANDING one, not the one-shot key setup used. Creating links is ongoing
work, so it needs a credential that outlives the setup call that was discarded.

Only Stripe has an adapter today. A firm on another processor gets a refusal that names what is
missing, rather than a stub that returns something that does not work.
"""

import json
import logging
import os

import _providers
import payment_stripe
from aws import client as _aws, log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)

ADAPTERS = {a.NAME: a for a in (payment_stripe,)}

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)
GET_INVOICES_FN = os.environ.get("GET_INVOICES_FN", "")
# where a payer lands after paying when the caller names nowhere: a page that says the payment went
# through. Never the firm's portal — its url carries the slug, the portal's only credential.
PAYER_LANDING_URL = os.environ.get("PAYER_LANDING_URL", "https://gradienterp.cloud/paid")
PAYABLE = ("issued", "overdue", "partial")


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}


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


def _invoice(invoice_id):
    """Read it fresh. The amount on the link has to be what is owed NOW, not what was owed when
    someone last looked — a sequence sends the same invoice a dozen times."""
    resp = _aws("lambda").invoke(
        FunctionName=GET_INVOICES_FN,
        Payload=json.dumps({"op": "get", "invoice_id": invoice_id}).encode(),
    )
    out = json.loads(resp["Payload"].read() or b"{}")
    if out.get("statusCode") != 200:
        return None
    return next(iter(json.loads(out["body"]).get("invoices", [])), None)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    invoice_id = (body.get("invoice_id") or "").strip()
    if not invoice_id:
        return _err("invoice_id is required — the invoice this link pays")
    if not GET_INVOICES_FN:
        return _err("GET_INVOICES_FN not configured", 500)

    try:
        provider = _providers.resolve((body.get("provider") or "").strip().lower())
        adapter = ADAPTERS.get(provider)
        if not adapter:
            return _err(
                f"this firm is set up with {provider}, but payment links are only built for "
                f"{', '.join(sorted(ADAPTERS))} so far",
                501,
            )

        inv = _invoice(invoice_id)
        if not inv:
            return _err(f"no invoice {invoice_id}", 404)
        if inv.get("status") not in PAYABLE:
            return _err(
                f"invoice {invoice_id} is {inv.get('status')}, so there is nothing to pay — "
                f"a link is for {', '.join(PAYABLE)}",
                409,
            )

        secret_name = os.environ.get(
            f"{provider.upper()}_KEY_SECRET", adapter.STANDING_SECRET
        )
        creds, problem = adapter.credentials(_read_secret, secret_name)
        if problem:
            return _err(problem, 404)

        # where the payer lands after paying: the payment-received page unless the caller names
        # somewhere. Stripe rejects a session without one, so this is checked HERE — a refusal naming
        # the missing thing beats a 400 from a vendor.
        return_url = (body.get("return_url") or "").strip() or PAYER_LANDING_URL
        if getattr(adapter, "NEEDS_RETURN_URL", False) and not return_url:
            return _err(f"{provider} needs somewhere to send the payer after paying — pass return_url", 400)

        api_base = os.environ.get(adapter.API_BASE_ENV, adapter.API_BASE_DEFAULT)
        try:
            url, extra = adapter.create_link(creds, inv, api_base, return_url)
        except Exception as e:  # noqa: BLE001 — surface the failure without leaking the credential
            detail = _providers.provider_error(provider, e)
            alog.error("payment link failed", provider=provider, invoice_id=invoice_id, detail=detail)
            return _err(detail, 502)

        return {"statusCode": 200, "body": json.dumps({
            "invoice_id": invoice_id, "provider": provider, "url": url,
            "amount": inv["total"], "customer": inv.get("customer"), **extra,
        })}
    except _providers.NoProvider as e:
        return _err(str(e), 409)
    except Exception as e:  # noqa: BLE001
        alog.error("payment link failed", invoice_id=invoice_id, error=str(e))
        return _err(str(e), 502)
