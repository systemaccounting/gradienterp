"""The local Stripe stand-in, from the side of the stacks that call it.

Every lambda that talks to Stripe reads `STRIPE_API_BASE` from its env and builds paths under it, so
nothing is patched: the real handlers run against `tests/server/stripe/server.py` on :4242. This
module is the two things the OTHER surfaces need for that — the override on their image, and the
secrets seeded where the lambdas will look for them.

Leave `LOCAL_STRIPE_URL` unset to keep the lambdas on real Stripe (a test key under the local
prefix runs test mode, as before); set it to `off` explicitly to say so.
"""

import os

KEY = "sk_local"                 # the fake accepts any bearer; the value exists so "no secret" is not the branch that runs
SIGNING_SECRET = "whsec_local"   # what the fake signs webhooks with, and what ingest_stripe verifies against


def url() -> str:
    u = os.environ.get("LOCAL_STRIPE_URL", "http://127.0.0.1:4242")
    return "" if u == "off" else u


def point_image_at_fake(image: dict) -> None:
    """On the IMAGE, the way WEB_DIR is: the runtime re-applies each function's snapshotted env per
    request, so a value set only in the environment is overwritten on the first call."""
    base = url()
    if not base:
        return
    for cfg in image.get("functions", {}).values():
        env = cfg.setdefault("env", {})
        if "STRIPE_API_BASE" in env or "payments" in str(cfg.get("src_dir", "")):
            env["STRIPE_API_BASE"] = base
    os.environ["STRIPE_API_BASE"] = base


def seed_secrets(image: dict) -> None:
    """The key and the signing secret under every secret prefix a function in this image reads.
    The snapshotted env carries the tenant prefix; a function with CUSTOMER_ID unset derives the
    `local` one — so both get seeded, and the lambdas find whichever they compute."""
    if not url():
        return
    from aws import client
    ssm = client("ssm")
    prefixes = {"/gradienterp/customers/local/secrets"}
    for cfg in image.get("functions", {}).values():
        p = (cfg.get("env") or {}).get("SECRET_PARAM_PREFIX")
        if p:
            prefixes.add(p.rstrip("/"))
    for prefix in prefixes:
        for name, value in ((f"{prefix}/stripe_billing", KEY),
                            (f"{prefix}/stripe_setup", KEY),
                            (f"{prefix}/stripe/signing_secret", SIGNING_SECRET)):
            ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
