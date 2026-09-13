"""The bff stack (gradienterp.cloud, the owner web app), locally.

    bash scripts/local-dev.sh --start        # this on :3000, a registered Cognito callback origin
    curl -s localhost:3000/api/gerps -H 'x-debug-sub: <your-sub>'

A real Hosted-UI login works against it: the bearer arrives and `_image` decodes its claims without
verifying, which is the one thing local cannot do — production's APIGW JWT authorizer validates the
token before the handler ever sees it.

    python3 tests/server/snapshot.py bff     # re-take the manifest
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _image import OPERATOR_TABLES, REPO, build  # noqa: E402


def _load_dotenv():
    """Repo-root .env → os.environ. Local dev config (LOCAL_GERPS); .env is gitignored. Explicit
    env wins, the standard dotenv contract."""
    import os
    env = REPO / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _account_id() -> str:
    """This stack's AWS account, read off any arn in the per_customer config rather than typed."""
    import json
    img = HERE.parent / "per_customer" / "image.json"
    if img.exists():
        for cfg in json.loads(img.read_text())["functions"].values():
            for v in cfg.get("env", {}).values():
                if isinstance(v, str) and v.startswith("arn:aws:"):
                    parts = v.split(":")
                    if len(parts) > 4 and parts[4].isdigit():
                        return parts[4]
    return "000000000000"


def _seed(image):
    """WEB_DIR is `/var/task/web` in the deployed function — a path that only exists inside a
    Lambda. Point it at the repo so the handler serves the same files `static` does.

    On the IMAGE, not just `os.environ`: `run()` re-applies each function's snapshotted env before
    every request, so a value set only in the environment is overwritten on the first call. Any
    snapshotted `/var/task/...` path needs the same treatment.
    """
    web = str(REPO / "prod" / "gradienterp_cloud" / "web")
    for cfg in image.get("functions", {}).values():
        if "WEB_DIR" in (cfg.get("env") or {}):
            cfg["env"]["WEB_DIR"] = web
        # no Cognito pool locally — an account deleted here has no user to delete; and no tower,
        # so a changed login has no Identity Center user to reach
        for k in ("USER_POOL_ID", "OWNER_EMAIL_FN", "BUSINESS_INFO_FN"):
            if k in (cfg.get("env") or {}):
                cfg["env"][k] = ""
    import os
    os.environ["WEB_DIR"] = web
    _seed_gerps(image)
    # the seller's card lambdas run in this process too (bff depends on per_customer); point them
    # at the local Stripe stand-in and put the key where they read it
    import _stripe_local
    _stripe_local.point_image_at_fake(image)
    _stripe_local.seed_secrets(image)
    # the seller's customer-contact hook, published on this stack (see _hooks_local)
    import _hooks_local
    _hooks_local.point_image_at_local(image)
    _hooks_local.seed_customer_hook(image)


def _seed_gerps(image):
    """The gerps an account holds, as provisioning leaves them: an instance row on gerp-customers
    (owner_sub is who created it) and the membership row, which is what the ownership check reads. Seeded
    from LOCAL_GERPS so the dogfood login lands on its own gerps."""
    import json
    import os
    raw = (os.environ.get("LOCAL_GERPS") or "").strip()
    if not raw:
        return
    data = json.loads(raw) if raw.startswith("{") else (
        json.loads(pathlib.Path(raw).read_text()) if pathlib.Path(raw).exists() else {})
    from aws import client
    ddb = client("dynamodb")
    for sub, gerps in data.items():
        for g in gerps:
            gid = g.get("gerp_id") or g.get("customer_id")   # `customer_id` predates the id rename
            if not gid:
                continue
            # `status` carries awaiting_payment, which the card renders differently from a gerp
            # that is provisioning — both lack a gateway_url, so the row is the only thing that
            # tells them apart
            row = {k: g[k] for k in ("gateway_url", "chat_url", "label", "status") if g.get(k)}
            # Point the forward at the LOCAL per_customer stack by default, so browser → bff →
            # gateway → moto is one local chain. The real deployed gateway rejects a locally-issued
            # token (its authorizer is real), which is correct and is why this override exists;
            # unset LOCAL_GATEWAY_URL to exercise the live gateway with a real Hosted-UI login.
            if os.environ.get("LOCAL_GATEWAY_URL"):
                row["gateway_url"] = os.environ["LOCAL_GATEWAY_URL"]
            # The event dispatcher resolves a recipient gerp_id to its aws_account_id and then
            # invokes cross-account. Locally "another account" is the same process, but the row
            # still has to CARRY the field or the dispatcher drops the event as unroutable — which
            # is what it should do for a gerp it has never heard of.
            row.setdefault("aws_account_id", _account_id())
            ddb.put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item={
                "gerp_id": {"S": gid}, "owner_sub": {"S": sub},
                **{k: {"S": v} for k, v in row.items()}})
            ddb.put_item(TableName=os.environ["MEMBERS_TABLE"], Item={
                "account_id": {"S": sub}, "gerp_id": {"S": gid}, "role": {"S": "owner"}})


# the bff READS platform's four singletons and owns no tables of its own (scripts/tags.json).
# $default serves the SPA from disk, so a web edit shows on reload with no rebuild.
app = build(HERE / "image.json", tables=lambda t: t in OPERATOR_TABLES, seed=_seed,
            static=REPO / "prod" / "gradienterp_cloud" / "web")

# /dev/* — the local stack put into a state by url (dev.py). Mounted AHEAD of the image's routes:
# `$default` is a catch-all bound last, and anything added after it would never be reached.
from tests.server.bff.dev import router as _dev_router  # noqa: E402
app.include_router(_dev_router)                      # appends ONE included-router entry, last
app.router.routes.insert(0, app.router.routes.pop())  # ahead of everything, the catch-all included
