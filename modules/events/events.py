"""events — the one place an event is built and sent.

Emission splits by REACH, and the reach is the choice a caller makes by which function it calls:

    emit(source, detail_type, detail)             the firm's OWN bus — in-firm, nobody else sees it
    emit_to(source, recipient, detail_type, …)    the recipient's HUB bus, addressed — stamps
                                                  from/to and from_hub/to_hub
    publish(source, detail_type, detail, …)       the shared bus, for everyone — stamps the
                                                  publication envelope

Two buses, two blast radii (`modules/events/AGENTS.md`). A module holds `events:PutEvents` on one
bus ARN by name, so one wired only for in-firm messages gets AccessDenied from the shared one — and
that is a property worth keeping, which is why the bus is picked by the function rather than by
which environment variable the caller reached for.

An addressed event goes straight to the recipient's hub: `emit_to` reads the recipient's row in
the platform directory (`gerp-directory`, an operator table every gerp may read; `DIRECTORY_TABLE_ARN`,
the replica in this region), which names the hub and its bus arn, and puts there — cross-region
when the recipient lives elsewhere. The hub's spoke rule delivers. A recipient with no row does
not exist (or is closed) and is refused here, at the sender, where something can be done about
it. One read per recipient, kept for the process's life. A caller that starts a thread calls
`resolve(recipient)` before its durable write and turns `UnknownRecipient` into its own answer;
`emit_to` after a write never raises, and an unknown recipient there is one incident line.

**Fire-and-forget, always.** Emission is downstream of the durable write: the journal (or the stock
ledger, or the invoice) is authoritative and the bus is visibility. A PutEvents failure logs an
incident line and returns; it never raises, because a failure to ANNOUNCE a write must not report
the write as failed.

**Local mode** appends to `LOCAL_EVENTS` (default `out/events.jsonl`) and sends nothing, so a test
reads back exactly what would have gone out.

This exists because four modules had each written the same `emit_event` — byte-identical apart from
the source literal, which is the module's own name. A fifth, `emit_distribution_paid`, is the same
shape for the publication reach.
"""

import json
import logging
import os
from decimal import Decimal

from aws import client as _aws, resource as _aws_resource

log = logging.getLogger()

# Read at call time, not import: a harness points it at a scratch file per case, and a
# module-level snapshot would send the first test's value for the whole run.
LOCAL_EVENTS = ""
SCHEMA_VERSION = 1


def _gerp_id() -> str:
    """Read per call, not at import: a test harness rebinds the env between cases."""
    return os.environ.get("GERP_ID") or os.environ.get("CUSTOMER_ID", "local")


class _DecimalEncoder(json.JSONEncoder):
    """DDB hands back Decimal and json refuses it. Whole values go out as ints so a quantity does
    not arrive as 3.0."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _openly_operated() -> bool:
    """Current publication consent, read PER INVOKE, never cached.

    Consent is a reference, not a snapshot: a cold-start cache kept stamping `true` after a gerp
    turned publication off, and the publication rule routes flagged events to a firehose ARCHIVE,
    which is unrecoverable. One GetItem is the price of that being right.
    """
    name = os.environ.get("SETTINGS_TABLE")
    if not name:
        return False
    row = _aws_resource("dynamodb").Table(name).get_item(
        Key={"gerp_id": _gerp_id(), "sk": "GERP#openly_operated"}
    ).get("Item") or {}
    return bool(row.get("value", False))


def _send(bus: str, source: str, detail_type: str, detail: dict) -> dict:
    """Put one entry, or append it to the local jsonl. Never raises.

    The failure line is the shape `create_inc_from_log` reads, keyed on the event type: a bus that
    is failing fails EVERY emit of that type, so plain logging writes the same line thousands of
    times and nobody reads any of them. Keyed, they collapse into one incident with a strike count.
    """
    entry = {"EventBusName": bus, "Source": source, "DetailType": detail_type,
             "Detail": json.dumps(detail, cls=_DecimalEncoder)}
    local = LOCAL_EVENTS or os.environ.get("LOCAL_EVENTS", "")
    if local:
        with open(local, "a") as fh:
            fh.write(json.dumps({"source": source, "detail-type": detail_type,
                                 "detail": detail}, cls=_DecimalEncoder) + "\n")
        return {"emitted": detail_type, "local": True}
    try:
        # a bus arn names its region; the put goes through a client for it (a recipient's hub
        # may be elsewhere). A bare name is this region's
        region = bus.split(":")[3] if bus.startswith("arn:") and bus.count(":") >= 5 else ""
        _regional("events", region).put_events(Entries=[entry])
        return {"emitted": detail_type}
    except Exception as e:  # noqa: BLE001 — the write already happened; this only announces it
        print(json.dumps({
            "event": "emit_fail", "incident": "fail",
            "subject": f"event:{source}.{detail_type}",
            "category": "events",
            "label": f"Event `{source}.{detail_type}` is not reaching the bus",
            "error": str(e),
        }))
        return {"emitted": None, "error": str(e)}


def emit(source: str, detail_type: str, detail: dict) -> dict:
    """In-firm, on the firm's OWN bus. Nothing leaves the gerp and no envelope is owed — the
    consumers are this firm's own modules, which know what they are reading."""
    bus = os.environ.get("INTERNAL_BUS_NAME")
    if not bus:
        raise RuntimeError(
            "INTERNAL_BUS_NAME is not set — this lambda emits in-firm events but has no bus to "
            "put them on. Wire the firm's own bus into its environment.")
    return _send(bus, source, detail_type, detail)


