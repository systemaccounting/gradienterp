"""Shared by every schemas lambda: what the registries ARE.

The canonical bucket is the list. One `<registry>.json` per registry, so the names are
derived from its keys rather than repeated as a tuple in each handler — a registry added
later (item_fields, labor_fields) is then visible to tools written before it existed. The
hardcoded tuple is what let those two get seeded at provisioning and never pulled again.

Two keys in the bucket are NOT field registries:
  rule_params     platform VALUES assigned to rule fields (bracket tables, wage bases).
                  A different kind of data with a different destination — seed_schema
                  writes them straight to the rules-params table, and no owner approves them.
  profile_fields  the a2a public profile registry. Operator-side (prod/optimizer), not
                  per-gerp, so it never lands in a customer's schema table.

Local mode reads the same names off the canonical source dir, so a test sees what prod sees.
"""

import os

from aws import client as _aws_client, resource as _aws_resource

NOT_REGISTRIES = {"rule_params", "profile_fields"}

_cache = {}


def _source():
    # Env is read per call, not at import: the source has to track the environment a test
    # scratches in, and a warm lambda's env never changes anyway.
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return ("s3", os.environ.get("CANONICAL_BUCKET", ""))
    return ("dir", os.environ.get("LOCAL_CANONICAL_DIR", "modules/schemas/data"))


def _canonical_names(kind, where):
    if kind == "s3":
        keys = []
        pages = _aws_client("s3").get_paginator("list_objects_v2").paginate(Bucket=where)
        for page in pages:
            keys += [obj["Key"] for obj in page.get("Contents", [])]
    else:
        keys = os.listdir(where) if os.path.isdir(where) else []
    return {k[:-5] for k in keys if k.endswith(".json")}


def registries():
    """The canonical field registries, sorted. Cached per source for the container's life —
    a registry added to the bucket lands on the next cold start, the same latency a code
    deploy already had."""
    source = _source()
    if source not in _cache:
        _cache[source] = sorted(_canonical_names(*source) - NOT_REGISTRIES)
    return _cache[source]


def unknown_registry(registry):
    """The 400 body for a bad registry name. Names the valid ones so the agent can retry —
    the tool schemas carry no enum (it would freeze the list again), so this IS the discovery path."""
    import json
    return {
        "statusCode": 400,
        "body": json.dumps({"error": f"unknown registry: {registry}", "registries": registries()}),
    }
