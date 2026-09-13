"""approve_automation — copies a reviewed script from staged/ into the prefix its kind runs from.

This is the only principal in the account that can write under `automations/approved/`.
`manage_storage` — the tool the agent uses to author scripts — carries an explicit Deny
on that prefix, so the turn that writes a script cannot also bless it.

Approval is the copy. The approved object is a SEPARATE object the author cannot write,
so rewriting the staged copy afterwards leaves the running script untouched:
approve-then-swap-the-body fails on the write rather than on a comparison somebody has
to remember to make. Nothing has to pin a version id.

The kind is the destination prefix, and it decides the script's reach — `modules/` is
read by the runner holding the tool allowlist, `external/` by the one holding internet
and env secrets. A script filed under the wrong kind gets AccessDenied at run time
instead of running with reach nobody reviewed it for.

Gated on a TICKET, which `review_automation` creates and only it can create. So approval
means a review just passed against these exact bytes, rather than an agent having been
asked nicely. The staged version is re-read here and matched against the ticket, so a
script edited between the review and the approval fails rather than slipping through.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

import _reviews
from aws import log
from _helpers import err, ok

BUCKET = os.environ["CABINET_BUCKET"]
STAGED_PREFIX = os.environ.get("STAGED_PREFIX", "automations/staged/")
APPROVED_PREFIX = os.environ.get("APPROVED_PREFIX", "automations/approved/")
from _helpers import kind_or_error

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    script = (body.get("script") or "").strip()
    kind, problem = kind_or_error(script)
    if problem:
        return err(problem)
    ticket = (body.get("ticket") or "").strip()

    if not script:
        return err("script is required — the key under automations/staged/, e.g. "
                   "'collections/charge_next_card.py'")
    # No path check: this role reads only its own prefix, so a name that walks out resolves to a
    # key IAM refuses. Banning `/` bought nothing and kept every script in one flat prefix.

    src = STAGED_PREFIX + script
    dst = f"{APPROVED_PREFIX}{kind}/{script}"

    # what is staged NOW, not what was staged when the review ran
    try:
        staged = s3().head_object(Bucket=BUCKET, Key=src)
    except Exception as e:
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return err(f"no staged script at {src}", 404)
        log.error("staged script head failed", script=script, key=src, error=str(e))
        return err(f"could not read the staged script at {src}: {e}", 502)

    try:
        _reviews.spend(script, ticket, kind, staged.get("VersionId") or "")
    except _reviews.ReviewError as e:
        return err(str(e), 403)

    try:
        # read-then-write rather than CopyObject. A server-side copy carries the source's
        # annotations and tags, which needs permissions beyond GetObject/PutObject that this
        # role deliberately does not have — and the approved object is better as an explicit
        # write of bytes this lambda actually read than as a copy of something it never saw.
        body = s3().get_object(Bucket=BUCKET, Key=src)["Body"].read()
        s3().put_object(Bucket=BUCKET, Key=dst, Body=body, ContentType="text/x-python")
    except s3().exceptions.NoSuchKey:
        return err(f"no staged script at {src}", 404)
    except Exception as e:
        log.error("approve copy failed", script=script, ticket=ticket, key=dst, error=str(e))
        # nothing was approved, so the review still stands — give the ticket back rather
        # than making a platform fault cost the owner another review
        _reviews.release(script, ticket)
        return err(f"could not approve {script}: {e}", 502)

    head = s3().head_object(Bucket=BUCKET, Key=dst)
    return ok({
        "script": script,
        "kind": kind,
        "key": dst,
        "version_id": head.get("VersionId"),
    })
