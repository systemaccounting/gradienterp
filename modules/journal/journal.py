"""The client half of `post_journal_entry` — how any module hands accounting an entry.

Eleven modules were each carrying their own copy of this invoke, and every copy ended the same way:

    body = json.loads(result.get("body", "{}"))
    return body.get("entryId")          # None when accounting REFUSED

`None` reads exactly like "no entry was needed", so every caller treated a refusal as a success and
advanced its own row. A PO naming an account outside the chart returned
`{"status": "received", "journal_entry_id": null}` with a 200: goods received, payable never
booked, nothing logged, nobody told. `post` raises instead.

WHAT COUNTS AS ACCEPTED. Both 2xx outcomes do:

    200  the entry is on the ledger
    202  it is parked in pending, because a line arrived without an `accountType` and only the
         owner or the agent can say what it is. The entry is not lost — `classify_pending`
         completes and posts it — so this is not a failure and must not raise.

Everything else is a refusal: an unknown account name, a non-positive amount, debits that do not
equal credits, or the callee throwing. Those are all conditions the CALLER got wrong, and none of
them are fixed by carrying on as if the money had moved.

Bundled beside the caller's `main.py` (`modules/accounting/journal.py`, imported as `journal`).
"""

import json
import os
from decimal import Decimal

from aws import client as _aws


class Refused(Exception):
    """accounting declined the entry. Carries what it said, so the log line names the reason."""

    def __init__(self, status, body, payload):
        body = body if isinstance(body, dict) else {"error": str(body)}
        self.status = status
        self.body = body
        # everything accounting said BESIDES the message — `unknownAccounts`, `badLineItems`,
        # `totalDebits`/`totalCredits`. That is the actionable half: the message says a name is
        # unknown, the detail says WHICH, and a caller relaying only the message strands the owner.
        self.detail = {k: v for k, v in body.items() if k != "error"}
        self.error = ", ".join(
            [str(body.get("error") or body)] + [f"{k}={v}" for k, v in sorted(self.detail.items())])
        self.payload = payload
        super().__init__(f"post_journal_entry refused ({status}): {self.error} "
                         f"[entryId={payload.get('entryId')} source={payload.get('source')}]")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def post(payload: dict, fn: str | None = None) -> str:
    """Invoke accounting's post_journal_entry. Returns the entryId, or raises `Refused`."""
    resp = _aws("lambda").invoke(
        FunctionName=fn or os.environ["POST_JOURNAL_ENTRY_FN"],
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, cls=_DecimalEncoder),
    )
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        raise Refused(500, {"error": (raw or b"")[:500].decode(errors="replace")}, payload)
    result = json.loads(raw or b"{}")
    status = result.get("statusCode")
    body = json.loads(result.get("body") or "{}")
    if status not in (200, 202) or not body.get("entryId"):
        raise Refused(status, body, payload)
    return body["entryId"]
