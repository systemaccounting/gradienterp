"""Smoke tests for manage_notes (op: get|put|update|query|scan) in local mode.

Covers: put/get round-trip, update appends a new version (old version preserved),
query by each FK type returns latest-per-note, scan returns all latest, created_at
preserved across versions.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env


def _invoke(lam, body):
    # AgentCore Gateway tool invocation passes input as the event dict directly.
    # Lambdas handle both shapes via the standard `event["body"]`-or-`event` pattern.
    resp = lam.handler(body, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_put_get_roundtrip():
    with scratch_env():
        put = load_lambda("manage_notes")
        get = load_lambda("manage_notes")

        code, body = _invoke(put, {"op": "put", 
            "note_id": "n1",
            "content": "ken always orders the dark roast",
            "contact_id": "ken",
        })
        assert code == 200, body
        assert body["note"]["note_id"] == "n1"
        assert body["note"]["version_ts"]
        assert body["note"]["created_at"] > 0
        v1_ts = body["note"]["version_ts"]
        v1_created = body["note"]["created_at"]

        code, body = _invoke(get, {"op": "get", "note_id": "n1"})
        assert code == 200, body
        assert body["note"]["content"] == "ken always orders the dark roast"
        assert body["note"]["version_ts"] == v1_ts
        assert body["note"]["created_at"] == v1_created


def test_get_404_when_missing():
    with scratch_env():
        get = load_lambda("manage_notes")
        code, body = _invoke(get, {"op": "get", "note_id": "nope"})
        assert code == 404
        assert "not found" in body["error"]


def test_update_appends_version():
    with scratch_env():
        put = load_lambda("manage_notes")
        update = load_lambda("manage_notes")
        get = load_lambda("manage_notes")

        _invoke(put, {"op": "put", "note_id": "n1", "content": "v1 text", "contact_id": "ken"})
        # ensure version_ts strictly increases — sleep 2ms past the 4-digit suffix space
        time.sleep(0.002)

        code, body = _invoke(update, {"op": "update", 
            "note_id": "n1",
            "updates": {"content": "v2 text — updated"},
        })
        assert code == 200, body
        assert body["note"]["content"] == "v2 text — updated"
        assert body["note"]["contact_id"] == "ken"  # carried forward

        code, body = _invoke(get, {"op": "get", "note_id": "n1"})
        assert code == 200
        assert body["note"]["content"] == "v2 text — updated"


def test_update_404_when_missing():
    with scratch_env():
        update = load_lambda("manage_notes")
        code, body = _invoke(update, {"op": "update", "note_id": "nope", "updates": {"content": "x"}})
        assert code == 404


def test_created_at_preserved_across_versions():
    with scratch_env():
        put = load_lambda("manage_notes")
        update = load_lambda("manage_notes")
        get = load_lambda("manage_notes")

        code, body = _invoke(put, {"op": "put", "note_id": "n1", "content": "v1"})
        original_created = body["note"]["created_at"]

        time.sleep(0.002)
        _invoke(update, {"op": "update", "note_id": "n1", "updates": {"content": "v2"}})
        time.sleep(0.002)
        _invoke(update, {"op": "update", "note_id": "n1", "updates": {"content": "v3"}})

        code, body = _invoke(get, {"op": "get", "note_id": "n1"})
        assert code == 200
        assert body["note"]["created_at"] == original_created
        assert body["note"]["content"] == "v3"


def test_query_by_contact_id():
    with scratch_env():
        put = load_lambda("manage_notes")
        query = load_lambda("manage_notes")

        _invoke(put, {"op": "put", "note_id": "n1", "content": "first", "contact_id": "ken"})
        time.sleep(0.002)
        _invoke(put, {"op": "put", "note_id": "n2", "content": "second", "contact_id": "ken"})
        time.sleep(0.002)
        _invoke(put, {"op": "put", "note_id": "n3", "content": "other", "contact_id": "alice"})

        code, body = _invoke(query, {"op": "query", "contact_id": "ken"})
        assert code == 200, body
        ids = sorted(n["note_id"] for n in body["notes"])
        assert ids == ["n1", "n2"]


def test_query_returns_latest_only():
    with scratch_env():
        put = load_lambda("manage_notes")
        update = load_lambda("manage_notes")
        query = load_lambda("manage_notes")

        _invoke(put, {"op": "put", "note_id": "n1", "content": "v1", "contact_id": "ken"})
        time.sleep(0.002)
        _invoke(update, {"op": "update", "note_id": "n1", "updates": {"content": "v2"}})
        time.sleep(0.002)
        _invoke(update, {"op": "update", "note_id": "n1", "updates": {"content": "v3"}})

        code, body = _invoke(query, {"op": "query", "contact_id": "ken"})
        assert code == 200, body
        assert len(body["notes"]) == 1
        assert body["notes"][0]["content"] == "v3"


def test_query_by_journal_entry_id():
    with scratch_env():
        put = load_lambda("manage_notes")
        query = load_lambda("manage_notes")

        _invoke(put, {"op": "put", "note_id": "n1", "content": "JE annotation", "journal_entry_id": "je-001"})
        _invoke(put, {"op": "put", "note_id": "n2", "content": "different JE", "journal_entry_id": "je-002"})

        code, body = _invoke(query, {"op": "query", "journal_entry_id": "je-001"})
        assert code == 200, body
        ids = [n["note_id"] for n in body["notes"]]
        assert ids == ["n1"]


def test_query_requires_one_fk():
    with scratch_env():
        query = load_lambda("manage_notes")
        code, body = _invoke(query, {"op": "query", })
        assert code == 400
        assert "is required" in body["error"]


def test_scan_returns_latest_per_note():
    with scratch_env():
        put = load_lambda("manage_notes")
        update = load_lambda("manage_notes")
        scan = load_lambda("manage_notes")

        _invoke(put, {"op": "put", "note_id": "n1", "content": "v1", "contact_id": "ken"})
        time.sleep(0.002)
        _invoke(update, {"op": "update", "note_id": "n1", "updates": {"content": "v2"}})
        _invoke(put, {"op": "put", "note_id": "n2", "content": "single", "contact_id": "alice"})

        code, body = _invoke(scan, {"op": "scan", })
        assert code == 200, body
        rows_by_id = {n["note_id"]: n for n in body["notes"]}
        assert rows_by_id["n1"]["content"] == "v2"
        assert rows_by_id["n2"]["content"] == "single"


def test_content_is_a_template_and_the_values_stay_out_of_it():
    """A note is where someone writes "call Dana about the late invoice". Classing the whole field
    secret would publish nothing; split, the shape of the annotation publishes and the name does not."""
    with scratch_env():
        put, get = load_lambda("manage_notes"), load_lambda("manage_notes")
        code, body = _invoke(put, {"op": "put", 
            "note_id": "n-tpl",
            "content": "called $1 about the late invoice, they will pay $2",
            "private_values": ["Dana Reyes", "friday"],
            "invoice_id": "inv_9",
        })
        assert code == 200, body
        code, body = _invoke(get, {"op": "get", "note_id": "n-tpl"})
        assert code == 200, body
        note = body["note"]
        assert "Dana" not in note["content"], "a private value must never reach the template"
        assert note["private_values"] == ["Dana Reyes", "friday"]
        assert note["invoice_id"] == "inv_9", "what the note is ABOUT still publishes"


def test_placeholders_and_values_must_correspond_on_put_and_on_update():
    with scratch_env():
        put, upd = load_lambda("manage_notes"), load_lambda("manage_notes")
        code, _ = _invoke(put, {"op": "put", "content": "broke on $1 and $2", "private_values": ["only one"]})
        assert code == 400
        code, _ = _invoke(put, {"op": "put", "content": "no placeholders", "private_values": ["but a value"]})
        assert code == 400

        code, body = _invoke(put, {"op": "put", "note_id": "n-drift", "content": "called $1",
                                   "private_values": ["Dana Reyes"]})
        assert code == 200, body
        # re-wording the text without re-stating the values is how the two halves drift apart
        code, _ = _invoke(upd, {"op": "update", "note_id": "n-drift", "updates": {"content": "called $1 and $2"}})
        assert code == 400, "a version whose $2 has nothing behind it must not be written"


def test_expand_puts_the_values_back_for_whoever_holds_both_halves():
    import template
    assert template.expand("called $1 about $2", ["Dana", "the invoice"]) == "called Dana about the invoice"
    # a reader holding only the public half still gets readable text
    assert template.expand("called $1 about $2", []) == "called $1 about $2"


def test_op_is_required_and_unknown_op_is_refused():
    with scratch_env():
        mn = load_lambda("manage_notes")
        code, body = _invoke(mn, {"note_id": "n1"})
        assert code == 400 and "op is required" in body["error"]
        assert "get | put | query | scan | update" in body["error"], "the refusal names the ops"
        code, body = _invoke(mn, {"op": "delete", "note_id": "n1"})
        assert code == 400


def test_op_does_not_leak_into_the_payload():
    """The router strips op before delegating — a put must not try to write an 'op' field
    (the registry would refuse it as an unknown field)."""
    with scratch_env():
        mn = load_lambda("manage_notes")
        code, body = _invoke(mn, {"op": "put", "note_id": "n1", "content": "clean"})
        assert code == 200, body
        assert "op" not in body["note"]


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")
