"""Stripe: create the endpoint, keep its signing secret. Two ways in.

The firm path (default): the owner installed Stripe's own tools (modules/mcp) with Write on
webhook endpoints, and this lambda makes the create through the gerp's vendor gateway as the
firm — `stripe_api_write PostWebhookEndpoints` — so the secret comes back here and never into
the model's context. Stripe may ask a human to approve a write first: the answer carries an
approval url, the door returns it as a 409, and the same call with `approval_token` goes through.

The key path (`secret_name`): one caller-named restricted key, the shape from before the
vendor gateway existed, kept for a firm that prefers a key.
"""

import json
import urllib.parse
import urllib.request

import firm_gateway
import stripe_api
from aws import log

NAME = "stripe"
API_BASE_ENV = "STRIPE_API_BASE"
API_BASE_DEFAULT = "https://api.stripe.com"

# the events ingest_stripe has transforms for; others would dead-letter
ENABLED_EVENTS = ["charge.succeeded", "refund.created", "invoice.paid", "payout.paid"]

# The key that COLLECTS. Only read here to compare modes — see `_mode_conflict`.
COLLECTING_SECRET = "stripe_billing"


def _mode(key):
    """test / live / "" — Stripe carries the mode inside the key rather than in the host."""
    for m in ("test", "live"):
        if f"_{m}_" in key[:12]:
            return m
    return ""


def _mode_conflict(setup_key, read_secret):
    """A message when the setup key and the collecting key are in different modes.

    PayPal and Square cannot express this: their sandboxes are different HOSTNAMES, so a mismatch
    fails at connect time on its own. Stripe uses one host and one API for both modes, so a test
    setup key alongside a live collecting key connects cleanly, stores a signing secret and reports
    success — while every live charge delivers to an endpoint that only exists in test mode.

    Checked here because this is the one moment both keys are in hand.
    """
    collecting = read_secret(COLLECTING_SECRET) or ""
    if not collecting:
        return None                      # collection not set up; nothing to disagree with
    a, b = _mode(setup_key), _mode(collecting)
    if not a or not b or a == b:
        return None
    return (f"this key is a {a}-mode key and '{COLLECTING_SECRET}' is {b}-mode. A webhook is "
            f"created in the mode of the key that creates it, so a {a}-mode endpoint would never "
            f"receive a {b}-mode charge. Use keys from the same mode — for a real business, both "
            "live.")


class ApprovalRequired(Exception):
    """Stripe wants a human to approve this write: `url` is where, `approval_id` what to pass back."""

    def __init__(self, url, approval_id):
        self.url, self.approval_id = url, approval_id
        super().__init__(f"Stripe asks the owner to approve this at {url}")


def credentials(body, read_secret):
    """Without `secret_name`: the firm's own grant on the vendor gateway — the Stripe account
    whose mode matches the collecting key's (or the only one). With it: a restricted API key the
    owner submitted, named by the caller.

    Returns None with a message when neither is there — the capability turns that into the
    404, so the wording stays with the provider that knows what it wanted.
    """
    name = (body.get("secret_name") or "").strip()
    if not name:
        return _firm_credentials(body, read_secret)
    key = read_secret(name)
    if not key:
        return None, (f"secret '{name}' not found — collect it from the owner first via "
                      "collect_secret")
    conflict = _mode_conflict(key, read_secret)
    if conflict:
        return None, conflict
    return {"api_key": key}, None


def _firm_credentials(body, read_secret):
    try:
        accounts = firm_gateway.call("stripe___list_available_accounts_or_orgs", {}).get("accounts") or []
    except firm_gateway.NoVendorGateway:
        return None, ("connect Stripe first: manage_mcp {op: install, provider: stripe} with Write on "
                      "webhook endpoints at Stripe's approval screen — or pass secret_name for a "
                      "restricted key")
    except firm_gateway.VendorConsentRequired as e:
        return None, (f"Stripe's tools need the owner's approval first — send this link and try "
                      f"again after they approve: {e.url}")
    except firm_gateway.VendorRefused as e:
        if "not found" in e.text.lower() or "unknown tool" in e.text.lower():
            return None, ("Stripe is not installed on this firm's vendor gateway: manage_mcp "
                          "{op: install, provider: stripe} first, or pass secret_name")
        return None, f"Stripe refused: {e.text}"
    if not accounts:
        return None, "the owner's Stripe approval covers no account; approve again and pick one"
    want = _mode(read_secret(COLLECTING_SECRET) or "")
    live = {True: "live", False: "test"}
    pick = next((a for a in accounts if not want or live[bool(a.get("livemode"))] == want), None)
    if pick is None:
        have = ", ".join(f"{a.get('name')} ({live[bool(a.get('livemode'))]})" for a in accounts)
        return None, (f"'{COLLECTING_SECRET}' is a {want}-mode key and the owner's Stripe approval "
                      f"covers only {have}. A webhook is created in the mode of the account that "
                      f"creates it, so approve the {want} account at Stripe's environment screen")
    return {"firm": True, "stripe_context": pick["stripe_context"], "livemode": bool(pick.get("livemode")),
            "approval_token": (body.get("approval_token") or "").strip()}, None


