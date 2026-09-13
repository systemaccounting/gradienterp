"""End-to-end smoke tests for manage_contacts in local mode.

Covers: put/get round-trip, update merges fields, 404 on missing contact,
query returns role-filtered list, scan returns everything, missing/unknown op refused.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_put_get_roundtrip():
    with scratch_env():
        mc = load_lambda("manage_contacts")

        code, body = _invoke(mc, {
            "op": "put",
            "contact_id": "blue_bottle",
            "entity_type": "organization",
            "is_vendor": True,
            "name": "Blue Bottle Coffee",
        })
        assert code == 200, body
        assert body["contact"]["contact_id"] == "blue_bottle"
        assert "op" not in body["contact"], "op is stripped before the row is written"
        assert body["contact"]["created_at"] > 0
        assert body["contact"]["updated_at"] >= body["contact"]["created_at"]

        code, body = _invoke(mc, {"op": "get", "contact_id": "blue_bottle"})
        assert code == 200, body
        assert body["contact"]["name"] == "Blue Bottle Coffee"


def test_get_404_when_missing():
    with scratch_env():
        mc = load_lambda("manage_contacts")
        code, body = _invoke(mc, {"op": "get", "contact_id": "nope"})
        assert code == 404
        assert "not found" in body["error"]


def test_update_merges_fields():
    with scratch_env():
        mc = load_lambda("manage_contacts")

        _invoke(mc, {
            "op": "put",
            "contact_id": "blue_bottle",
            "entity_type": "organization",
            "is_vendor": True,
            "name": "Blue Bottle Coffee",
        })

        code, body = _invoke(mc, {
            "op": "update",
            "contact_id": "blue_bottle",
            "updates": {"terms": "net_30", "is_customer": True},
        })
        assert code == 200, body

        code, body = _invoke(mc, {"op": "get", "contact_id": "blue_bottle"})
        assert code == 200
        c = body["contact"]
        assert c["name"] == "Blue Bottle Coffee"   # preserved
        assert c["terms"] == "net_30"              # added
        assert c["is_customer"] is True            # added
        assert c["is_vendor"] is True              # preserved


def test_update_404_when_missing():
    with scratch_env():
        mc = load_lambda("manage_contacts")
        code, body = _invoke(mc, {
            "op": "update",
            "contact_id": "nope",
            "updates": {"terms": "net_30"},
        })
        assert code == 404


def test_query_by_role():
    with scratch_env():
        mc = load_lambda("manage_contacts")

        _invoke(mc, {"op": "put", "contact_id": "v1", "entity_type": "organization", "is_vendor": True, "name": "Vendor One"})
        _invoke(mc, {"op": "put", "contact_id": "c1", "entity_type": "organization", "is_customer": True, "name": "Customer One"})
        _invoke(mc, {"op": "put", "contact_id": "e1", "entity_type": "person", "is_employee": True, "first_name": "Ken", "last_name": "Cook"})

        code, body = _invoke(mc, {"op": "query", "role": "vendor"})
        assert code == 200, body
        ids = [c["contact_id"] for c in body["contacts"]]
        assert ids == ["v1"]

        code, body = _invoke(mc, {"op": "query", "role": "employee"})
        assert code == 200
        ids = [c["contact_id"] for c in body["contacts"]]
        assert ids == ["e1"]


def test_scan_returns_all():
    with scratch_env():
        mc = load_lambda("manage_contacts")

        _invoke(mc, {"op": "put", "contact_id": "a", "entity_type": "organization", "name": "A"})
        _invoke(mc, {"op": "put", "contact_id": "b", "entity_type": "organization", "name": "B"})
        _invoke(mc, {"op": "put", "contact_id": "c", "entity_type": "organization", "name": "C"})

        code, body = _invoke(mc, {"op": "scan"})
        assert code == 200, body
        ids = sorted(c["contact_id"] for c in body["contacts"])
        assert ids == ["a", "b", "c"]


def test_missing_or_unknown_op_is_refused():
    with scratch_env():
        mc = load_lambda("manage_contacts")
        code, body = _invoke(mc, {"contact_id": "blue_bottle"})
        assert code == 400
        assert "op is required" in body["error"] and "put" in body["error"], "the refusal names the ops"
        code, body = _invoke(mc, {"op": "delete", "contact_id": "blue_bottle"})
        assert code == 400


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")
