"""Local-mode tests for the shared emit path.

Three reaches, three buses' worth of rules about what an event owes. What is worth pinning is that
the reach decides the envelope and the bus — a caller picks by which function it calls — and that
nothing here can raise into a caller that has already done its durable write.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env  # noqa: F401

import events


def _lines(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def test_emit_uses_the_firms_own_bus_and_owes_no_envelope():
    """In-firm consumers are this firm's own modules — there is no archive to qualify for and no
    recipient to prove a sender to."""
    with scratch_env() as env:
        os.environ["INTERNAL_BUS_NAME"] = "gerp-internal-x"
        os.environ["LOCAL_EVENTS"] = str(Path(env) / "e.jsonl")
        events.emit("invoicing", "collection.requested", {"invoice_id": "inv-1"})

        got = _lines(os.environ["LOCAL_EVENTS"])[0]
        assert got["source"] == "invoicing"
        assert got["detail-type"] == "collection.requested"
        assert got["detail"] == {"invoice_id": "inv-1"}


def test_emit_to_stamps_from_and_to():
    """`to` is what the hub's spoke rule routes on; `from` is stamped HERE so a recipient never
    takes a sender's word for who sent it. No directory wired: the sender's own hub, unstamped."""
    with scratch_env() as env:
        os.environ["OP_EVENT_BUS_ARN"] = "arn:aws:events:::event-bus/gerp-events"
        os.environ["GERP_ID"] = "ken-cafe"
        os.environ["LOCAL_EVENTS"] = str(Path(env) / "e.jsonl")
        events.emit_to("purchasing", "valley-dairy", "quote.requested", {"thread": "po-9"})

        d = _lines(os.environ["LOCAL_EVENTS"])[0]["detail"]
        assert d["from"] == "ken-cafe" and d["to"] == "valley-dairy"
        assert d["thread"] == "po-9"
        assert "openly_operated" not in d, "an addressed event is not published"


def _directory_table(name="gerp-directory", rows=()):
    """The platform directory in the local emulator: one row per live gerp."""
    import aws
    ddb = aws.client("dynamodb")
    try:
        ddb.create_table(TableName=name, KeySchema=[{"AttributeName": "gerp_id", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "gerp_id", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    except ddb.exceptions.ResourceInUseException:
        pass
    for r in rows:
        ddb.put_item(TableName=name, Item={k: {"S": v} for k, v in r.items()})
    return f"arn:aws:dynamodb:us-east-1:185369506315:table/{name}"


def test_emit_to_puts_on_the_recipients_hub_and_stamps_the_hubs():
    """The recipient's directory row names its hub and that hub's bus: the put goes THERE, in
    that region, with `to_hub` and `from_hub` beside `to` and `from`. The sender's own hub bus is
    not involved."""
    with scratch_env():
        os.environ["OP_EVENT_BUS_ARN"] = "arn:aws:events:us-east-1:582129522725:event-bus/gerp-events"
        os.environ["GERP_ID"] = "ken-cafe"
        os.environ["HUB_ID"] = "us-east-1"
        os.environ["DIRECTORY_TABLE_ARN"] = _directory_table(rows=[
            {"gerp_id": "valley-dairy", "hub": "eu-west-1", "hub_bus_arn": "arn:aws:events:eu-west-1:111122223333:event-bus/gerp-events",
             "region": "eu-west-1", "aws_account_id": "444455556666"}])
        events._directory.clear()
        sent = []
        real = events._regional

        class _Bus:
            def put_events(self, Entries):
                sent.append(Entries[0])
                return {"FailedEntryCount": 0}

        events._regional = lambda service, region: _Bus() if service == "events" else real(service, region)
        try:
            out = events.emit_to("purchasing", "valley-dairy", "quote.requested", {"thread": "po-9"})
        finally:
            events._regional = real
        assert out == {"emitted": "quote.requested"}
        assert sent[0]["EventBusName"] == "arn:aws:events:eu-west-1:111122223333:event-bus/gerp-events"
        d = json.loads(sent[0]["Detail"])
        assert d == {"from": "ken-cafe", "from_hub": "us-east-1", "to": "valley-dairy", "to_hub": "eu-west-1", "thread": "po-9"}


def test_a_recipient_the_directory_does_not_hold_is_refused_at_the_sender():
    """A gerp that does not exist, or is closed, has no row. `resolve` raises so a caller starting
    a thread can answer before its durable write; `emit_to` after a write never raises — an
    unknown recipient there is one incident line and `emitted: None`, like a bus that is down."""
    with scratch_env():
        os.environ["OP_EVENT_BUS_ARN"] = "arn:aws:events:us-east-1:582129522725:event-bus/gerp-events"
        os.environ["GERP_ID"] = "ken-cafe"
        os.environ["DIRECTORY_TABLE_ARN"] = _directory_table()
        events._directory.clear()
        try:
            events.resolve("nobody-here")
        except events.UnknownRecipient as e:
            assert "nobody-here" in str(e)
        else:
            raise AssertionError("an unknown recipient was not refused")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = events.emit_to("purchasing", "nobody-here", "quote.requested", {})
        assert out["emitted"] is None and "nobody-here" in out["error"]
        line = json.loads(buf.getvalue().strip())
        assert line["incident"] == "fail" and line["subject"] == "recipient:nobody-here" and line["category"] == "events"


def test_the_directory_row_is_read_once_per_recipient():
    with scratch_env():
        os.environ["OP_EVENT_BUS_ARN"] = "arn:aws:events:us-east-1:582129522725:event-bus/gerp-events"
        os.environ["GERP_ID"] = "ken-cafe"
        os.environ["DIRECTORY_TABLE_ARN"] = _directory_table(rows=[
            {"gerp_id": "valley-dairy", "hub": "us-east-1", "hub_bus_arn": "arn:aws:events:us-east-1:582129522725:event-bus/gerp-events"}])
        events._directory.clear()
        reads = []
        real = events._regional

        class _Fake:
            def get_item(self, **kw):
                reads.append(kw["Key"]["gerp_id"]["S"])
                return {"Item": {"gerp_id": {"S": "valley-dairy"}, "hub": {"S": "us-east-1"},
                                 "hub_bus_arn": {"S": "arn:aws:events:us-east-1:582129522725:event-bus/gerp-events"}}}

            def put_events(self, Entries):
                return {}

        events._regional = lambda service, region: _Fake()
        try:
            events.emit_to("purchasing", "valley-dairy", "quote.requested", {})
            events.emit_to("purchasing", "valley-dairy", "quote.accepted", {})
        finally:
            events._regional = real
        assert reads == ["valley-dairy"]


def test_publish_stamps_the_publication_envelope():
    with scratch_env() as env:
        os.environ["OP_EVENT_BUS_ARN"] = "arn:aws:events:::event-bus/gerp-events"
        os.environ["GERP_ID"] = "ken-cafe"
        os.environ["LOCAL_EVENTS"] = str(Path(env) / "e.jsonl")
        events._openly_operated = lambda: True
        events.publish("accounting", "journal_entry.posted", {"entry_id": "je-1"})

        d = _lines(os.environ["LOCAL_EVENTS"])[0]["detail"]
        assert d["openly_operated"] is True, "the publication rule matches on exactly this"
        assert d["schema_version"] == 1 and d["customer_id"] == "ken-cafe"


def test_a_bus_failure_never_raises_and_files_one_incident():
    """The durable write already happened. A failure to ANNOUNCE it must not report it as failed,
    and the line is keyed so a bus failing every emit of a type collapses into one incident."""
    with scratch_env():
        os.environ["INTERNAL_BUS_NAME"] = "gerp-internal-x"
        os.environ.pop("LOCAL_EVENTS", None)
        events.LOCAL_EVENTS = ""

        def _boom(name, **kw):
            class C:
                def put_events(self, **kw):
                    raise RuntimeError("bus is down")
            return C()
        real, events._aws = events._aws, _boom
        events._clients.clear()

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = events.emit("invoicing", "collection.requested", {"invoice_id": "inv-1"})

        assert out["emitted"] is None, "the caller is told, and not by an exception"
        line = json.loads(buf.getvalue().strip())
        assert line["incident"] == "fail"
        assert line["subject"] == "event:invoicing.collection.requested", "the dedupe key"
        assert line["category"] == "events"
        events._aws = real
        events._clients.clear()


def test_a_missing_bus_is_a_configuration_error_not_a_silent_drop():
    """Unset means the lambda was never wired for this reach. Silently succeeding would hide a
    whole module's events for as long as nobody looked."""
    with scratch_env():
        os.environ.pop("INTERNAL_BUS_NAME", None)
        for fn, args in ((events.emit, ("invoicing", "x.y", {})),
                         (events.emit_to, ("invoicing", "them", "x.y", {})),
                         (events.publish, ("invoicing", "x.y", {}))):
            os.environ.pop("OP_EVENT_BUS_ARN", None)
            os.environ.pop("DIRECTORY_TABLE_ARN", None)
            try:
                fn(*args)
            except RuntimeError as e:
                assert "BUS" in str(e).upper()
            else:
                raise AssertionError(f"{fn.__name__} accepted an unwired bus")


def test_decimals_survive_the_encoder():
    """DDB hands back Decimal and json refuses it; a whole quantity must not arrive as 3.0."""
    from decimal import Decimal
    with scratch_env() as env:
        os.environ["INTERNAL_BUS_NAME"] = "gerp-internal-x"
        os.environ["LOCAL_EVENTS"] = str(Path(env) / "e.jsonl")
        events.LOCAL_EVENTS = os.environ["LOCAL_EVENTS"]
        events.emit("inventory", "stock.moved", {"qty": Decimal("3"), "cost": Decimal("2.50")})

        d = _lines(os.environ["LOCAL_EVENTS"])[0]["detail"]
        assert d["qty"] == 3 and isinstance(d["qty"], int)
        assert d["cost"] == 2.5


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all events tests passed")