class UnknownRecipient(LookupError):
    """No directory row for the recipient: a gerp that does not exist, or is closed."""


_directory: dict = {}   # recipient → its directory row, for the process's life
_clients: dict = {}     # (service, region) → a client for another region, kept like the shared ones


def _in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _regional(service: str, region: str):
    if not region:
        return _aws(service)
    key = (service, region)
    if key not in _clients:
        _clients[key] = _aws(service, region_name=region)
    return _clients[key]


def resolve(recipient: str) -> dict:
    """The recipient's directory row: {hub, hub_bus_arn, region, aws_account_id}."""
    if recipient in _directory:
        return _directory[recipient]
    arn = os.environ.get("DIRECTORY_TABLE_ARN", "")
    if not arn:
        raise RuntimeError(
            "DIRECTORY_TABLE_ARN is not set — this lambda addresses events to other firms but has "
            "no directory to find their hub in.")
    region = arn.split(":")[3]
    # the table is the operator's: the full arn as TableName is what reaches a table in another
    # account under its resource policy (a bare name resolves in the caller's own account). The
    # local emulator knows tables by name only
    table = arn if _in_lambda() else arn.rsplit("/", 1)[-1]
    item = _regional("dynamodb", region).get_item(TableName=table, Key={"gerp_id": {"S": recipient}}).get("Item")
    if not item:
        raise UnknownRecipient(f"{recipient} is not a gerp the directory knows")
    row = {k: v.get("S", "") for k, v in item.items()}
    _directory[recipient] = row
    return row


def emit_to(source: str, recipient: str, detail_type: str, detail: dict) -> dict:
    """Addressed to ONE firm, on the RECIPIENT'S hub bus. `to` is what the hub's spoke rule routes
    on, `to_hub` says which hub that is, and `from` is stamped here, so a recipient never has to
    trust a sender's word for who sent it (EventBridge stamps the sender's account beside it).

    No publication envelope: an addressed event is not published, and `openly_operated` decides
    only whether something lands in the public archive.

    With no directory wired (`DIRECTORY_TABLE_ARN` unset) the put goes to the sender's own hub
    and nothing is stamped: the local suites run so; in prod every addressed emitter carries the
    directory (the shape test in tests/server)."""
    stamped = {"from": _gerp_id(), "to": recipient, **detail}
    if not os.environ.get("DIRECTORY_TABLE_ARN"):
        # no directory wired: the sender's own hub, which is every recipient's hub while the
        # platform has one. The shape test in tests/server holds every addressed emitter to
        # carrying the directory in prod; the local suites run their buses without one
        return _send(_shared_bus(), source, detail_type, stamped)
    try:
        row = resolve(recipient)
    except UnknownRecipient as e:
        # fire-and-forget holds here too: the caller's durable write is done. A caller that can
        # still act (one starting a thread) calls `resolve` BEFORE its write and answers its
        # own caller; a reply to a gerp closed mid-thread lands here, once, as an incident
        print(json.dumps({
            "event": "emit_fail", "incident": "fail",
            "subject": f"recipient:{recipient}",
            "category": "events",
            "label": f"Event `{source}.{detail_type}` is addressed to `{recipient}`, a gerp the directory does not hold",
            "error": str(e),
        }))
        return {"emitted": None, "error": str(e)}
    stamped = {"from": _gerp_id(), "from_hub": os.environ.get("HUB_ID", ""), "to": recipient,
               "to_hub": row.get("hub", ""), **detail}
    bus = row.get("hub_bus_arn") or _shared_bus()
    return _send(bus, source, detail_type, stamped)


def publish(source: str, detail_type: str, detail: dict) -> dict:
    """To everyone, on the shared bus, when the firm is openly operated: `if oob: send`. The
    flag is read per invoke (`_openly_operated`); a firm whose flag is off puts nothing and gets
    `{"emitted": None, "withheld": "openly_operated"}` back. Every platform copy of a firm's
    event goes through here."""
    if _openly_operated():
        return put_shared(source, detail_type, detail)
    return {"emitted": None, "withheld": "openly_operated"}


def put_shared(source: str, detail_type: str, detail: dict) -> dict:
    """On the shared bus with the publication envelope, whatever the flag says. For the one
    control message that must leave when the flag is off: the flip itself, `gerp.unpublished`
    (modules/settings). The envelope's `openly_operated` is what the operator's publication rule
    matches, so an emitter that omits it withholds the event from the public archive."""
    return _send(_shared_bus(), source, detail_type, {
        "schema_version": SCHEMA_VERSION,
        "openly_operated": _openly_operated(),
        "customer_id": _gerp_id(),
        **detail,
    })


def _shared_bus() -> str:
    bus = os.environ.get("OP_EVENT_BUS_ARN")
    if not bus:
        raise RuntimeError(
            "OP_EVENT_BUS_ARN is not set — this lambda sends events beyond the firm but has no "
            "shared bus to put them on.")
    return bus