def _firm_write(creds, operation, parameters):
    args = {"stripe_api_operation_id": operation, "parameters": parameters,
            "stripe_context": creds["stripe_context"], "livemode": creds["livemode"]}
    if creds.get("approval_token"):
        args["human_confirmation"] = {"approval_token": creds["approval_token"]}
    out = firm_gateway.call("stripe___stripe_api_write", args)
    approval = _approval(out)
    if approval:
        raise ApprovalRequired(*approval)
    return out


def _firm_read(creds, operation, parameters):
    return firm_gateway.call("stripe___stripe_api_read", {
        "stripe_api_operation_id": operation, "parameters": parameters,
        "stripe_context": creds["stripe_context"], "livemode": creds["livemode"]})


def _approval(out):
    """(url, approval_id) when Stripe answered a write with a human-approval request, else None."""
    if not isinstance(out, dict):
        return None
    blob = json.dumps(out)
    if "approval" not in blob.lower():
        return None
    url = out.get("approval_url") or out.get("url") or (out.get("human_confirmation") or {}).get("approval_url")
    aid = out.get("approval_request_id") or out.get("approval_id") or (out.get("human_confirmation") or {}).get("approval_request_id")
    if url or aid:
        return url or "", aid or ""
    text = out.get("text", "")
    if "approv" in text.lower() and "http" in text:
        url = next((w for w in text.split() if w.startswith("http")), "")
        return url.rstrip(".,)"), ""
    return None


def _call(creds, api_base, path, data=None, method="GET"):
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=urllib.parse.urlencode(data).encode() if data is not None else None,
        method=method,
    )
    req.add_header("Authorization", f"Bearer {creds['api_key']}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def configure(creds, url, api_base):
    """POST /v1/webhook_endpoints; return the signing secret, leaving exactly one at this url.

    `secret` appears only in the CREATE response, never on a later retrieve, so it is captured
    here or not at all — which is why re-running setup CREATES rather than reusing what is there.
    POST has no upsert, so without the sweep below each run left another live endpoint delivering
    every event again, signed with a secret the vault no longer holds.

    Create first, then sweep. The reverse would leave a window with no endpoint at all, and an
    event in that window is simply lost; an overlap merely delivers twice, which the ingest
    lambda's event-id dedup already absorbs.

    The sweep needs `webhook_read` on the key. Without it the create still succeeds and the
    duplicates stay — setup working is not worth failing over a listing permission.
    """
    # `api_version` decides the shape Stripe POSTS to us, and it is fixed at creation — no header
    # we send reaches it, and it cannot be edited afterwards. Left unset the endpoint delivers the
    # ACCOUNT DEFAULT, a dashboard setting, so an upgrade clicked there changes live payload shapes
    # with no deploy. Same constant the outbound calls pin, so one string governs both directions.
    if creds.get("firm"):
        obj = _firm_write(creds, "PostWebhookEndpoints",
                          {"url": url, "api_version": stripe_api.VERSION, "enabled_events": ENABLED_EVENTS})
        if "secret" not in obj:
            raise RuntimeError(f"Stripe's answer carried no endpoint: {json.dumps(obj)[:300]}")
        try:
            existing = _firm_read(creds, "GetWebhookEndpoints", {"limit": 100}).get("data") or []
        except Exception as e:  # noqa: BLE001
            log.warning("stripe webhook endpoint listing failed; older endpoints at this url not swept",
                        endpoint_id=obj.get("id"), error=str(e))
            return obj["secret"], {"duplicate_sweep_skipped": True, "account": creds["stripe_context"],
                                   "mode": "live" if creds["livemode"] else "test"}
        # Stripe's server exposes list, create, get and update for webhook endpoints and no
        # delete, so an older endpoint at this url is DISABLED: delivery stops the same way and
        # the dashboard still shows it
        for e in existing:
            if e.get("url") == url and e.get("id") != obj["id"] and e.get("status") != "disabled":
                try:
                    _firm_write(creds, "PostWebhookEndpointsWebhookEndpoint", {"id": e["id"], "disabled": True})
                except Exception as del_e:  # noqa: BLE001
                    log.warning("stripe duplicate webhook endpoint not disabled",
                                endpoint_id=e["id"], error=str(del_e))
        return obj["secret"], {"account": creds["stripe_context"], "mode": "live" if creds["livemode"] else "test"}

    body = ([("url", url), ("api_version", stripe_api.VERSION)]
            + [("enabled_events[]", e) for e in ENABLED_EVENTS])
    obj = _call(creds, api_base, "/v1/webhook_endpoints", body, "POST")

    try:
        existing = _call(creds, api_base, "/v1/webhook_endpoints?limit=100")["data"]
    except Exception as e:  # noqa: BLE001 — see the read-permission note above
        log.warning("stripe webhook endpoint listing failed; older endpoints at this url not swept",
                    endpoint_id=obj.get("id"), error=str(e))
        return obj["secret"], {"duplicate_sweep_skipped": True}
    for e in existing:
        if e.get("url") == url and e.get("id") != obj["id"]:
            try:
                _call(creds, api_base, f"/v1/webhook_endpoints/{e['id']}", method="DELETE")
            except Exception as del_e:  # noqa: BLE001
                log.warning("stripe duplicate webhook endpoint not deleted",
                            endpoint_id=e["id"], error=str(del_e))
    return obj["secret"], {}
