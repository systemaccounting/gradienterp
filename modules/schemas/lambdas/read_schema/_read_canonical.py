import json
import os

from _registries import registries, unknown_registry

from aws import client as _aws

# Where the canonical JSON lives is config, not environment: the tower publishes it to a bucket,
# and without one the repo's own modules/schemas/data holds the same files.
CANONICAL_BUCKET = os.environ.get("CANONICAL_BUCKET", "")
LOCAL_CANONICAL_DIR = os.environ.get("LOCAL_CANONICAL_DIR", "modules/schemas/data")


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    registry = body.get("registry")

    # No registry named → the list itself. The agent's canonical-pull needs to know what
    # there IS to diff, and the bucket is the only place that knows.
    if not registry:
        return {"statusCode": 200, "body": json.dumps({"registries": registries()})}

    if registry not in registries():
        return unknown_registry(registry)

    key = f"{registry}.json"

    if CANONICAL_BUCKET:
        obj = _aws("s3").get_object(Bucket=CANONICAL_BUCKET, Key=key)
        content = json.loads(obj["Body"].read())
    else:
        with open(os.path.join(LOCAL_CANONICAL_DIR, key)) as f:
            content = json.load(f)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "registry": registry,
            "content": content,
        }),
    }
