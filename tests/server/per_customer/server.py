"""The per_customer stack, locally. Everything general is in `tests/server/_image.py`.

    bash scripts/local-dev.sh --start
    curl -s localhost:8080/healthz

A webhook here is verified the way it is in Lambda, so an unsigned curl gets 400. Signed deliveries
come from the Stripe stand-in (:4242) when a card is charged; `curl localhost:3000/dev` lists the
verbs that put the stack into that state. `/oob/*` answers 404 until the gerp is published.

    python3 tests/server/snapshot.py per_customer      # re-take the manifest
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _image import OPERATOR_TABLES, REPO, build, local_gerps  # noqa: E402


def _seed(image):
    """The registries are fail-closed: post_journal_entry refuses an account outside the chart, and
    every module validates its field set the same way. An empty registry is not an empty stack, it
    is a stack that rejects everything."""
    from helpers.localaws import real_name, seed_registry
    registries = sorted(p.stem for p in (REPO / "modules/schemas/data").glob("*.json")
                        if p.stem != "rule_params")
    seed_registry(real_name("schema", image["gerp"]), *registries)
    # cards and charges go to the local Stripe stand-in (tests/server/stripe); the webhook it
    # sends back lands on this surface's /webhooks/stripe, signed with the seeded secret
    import _stripe_local
    _stripe_local.point_image_at_fake(image)
    import _hooks_local
    _hooks_local.point_image_at_local(image)
    _stripe_local.seed_secrets(image)
    _seed_owner(image)


def _seed_owner(image):
    """The owner the web doors answer (`aws.refuse_non_owner`), written where provisioning writes it: the
    sub in LOCAL_GERPS that holds this stack's gerp. With none, the doors refuse every signed-in caller,
    as they do in Lambda."""
    import os
    from aws import client
    param = os.environ.get("OWNER_SUB_PARAM")
    subs = [sub for sub, gerps in local_gerps().items()
            if any((g.get("gerp_id") or g.get("customer_id")) == image["gerp"] for g in gerps)]
    if param and subs:
        client("ssm").put_parameter(Name=param, Value=subs[0], Type="String", Overwrite=True)


# the operator singletons belong to `platform`, not here — see scripts/tags.json
app = build(HERE / "image.json", tables=lambda t: t not in OPERATOR_TABLES, seed=_seed)
