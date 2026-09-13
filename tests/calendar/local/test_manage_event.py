"""Local tests for manage_event — the calendar events table + its calendar-index GSI.

Run: `bash scripts/test.sh --module calendar`."""
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table, seed_registry   # noqa: E402

_LAMBDAS = REPO_ROOT / "modules/calendar/lambdas"
for p in (str(_LAMBDAS), str(_LAMBDAS / "manage_event")):
    if p not in sys.path:
        sys.path.insert(0, p)
import main  # noqa: E402  (manage_event/main.py)


def _use(_d=None):
    """A fresh events table + registry for one case — tables now, where this used to point
    LOCAL_EVENTS at a temp file."""
    schema = make_table("schema")
    seed_registry(schema, "calendar_fields")
    os.environ["SCHEMA_TABLE"] = schema
    os.environ["EVENTS_TABLE"] = make_table("calendar-events")


def _b(r):
    return json.loads(r["body"])


def test_put_get_delete():
    _use()
    r = _b(main.handler({"op": "put", "subject": "c-ava-reyes", "expr": "2026-08-15", "description": "AcmeConf, Vegas", "event_id": "e1"}))
    assert r["event_id"] == "e1"
    got = _b(main.handler({"op": "get", "event_id": "e1"}))["item"]
    assert got["description"] == "AcmeConf, Vegas" and got["starts_at"] == "2026-08-15"  # starts_at defaulted from a one-off expr
    main.handler({"op": "delete", "event_id": "e1"})
    assert main.handler({"op": "get", "event_id": "e1"})["statusCode"] == 404


def test_query_by_date_window():
    _use()
    for sid, day in [("a", "2026-08-05"), ("b", "2026-08-20"), ("c", "2026-09-02")]:
        main.handler({"op": "put", "subject": "c-ava-reyes", "expr": day, "description": sid, "event_id": sid})
    main.handler({"op": "put", "subject": "c-ben-osei", "expr": "2026-08-10", "description": "x", "event_id": "z"})
    r = _b(main.handler({"op": "query", "subject": "c-ava-reyes", "start": "2026-08-01", "end": "2026-08-31"}))
    assert [i["event_id"] for i in r["items"]] == ["a", "b"]  # August only, this subject, sorted by starts_at
    r2 = _b(main.handler({"op": "query", "subject": "c-ava-reyes"}))
    assert r2["count"] == 3  # no window → all of the subject's events


def test_errors():
    _use()
    assert main.handler({"op": "frob"})["statusCode"] == 400                                              # bad op
    assert main.handler({"op": "put", "expr": "2026-08-15", "description": "x"})["statusCode"] == 400      # no subject
    assert main.handler({"op": "put", "subject": "c-ava-reyes", "expr": "FREQ=WEEKLY", "description": "standup"})["statusCode"] == 400  # recurring needs explicit starts_at
    assert main.handler({"op": "query"})["statusCode"] == 400                                             # query needs subject
    # (unknown-field rejection is registry-validated → prod-only; covered by the live smoke test)


def test_a_published_event_carries_no_person_and_no_prose():
    """`subject` is a CONTACT_ID reference, so a reader groups a calendar by person without ever
    publishing who — and names them only if that contact points at a public profile. The
    free-form `description` takes the closed default until it gains a template."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "schemas"))
    import oob
    oob._CACHE.clear()
    os.environ["LOCAL_CANONICAL_DIR"] = str(
        Path(__file__).resolve().parents[3] / "modules" / "schemas" / "data")

    row = {"event_id": "e1", "subject": "c-ava-reyes", "expr": "2026-08-15",
           "starts_at": "2026-08-15", "description": "lunch w/ Dana", "duration": 60,
           "contact_id": "c-dana", "created_at": 1}
    out = oob.project(row, "calendar_fields")
    assert out["fields"] == {"event_id": "e1", "expr": "2026-08-15",
                             "starts_at": "2026-08-15", "duration": 60, "created_at": 1}
    assert out["subjects"] == {"subject": "c-ava-reyes", "contact_id": "c-dana"}
    assert "Dana" not in str(out["fields"])


if __name__ == "__main__":
    test_put_get_delete()
    test_query_by_date_window()
    test_errors()
    test_a_published_event_carries_no_person_and_no_prose()
    print("ok")
