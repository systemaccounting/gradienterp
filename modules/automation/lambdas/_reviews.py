"""The review record — every review a script has ever had, and the capability a passing one carries.

One row per review, PASS OR NOT. The findings are the substance: they are what the owner
reads, what an escalation to a human review carries, and the only record of why a script
that runs against the firm's books was allowed to. A table that kept only the passes would
throw away the argument.

    pk = script            every review of one script, in order — the history is a query
    sk = review_id

A passing review is also a **one-shot capability**. `approve_automation` spends it, and
because the reviewer creates and the approver spends — separate roles, separate DynamoDB
actions — an agent can carry one and never make one. Bound to the staged version id with a
short spendable window, four things hold without anyone remembering to check them:

  - nothing is approved without a review
  - a review of one script cannot approve another
  - bytes that changed after the review was read cannot be approved
  - yesterday's pass is not spendable today

The spend is a conditional update rather than read-then-write: two approvals racing the
same review must not both win. And `spendable_until` is not a TTL — the row outlives the
capability, because the record is the point.
"""

import os
import time
import uuid

import boto3
from aws import log

TABLE = os.environ.get("REVIEWS_TABLE", "")
SPEND_WINDOW = int(os.environ.get("REVIEW_SPEND_SECONDS", "1800"))
PASS = "approve"

_ddb = None


def table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(TABLE)
    return _ddb


class ReviewError(RuntimeError):
    pass


def record(script: str, kind: str, version_id: str, verdict: str, findings: str) -> dict:
    """Write what the review concluded. Returns the ticket only when it passed."""
    review_id = uuid.uuid4().hex
    now = int(time.time())
    item = {
        "script": script,
        "review_id": review_id,
        "kind": kind,
        "version_id": version_id or "",
        "verdict": verdict,
        "findings": (findings or "")[:4000],
        "created_at": now,
    }
    if verdict == PASS:
        item["spendable_until"] = now + SPEND_WINDOW
    table().put_item(Item=item)

    out = {"review_id": review_id}
    if verdict == PASS:
        out |= {"ticket": review_id, "expires_in": SPEND_WINDOW}
    return out


def spend(script: str, ticket: str, kind: str, version_id: str) -> dict:
    """Consume a passing review, or raise saying it does not apply.

    Every condition is in the ConditionExpression, not in Python, so a concurrent approve
    cannot slip between the read and the write.
    """
    if not ticket:
        raise ReviewError("a review ticket is required — run review_automation first")

    now = int(time.time())
    try:
        resp = table().update_item(
            Key={"script": script, "review_id": ticket},
            UpdateExpression="SET consumed_at = :now",
            ConditionExpression=(
                "attribute_exists(review_id) AND attribute_not_exists(consumed_at) "
                "AND verdict = :pass AND kind = :l AND version_id = :v AND spendable_until > :now"
            ),
            ExpressionAttributeValues={":now": now, ":pass": PASS, ":l": kind, ":v": version_id or ""},
            ReturnValues="ALL_NEW",
        )
    except Exception as e:
        if "ConditionalCheckFailed" not in type(e).__name__ and "ConditionalCheckFailed" not in str(e):
            raise
        # One message for every failure mode on purpose: the ticket is opaque to the caller
        # and the fix is the same in each case — review the current bytes again.
        raise ReviewError(
            f"this ticket does not approve {script} as it stands now — it may be spent, expired, "
            "for another kind, from a review that did not pass, or issued against a version that "
            "has since changed. Run review_automation again."
        ) from e

    return resp.get("Attributes", {})


def release(script: str, ticket: str):
    """Un-spend a review whose approval then failed.

    Spending happens BEFORE the copy on purpose — unreviewed bytes must never reach the
    approved prefix. But that means a copy that fails for a platform reason (a missing KMS
    grant, a throttle) leaves a dead ticket and costs the owner a whole re-review for
    something they did not do. Nothing was approved, so the review is still good.

    Safe because the binding is unchanged: the released ticket still only approves the same
    script, kind and version it was issued against.
    """
    try:
        table().update_item(
            Key={"script": script, "review_id": ticket},
            UpdateExpression="REMOVE consumed_at",
            ConditionExpression="attribute_exists(review_id)",
        )
    except Exception as e:
        # best effort — the copy failure is what the caller needs to hear about
        log.warning("review release failed", script=script, ticket=ticket, error=str(e))
