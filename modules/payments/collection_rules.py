"""collection rules — what a firm attaches to an invoice transition to get paid.

`charge_saved_card` sends an event on the firm's own bus; `infra/collection.tf` routes it to
`charge_saved_method`. The instance row (`INVOICE#<status>`, the firm's choice of status) is the
whole configuration.

It sends rather than calling payments directly: the callsite has already answered its caller, so a
failed call would be seen by nobody.
"""

import json
import os

from aws import client as _aws
from rules import rule


def _bus() -> str:
    """The firm's own bus (`modules/events/infra`), from the callsite lambda's env. Empty is a wiring
    error rather than a fallback to `default`, where nothing is listening."""
    bus = os.environ.get("INTERNAL_BUS_NAME")
    if not bus:
        raise RuntimeError(
            "INTERNAL_BUS_NAME is not set — this lambda runs collection rules but has no bus to "
            "announce on. Grant events:PutEvents on the firm's internal bus and pass its name.")
    return bus


@rule
def charge_saved_card(ctx):
    """Ask payments to charge whatever card the payer saved earlier.

    No parameters — whether it is attached and to which status are both in the instance row.
    `charge_saved_method` refuses cleanly when the payer saved no card.
    """
    _aws("events").put_events(Entries=[{
        "EventBusName": _bus(),
        "Source":       "invoicing",
        "DetailType":   "collection.requested",
        # The id only — payments reads the invoice fresh, since a partial payment can land first.
        "Detail":       json.dumps({"invoice_id": ctx["invoice_id"]}),
    }])
    return [{"requested": "charge_saved_method", "invoice_id": ctx["invoice_id"]}]
