"""Which payment providers a firm has configured, and which one a call means.

    pk = gerp_id, sk = PROVIDER#stripe  →  {configured_at, default: true}

Nothing recorded this before: the provider was implicit in WHICH TOOL the agent called
(`configure_webhook` vs `configure_webhook`), which is deployment state living in a
tool name. Take the name away and the fact needs somewhere to be.

**Rows rather than one value, because several at once is real.** Nothing stops a firm running
Stripe for online and Square in person — the ingest endpoints are separate routes and coexist.

**The row records that SETUP SUCCEEDED. It is not the credential.** That stays at
`/secrets/<provider>/signing_secret`, where `ingest_<provider>` reads it. So a row that outlives its
credential produces a clear "no credential for stripe" rather than a silent wrong path, and the
secret's existence has only one copy.
"""

import json
import os
import time

from aws import resource

SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")
GERP_ID = os.environ.get("CUSTOMER_ID", "")
PREFIX = "PROVIDER#"


class NoProvider(RuntimeError):
    pass


def _table():
    return resource("dynamodb").Table(SETTINGS_TABLE)


def rows() -> list:
    if not SETTINGS_TABLE:
        return []
    from boto3.dynamodb.conditions import Key

    return _table().query(
        KeyConditionExpression=Key("gerp_id").eq(GERP_ID) & Key("sk").begins_with(PREFIX),
    ).get("Items", [])


def configured() -> list:
    return sorted(r["sk"][len(PREFIX):] for r in rows())


def resolve(provider: str = "") -> str:
    """Which provider this call means.

    Named → that one, if the firm configured it. Omitted → the row flagged default. Never an
    arbitrary row: a firm with two providers would otherwise have a payment link come back from
    whichever sorted first.
    """
    have = rows()
    if not have:
        raise NoProvider(
            "no payment provider is set up for this firm yet. Configure one with "
            "configure_webhook and payments can be taken."
        )
    names = {r["sk"][len(PREFIX):]: r for r in have}

    if provider:
        if provider not in names:
            raise NoProvider(
                f"this firm has not set up {provider} — configured: {', '.join(sorted(names))}"
            )
        return provider

    # most recently updated wins a tie, so a half-landed flag move is never ambiguous
    flagged = sorted((r for r in have if r.get("default")),
                     key=lambda r: int(r.get("configured_at", 0)), reverse=True)
    if not flagged:
        raise NoProvider(
            f"no default payment provider is set, so `provider` is required — "
            f"configured: {', '.join(sorted(names))}"
        )
    return flagged[0]["sk"][len(PREFIX):]


def record(provider: str):
    """Note that setup succeeded. Flags the first one default — otherwise a firm configures their
    only provider and a call with no `provider` still resolves to nothing."""
    if not SETTINGS_TABLE:
        return
    have = rows()
    key = PREFIX + provider
    prior = next((r for r in have if r["sk"] == key), {})
    item = {"gerp_id": GERP_ID, "sk": key, "configured_at": int(time.time())}
    # This row's OWN flag is carried forward, because `put_item` replaces rather than merges.
    # Asking only whether some row is already default reads as "do not steal it from another
    # provider" and does the opposite on a re-run of the default one: the flag is absent from the
    # new item and the put erases it, so the firm's only provider stops resolving.
    if prior.get("default") or not any(r.get("default") for r in have):
        item["default"] = True
    _table().put_item(Item=item)


def provider_error(provider, e):
    """What a provider's 4xx actually said.

    A bare exception type is enough to know something failed and useless for knowing WHAT — and a
    4xx from a payment API is almost always about the request (a missing field, a bad amount), which
    is exactly the thing worth reading. The body describes the REQUEST, not the credential, so it is
    safe to surface; the credential travels in a header and is never echoed back.
    """
    import urllib.error

    if not isinstance(e, urllib.error.HTTPError):
        return f"{provider} API call failed: {type(e).__name__}"
    try:
        body = json.loads(e.read().decode())
    except Exception:  # noqa: BLE001 — a non-JSON body still gets the status
        return f"{provider} API call failed: HTTP {e.code}"
    detail = (body.get("error") or {})
    message = detail.get("message") or detail.get("detail") or json.dumps(body)[:300]
    # the CODE comes along, not just the prose. It is the machine-readable half — a caller deciding
    # whether a failure is worth retrying reads `authentication_required`, not a sentence that a
    # processor is free to reword.
    code = detail.get("code") or detail.get("name") or ""
    suffix = f" [{code}]" if code else ""
    return f"{provider} rejected the request (HTTP {e.code}): {message}{suffix}"
