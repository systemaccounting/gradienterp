"""create_test_payment — prove the processor→us seam with a real payment.

The integration test the agent runs after setup. Not a synthetic event POSTed at our own endpoint:
that never touches the processor and proves nothing. This asks the processor to create a real
test-mode or sandbox payment, and the processor then signs its own webhook and delivers it. What
gets exercised is the whole seam — endpoint reachability, the real signature round-trip, the real
payload shape.

One capability, whichever processor the firm uses. Same adapter interface as configure_webhook:

    NAME, API_BASE_ENV, API_BASE_DEFAULT, DEFAULT_AMOUNT, DELIVERS
    credentials(body, read_secret) -> (creds, error_message)
    create(creds, amount, api_base) -> a dict of what the processor made

The amount defaults differ per processor and belong to them: Stripe's test mode is free, so $20
reads clearly in a ledger; a sandbox dollar is enough elsewhere.

The result never claims the webhook ARRIVED. It says what was created and that the processor will
deliver in a few seconds — confirming is a separate look, because assuming delivery is how a broken
seam reads as a working one.
"""

import json
import logging
import os

import _providers
import test_paypal
import test_square
from aws import client as _aws, log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)

# a processor whose own tools are installed (modules/mcp) makes its test payment through them,
# and has no adapter here: Stripe's went with the vendor gateway
ADAPTERS = {a.NAME: a for a in (test_paypal, test_square)}

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)


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


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    try:
        provider = _providers.resolve((body.get("provider") or "").strip().lower())
        adapter = ADAPTERS.get(provider)
        if not adapter:
            return _err(f"no test payment is built here for {provider} — have: {', '.join(sorted(ADAPTERS))}. "
                        "A processor whose own tools are installed (manage_mcp) makes a test-mode payment "
                        "through them, and its webhook delivers the same way", 501)

        creds, problem = adapter.credentials(body, _read_secret)
        if problem:
            return _err(problem, 404)

        amount = float(body.get("amount") or adapter.DEFAULT_AMOUNT)
        api_base = os.environ.get(adapter.API_BASE_ENV, adapter.API_BASE_DEFAULT)
        try:
            made = adapter.create(creds, amount, api_base)
        except Exception as e:  # noqa: BLE001 — surface the failure without leaking the credential
            detail = _providers.provider_error(provider, e)
            alog.error("test payment failed", provider=provider, detail=detail)
            return _err(detail, 502)

        return {"statusCode": 200, "body": json.dumps({
            "provider": provider, "amount": amount, **made,
            "note": f"{provider} delivers {adapter.DELIVERS} to the webhook in a few seconds; "
                    "confirm with list_pending_entries rather than assuming it arrived.",
        })}
    except _providers.NoProvider as e:
        return _err(str(e), 409)
    except Exception as e:  # noqa: BLE001
        alog.error("test payment failed", provider=body.get("provider"), error=str(e))
        return _err(str(e), 502)
