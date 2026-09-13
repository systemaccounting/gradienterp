"""Local tests for the INSTRUCTION# rows — the firm's standing instructions.

Two writers share one store and one shape: the owner from the gerp screen (tenant_settings'
PUT /settings) and the agent's in-container `instruct` tool. So the tests drive BOTH sides against
the same DDB table and assert they see each other's rows — that seam is the whole feature (the
point of the agent write path is sparing the owner a trip back to the console).

The container half is loaded by slicing entrypoint.py rather than importing it: the module boots a
Strands engine at import time and needs the whole prod environment. Slicing keeps the assertions on
the real source instead of a copy that can drift.
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "out" / "instructions_test"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["CUSTOMER_ID"] = "testgerp"
sys.path.insert(0, str(REPO / "tests"))
from helpers.localaws import make_table   # noqa: E402

os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
# The container half (entrypoint.py) uses raw boto3, not modules/aws/aws.py — it is the agent IMAGE, not a
# lambda, so it does not bundle modules/aws. botocore honours the per-service endpoint variable, so
# pointing that at the same moto makes both writers share one real table.
os.environ["AWS_ENDPOINT_URL_DYNAMODB"] = os.environ.get("LOCAL_AWS_ENDPOINT", "http://localhost:5000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _fresh_table():
    """A clean settings table for one case — a table now, where this used to unlink a JSON file."""
    os.environ["SETTINGS_TABLE"] = make_table("settings")
    return os.environ["SETTINGS_TABLE"]

sys.path.insert(0, str(REPO / "modules" / "settings" / "lambdas"))

ENTRYPOINT = REPO / "modules/agent/docker/entrypoint.py"


def _load_settings():
    """tenant_settings on a fresh store — the owner's HTTP surface."""
    _fresh_table()
    for m in ("_locations", "ts_main"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(
        "ts_main", REPO / "modules/settings/lambdas/tenant_settings/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_container():
    """The container's instruction half, sliced out of entrypoint.py (see module docstring)."""
    src = ENTRYPOINT.read_text()
    blk = src[src.index("INSTRUCTION_SK = "):src.index("def _date_block")]
    blk += src[src.index("def _instruction_block"):src.index("def instruct(")]
    blk += src[src.index("def instruct("):src.index("def _read_owner_secret")]
    # SETTINGS_TABLE non-empty is what makes the container take its DDB path — the same table the
    # lambda writes, which is the seam these tests exist to prove
    ns = {"os": os, "re": re, "json": json, "Path": Path,
          "SETTINGS_TABLE": os.environ["SETTINGS_TABLE"], "CUSTOMER_ID": os.environ["CUSTOMER_ID"]}
    exec(compile(blk, "entrypoint-slice", "exec"), ns)  # noqa: S102 — the point is running real source
    return ns


def _put(ts, body):
    r = ts.handler({"requestContext": {"http": {"method": "PUT"}}, "body": json.dumps(body)}, None)
    return r["statusCode"], json.loads(r["body"])


def test_owner_adds_and_the_list_holds_insertion_order():
    ts = _load_settings()
    _put(ts, {"instruction": "schedule the highest performers on rush shifts"})
    code, body = _put(ts, {"instruction": "quote in gross, not net"})
    assert code == 200, body
    assert [i["text"] for i in body["instructions"]] == [
        "schedule the highest performers on rush shifts", "quote in gross, not net"]


def test_a_pasted_multiline_directive_collapses_to_one_line():
    ts = _load_settings()
    _, body = _put(ts, {"instruction": "  quote in\n  gross,   not net  "})
    assert [i["text"] for i in body["instructions"]] == ["quote in gross, not net"]


def test_duplicate_text_is_a_no_op():
    """The owner types it, then the agent offers to save the same thing — a second row would be
    billed into every turn's prompt forever."""
    ts = _load_settings()
    _put(ts, {"instruction": "quote in gross, not net"})
    _, body = _put(ts, {"instruction": "quote in gross, not net"})
    assert len(body["instructions"]) == 1


def test_empty_and_oversize_are_rejected():
    ts = _load_settings()
    assert _put(ts, {"instruction": "   "})[0] == 400
    assert _put(ts, {"instruction": "x" * 301})[0] == 400
    assert _put(ts, {"remove_instruction": ""})[0] == 400


def test_the_x_removes_by_id():
    ts = _load_settings()
    _put(ts, {"instruction": "one"})
    _, body = _put(ts, {"instruction": "two"})
    code, body = _put(ts, {"remove_instruction": body["instructions"][0]["id"]})
    assert code == 200 and [i["text"] for i in body["instructions"]] == ["two"]


def test_the_cap_holds():
    ts = _load_settings()
    for n in range(ts.INSTRUCTION_MAX):
        assert _put(ts, {"instruction": f"directive {n}"})[0] == 200
    code, body = _put(ts, {"instruction": "one too many"})
    assert code == 400 and "cap" in body["error"]


def test_the_container_renders_what_the_owner_saved():
    ts = _load_settings()
    _put(ts, {"instruction": "schedule the highest performers on rush shifts"})
    _put(ts, {"instruction": "quote in gross, not net"})
    block = _load_container()["_instruction_block"]()
    assert "- schedule the highest performers on rush shifts\n" in block
    assert "- quote in gross, not net\n" in block
    # oldest first — the same order the owner sees in the list
    assert block.index("rush shifts") < block.index("in gross")


def test_no_instructions_means_no_block_at_all():
    """An empty section is prompt weight and an invitation to invent policy — emit nothing."""
    _load_settings()
    assert _load_container()["_instruction_block"]() == ""


def test_the_agent_write_lands_in_the_owners_list():
    ts = _load_settings()
    _put(ts, {"instruction": "quote in gross, not net"})
    ep = _load_container()
    assert "saved" in ep["instruct"]("always confirm before paying an invoice over $500")
    assert [i["text"] for i in ts.list_instructions()] == [
        "quote in gross, not net", "always confirm before paying an invoice over $500"]


def test_the_agent_will_not_duplicate_what_the_owner_already_typed():
    ts = _load_settings()
    _put(ts, {"instruction": "quote in gross, not net"})
    ep = _load_container()
    assert "already" in ep["instruct"]("  quote in gross,   not net ")
    assert len(ts.list_instructions()) == 1


def test_the_agent_gets_a_readable_refusal_rather_than_a_crash():
    """Tool returns are model input: a bad write has to come back as something it can act on."""
    _load_settings()
    ep = _load_container()
    assert "empty" in ep["instruct"]("")
    assert "300" in ep["instruct"]("x" * 301)


def test_timezone_round_trips_and_rejects_a_typo():
    """Validated on write: `ZoneInfo(<bad name>)` raises at CALL time, so an unvalidated typo would
    surface at period close — months after someone fat-fingered it into a settings box."""
    ts = _load_settings()
    assert ts._view("acct-1")["timezone"] == "UTC"            # unset ⇒ UTC ⇒ prior behaviour
    code, body = _put(ts, {"timezone": "America/Los_Angeles"})
    assert code == 200 and body["timezone"] == "America/Los_Angeles"
    for bad in ("Pacific", "PST", "America/Nowhere", ""):
        code, body = _put(ts, {"timezone": bad})
        assert code == 400, f"accepted {bad!r}"
        assert "IANA" in body["error"]
    assert ts._view("acct-1")["timezone"] == "America/Los_Angeles"   # a rejected write changes nothing


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all instructions tests passed")
