"""The platform copy of a metrics event (issues #47, #52): `metrics.record` puts the event once on the
firm's own bus, stamped with the partition it counts under and the clock its periods are cut in,
and calls `events.publish` for the platform copy, which sends only when the firm is openly
operated. No rule, no role, no function between the firm's bus and the hub."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _helpers import drained, load_lambda, scratch_env  # noqa: E402
from helpers.localaws import drain, make_bus, make_table  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TF = {m: (REPO / "modules" / m / "infra" / "main.tf").read_text() for m in ("metrics", "labor", "inventory", "invoicing")}
LABOR_VARS = (REPO / "modules" / "labor" / "infra" / "variables.tf").read_text()
PER_CUSTOMER = (REPO / "prod" / "per_customer" / "main.tf").read_text()


def _openly_operated(value: bool):
    """The firm's flag, the settings row `events.publish` reads per invoke."""
    from aws import client
    os.environ["SETTINGS_TABLE"] = make_table("settings")
    gerp = os.environ.get("GERP_ID") or os.environ["CUSTOMER_ID"]
    client("dynamodb").put_item(TableName=os.environ["SETTINGS_TABLE"],
                                Item={"gerp_id": {"S": gerp}, "sk": {"S": "GERP#openly_operated"}, "value": {"BOOL": value}})


def _record(tool):
    return tool.handler({"body": json.dumps({"op": "record", "event": "account.signed_up", "subject_id": "ada",
                                             "at": "2026-09-22T06:30:00.000Z", "properties": {"source": "web"}})}, None)


def test_a_private_firms_record_stays_on_its_own_bus():
    with scratch_env():
        _openly_operated(False)
        shared, q = make_bus("fork-shared")
        os.environ["OP_EVENT_BUS_ARN"] = shared
        tool = load_lambda("manage_metrics")
        assert _record(tool)["statusCode"] == 200
        [ev] = drained(expected=1)
        d = ev["detail"]
        assert ev["detail_type"] == "account.signed_up"
        assert d["customer_id"] == "gradienterp", "the partition the platform counts under"
        assert d["zone"] == "America/Los_Angeles", "the firm's clock, so the platform cuts the same day the owner sees"
        assert d["subject_id"] == "ada" and d["ts"] == "2026-09-22T06:30:00.000Z" and d["via"] == "agent"
        assert d["properties"] == {"source": "web"}
        assert "openly_operated" not in d, "the firm's own bus owes no envelope"
        assert drain(q, expected=0, tries=2) == [], "nothing left the firm"


def test_a_published_firms_record_reaches_the_shared_bus_with_the_envelope():
    with scratch_env():
        _openly_operated(True)
        shared, q = make_bus("fork-shared")
        os.environ["OP_EVENT_BUS_ARN"] = shared
        tool = load_lambda("manage_metrics")
        assert _record(tool)["statusCode"] == 200
        [own] = drained(expected=1)
        [ev] = drain(q, expected=1)
        d = ev["detail"]
        assert ev["detail_type"] == "account.signed_up"
        assert d["openly_operated"] is True and d["schema_version"] == 1 and d["customer_id"] == "gradienterp"
        assert d["subject_id"] == "ada" and d["zone"] == "America/Los_Angeles", "the event as recorded, the envelope beside it"
        assert own["detail"]["subject_id"] == "ada"


def test_the_platform_copy_is_a_function_call_and_every_recorder_may_make_it():
    """Read off the terraform: no rule, target or role for the platform copy in modules/metrics; every
    function that records carries the hub's bus and the settings read `publish` needs."""
    assert "to_operator" not in TF["metrics"] and "operator_bus_arn" not in TF["metrics"]
    assert not (REPO / "modules" / "metrics" / "lambdas" / "forward").exists()
    assert "OP_EVENT_BUS_ARN  = var.op_event_bus_arn" in TF["metrics"] and "SETTINGS_TABLE    = local.settings_table" in TF["metrics"]
    assert TF["metrics"].count("Resource = [var.internal_bus_arn, var.op_event_bus_arn]") == 2, "the door's role and the tool's"
    for m in ("labor", "inventory"):
        assert re.search(r"OP_EVENT_BUS_ARN\s+= var.op_event_bus_arn", TF[m]), m
        assert "Resource = [var.internal_bus_arn, var.op_event_bus_arn]" in TF[m], m
        assert re.search(r"SETTINGS_TABLE\s+=", TF[m]), m
    assert 'variable "op_event_bus_arn"' in LABOR_VARS and 'variable "op_event_bus_arn"' in TF["inventory"]
    assert re.search(r"SETTINGS_TABLE\s+= \"\$\{var.stack_prefix\}-settings-", TF["invoicing"]), "invoicing's shared role reads the flag"
    assert "OP_EVENT_BUS_ARN      = var.op_event_bus_arn" in TF["invoicing"]
    for m in ("metrics", "labor", "inventory"):
        block = re.search(r'module "%s" \{.*?\n\}\n' % m, PER_CUSTOMER, re.S).group(0)
        assert re.search(r"op_event_bus_arn\s+= local.op_event_bus_arn", block), m


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all fork tests passed")
