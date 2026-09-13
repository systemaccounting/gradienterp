"""configure_webhook — point this firm's payment provider at our endpoint.

One capability, one tool, whichever provider the firm uses. The agent does not have to know which
that is in order to name a tool: `provider` is a parameter, and setup is the one moment the agent
has just been told the answer.

What every provider does is the same: read the credential the owner submitted, create a webhook
pointed at this firm's own ingest URL, and keep whatever `ingest_<provider>` will need to verify
deliveries. What differs is the request shape, and that is all an adapter is.

The adapter interface, in full:

    NAME, API_BASE_ENV, API_BASE_DEFAULT
    credentials(body, read_secret) -> (creds, error_message)
    configure(creds, url, api_base) -> (verification_value, extra_dict)

Both differences that looked like they would break the fold live entirely inside an adapter, and
neither leaks above the dispatch:

  - **PayPal authenticates in two steps.** It exchanges client id + secret for an access token
    before it can create anything. That happens inside its `configure`; nothing here knows.
  - **PayPal reads a PAIR of secrets at fixed names** where Stripe and Square read one the caller
    names. That is `credentials`, which each adapter answers for itself.

The setup credential is used once and never echoed. The verification value is written straight to
SSM and never returned.
"""

import json
import logging
import os
import urllib.error

import _providers
import provider_paypal
import provider_square
import provider_stripe
from aws import client as _aws, log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)

# imported by name so the bundler's import walk sees every adapter — a dict lookup on a string
# it could not follow would ship a zip missing two of these
ADAPTERS = {a.NAME: a for a in (provider_stripe, provider_paypal, provider_square)}

WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "")
CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)


def _err(message, status=400):
    return {"statusCode": status, "body": json.dumps({"error": message})}


def _read_secret(name):
    """An owner-submitted secret, by the name the agent holds. The value never enters the agent's
    context — only the name does. Swapped in tests."""
    from botocore.exceptions import ClientError

    try:
        return _aws("ssm").get_parameter(
            Name=f"{SECRET_PARAM_PREFIX}/{name}", WithDecryption=True
        )["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def _store_verification(provider, value, leaf="signing_secret", rotates=True):
    """Nested under `<provider>/` so a stack-derived secret cannot collide with the flat
    owner-submitted intake namespace. `_helpers.webhook_secrets(provider)` reads them back.

    The secret being replaced moves to `signing_secret_previous` first. Creating an endpoint
    rotates the secret, and for a moment both are live — the new endpoint signs with the new one
    while events already in flight carry the old. Overwriting without keeping the old value makes
    those 400, and Stripe retries them to an endpoint the sweep has since deleted."""
    ssm = _aws("ssm")
    # the leaf is the adapter's: the name its ingest door reads the value back by
    name = f"{SECRET_PARAM_PREFIX}/{provider}/{leaf}"
    if not rotates:
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
        return
    from botocore.exceptions import ClientError
    try:
        prior = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            raise
        prior = None
    if prior and prior != value:
        ssm.put_parameter(Name=f"{name}_previous", Value=prior,
                          Type="SecureString", Overwrite=True)
    ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    provider = (body.get("provider") or "").strip().lower()

    if not provider:
        return _err(
            f"provider is required to set one up — one of {', '.join(sorted(ADAPTERS))}. "
            "Ask the owner which payment processor they use."
        )
    adapter = ADAPTERS.get(provider)
    if not adapter:
        return _err(f"unknown provider {provider!r} — supported: {', '.join(sorted(ADAPTERS))}")
    if not WEBHOOK_BASE_URL:
        return _err("WEBHOOK_BASE_URL not configured", 500)

    creds, problem = adapter.credentials(body, _read_secret)
    if problem:
        return _err(problem, 404)

    url = f"{WEBHOOK_BASE_URL}/webhooks/{provider}"
    api_base = os.environ.get(adapter.API_BASE_ENV, adapter.API_BASE_DEFAULT)
    try:
        value, extra = adapter.configure(creds, url, api_base)
    except Exception as e:  # noqa: BLE001 — surface the failure without leaking the credential
        approval = getattr(e, "approval_id", None) if e.__class__.__name__ == "ApprovalRequired" else None
        if approval is not None:
            # Stripe wants a human to approve this write: the agent sends the link, the owner
            # approves, and the same call with approval_token goes through
            return {"statusCode": 409, "body": json.dumps({
                "error": "Stripe asks the owner to approve creating the webhook endpoint",
                "approval_url": e.url, "approval_id": e.approval_id,
                "next": "send the owner approval_url; when they say they approved, call again with approval_token = approval_id",
            })}
        text = getattr(e, "text", "")
        if "does not have the required permissions" in text:
            log.info("%s: the firm's grant lacks the write: %s", provider, text)
            return _err("the owner's Stripe approval is read-only; approve again at Stripe with Write on "
                        "webhook endpoints (manage_mcp uninstall, then install), or pass secret_name", 403)
        detail = _providers.provider_error(provider, e)
        if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
            log.info("%s refused the owner's key: %s", provider, detail)
            return _err(detail, 409)
        alog.exception("webhook creation failed", provider=provider)
        return _err(detail, 502)

    _store_verification(provider, value, getattr(adapter, "VERIFICATION_LEAF", "signing_secret"),
                        getattr(adapter, "VERIFICATION_ROTATES", True))
    _providers.record(provider)

    return {"statusCode": 200, "body": json.dumps({
        "status": "configured", "provider": provider, "webhook_url": url, **extra,
    })}
