"""Local-mode tests for manage_labor, the op-routed labor CRUD tool.

get / put / update / query are thin, schemaless ops over THREE tables. Unlike
tasks (single table) each call is parameterized by an `entity` ∈ {worker,
time_entry, worker_legal} that selects the table + its key schema:

    worker        keys (contact_id, role)
    time_entry    keys (worker_id, entry_id)
    worker_legal  keys (worker_id, sk)

Covers: put/get round-trip per entity, the op and entity args (required +
rejected when unknown), missing-key errors, time_entry entry_id auto-generation, update merges
+ key/created_at immutability + 404, query-by-partition per entity, and the
clock-out path (status→closed) the close-handler later reads. worker put lands
in the worker table, the same store the close-handler resolves rates from.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _wire_entity_stores(out_dir):
    """The shared harness wires WORKER_TABLE (so worker tool writes feed the
    close-handler); point the other two entity stores into the same scratch
    dir so every entity is isolated per test."""
    os.environ["LOCAL_TIME_ENTRIES"] = str(out_dir / "time-entries.jsonl")
    os.environ["LOCAL_WORKER_LEGAL"] = str(out_dir / "worker-legal.jsonl")


# ─── entity arg ───

def test_entity_required():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        get = load_lambda("manage_labor")
        code, body = _invoke(get, {"op": "get", "contact_id": "alice", "role": "barista"})
        assert code == 400
        assert "entity" in body["error"]


def test_unknown_entity_rejected():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        get = load_lambda("manage_labor")
        code, body = _invoke(get, {"op": "get", "entity": "payroll", "contact_id": "x", "role": "y"})
        assert code == 400
        assert "unknown entity" in body["error"]


# ─── worker (keys contact_id, role) ───

def test_worker_put_get_roundtrip():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        get = load_lambda("manage_labor")

        code, body = _invoke(put, {"op": "put", 
            "entity": "worker",
            "contact_id": "alice",
            "role": "barista",
            "attributes": {"rate": 20, "classification": "W-2"},
        })
        assert code == 200, body
        assert body["entity"] == "worker"
        item = body["item"]
        assert item["contact_id"] == "alice"
        assert item["role"] == "barista"
        assert item["rate"] == 20
        assert item["classification"] == "W-2"
        assert item["created_at"] > 0
        assert item["updated_at"] == item["created_at"]

        code, body = _invoke(get, {"op": "get", "entity": "worker", "contact_id": "alice", "role": "barista"})
        assert code == 200, body
        assert body["item"]["rate"] == 20


def test_worker_put_lands_in_local_workers_store():
    """The worker tool must write the same file the close-handler reads."""
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        _invoke(put, {"op": "put", 
            "entity": "worker", "contact_id": "alice", "role": "barista",
            "attributes": {"rate": 20, "classification": "W-2"},
        })
        from helpers.localaws import rows as table_rows
        rows = table_rows(os.environ["WORKER_TABLE"])
        assert any(r["contact_id"] == "alice" and r["role"] == "barista" and r["rate"] == 20
                   for r in rows)


def test_worker_missing_range_key():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        code, body = _invoke(put, {"op": "put", "entity": "worker", "contact_id": "alice",
                                   "attributes": {"rate": 20}})
        assert code == 400
        assert "role" in body["error"]


def test_worker_get_404():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        get = load_lambda("manage_labor")
        code, body = _invoke(get, {"op": "get", "entity": "worker", "contact_id": "nobody", "role": "ghost"})
        assert code == 404


def test_worker_multi_role_query():
    """one person, two priced roles → two worker rows under one contact_id."""
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        query = load_lambda("manage_labor")

        _invoke(put, {"op": "put", "entity": "worker", "contact_id": "dana", "role": "lawyer",
                      "attributes": {"rate": 300, "classification": "W-2"}})
        _invoke(put, {"op": "put", "entity": "worker", "contact_id": "dana", "role": "bookkeeper",
                      "attributes": {"rate": 40, "classification": "W-2"}})
        _invoke(put, {"op": "put", "entity": "worker", "contact_id": "alice", "role": "barista",
                      "attributes": {"rate": 20, "classification": "W-2"}})

        code, body = _invoke(query, {"op": "query", "entity": "worker", "contact_id": "dana"})
        assert code == 200, body
        roles = sorted(r["role"] for r in body["items"])
        assert roles == ["bookkeeper", "lawyer"]


# ─── time_entry (keys worker_id, entry_id) ───

def test_time_entry_autogenerates_entry_id():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        code, body = _invoke(put, {"op": "put", 
            "entity": "time_entry", "worker_id": "alice",
            "attributes": {"role": "barista", "started_at": 0, "status": "open"},
        })
        assert code == 200, body
        assert body["item"]["entry_id"]  # non-empty uuid
        assert body["item"]["worker_id"] == "alice"
        assert body["item"]["status"] == "open"


def test_time_entry_clockout_via_update():
    """Clock-out = update status→closed + ended_at. (The stream→close-handler
    accrual is exercised in test_close_handler; here we just confirm the tool
    writes the closed row.)"""
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        update = load_lambda("manage_labor")
        get = load_lambda("manage_labor")

        code, body = _invoke(put, {"op": "put", 
            "entity": "time_entry", "worker_id": "alice", "entry_id": "te_1",
            "attributes": {"role": "barista", "started_at": 0, "status": "open"},
        })
        assert code == 200, body

        code, body = _invoke(update, {"op": "update", 
            "entity": "time_entry", "worker_id": "alice", "entry_id": "te_1",
            "updates": {"status": "closed", "ended_at": 8 * 3_600_000},
        })
        assert code == 200, body
        assert body["item"]["status"] == "closed"
        assert body["item"]["ended_at"] == 8 * 3_600_000

        code, body = _invoke(get, {"op": "get", "entity": "time_entry", "worker_id": "alice", "entry_id": "te_1"})
        assert body["item"]["status"] == "closed"


def test_time_entry_query_by_worker():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        query = load_lambda("manage_labor")

        _invoke(put, {"op": "put", "entity": "time_entry", "worker_id": "alice", "entry_id": "te_a",
                      "attributes": {"role": "barista", "started_at": 0, "status": "open"}})
        _invoke(put, {"op": "put", "entity": "time_entry", "worker_id": "alice", "entry_id": "te_b",
                      "attributes": {"role": "barista", "started_at": 10, "status": "open"}})
        _invoke(put, {"op": "put", "entity": "time_entry", "worker_id": "bob", "entry_id": "te_c",
                      "attributes": {"role": "cook", "started_at": 0, "status": "open"}})

        code, body = _invoke(query, {"op": "query", "entity": "time_entry", "worker_id": "alice"})
        assert code == 200, body
        ids = sorted(r["entry_id"] for r in body["items"])
        assert ids == ["te_a", "te_b"]

        # a projection: the keys always, then only what was asked
        code, body = _invoke(query, {"op": "query", "entity": "time_entry", "worker_id": "alice",
                                     "fields": ["status"]})
        assert code == 200, body
        for row in body["items"]:
            assert set(row) == {"worker_id", "entry_id", "status"}, row


# ─── worker_legal (keys worker_id, sk) ───

def test_worker_legal_put_get_roundtrip():
    """First cut: the masked JSON is stored as-is (no masking/secure-store)."""
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        get = load_lambda("manage_labor")

        masked = {"ssn": "XXX-XX-1234", "dependents": 2}
        code, body = _invoke(put, {"op": "put", 
            "entity": "worker_legal", "worker_id": "alice", "sk": "barista#W-4",
            "attributes": {"type": "W-4", "value": masked},
        })
        assert code == 200, body
        assert body["item"]["type"] == "W-4"
        assert body["item"]["value"] == masked

        code, body = _invoke(get, {"op": "get", "entity": "worker_legal", "worker_id": "alice", "sk": "barista#W-4"})
        assert body["item"]["value"]["ssn"] == "XXX-XX-1234"


def test_worker_legal_query_by_worker():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        query = load_lambda("manage_labor")

        _invoke(put, {"op": "put", "entity": "worker_legal", "worker_id": "alice", "sk": "barista#W-4",
                      "attributes": {"type": "W-4", "value": {"dependents": 1}}})
        _invoke(put, {"op": "put", "entity": "worker_legal", "worker_id": "alice", "sk": "barista#W-2",
                      "attributes": {"type": "W-2", "value": {"box1": 1000}}})

        code, body = _invoke(query, {"op": "query", "entity": "worker_legal", "worker_id": "alice"})
        assert code == 200, body
        sks = sorted(r["sk"] for r in body["items"])
        assert sks == ["barista#W-2", "barista#W-4"]


# ─── update mechanics (shared across entities) ───

def test_update_merges_and_advances_updated_at():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        update = load_lambda("manage_labor")
        import time

        code, body = _invoke(put, {"op": "put", "entity": "worker", "contact_id": "alice", "role": "barista",
                                   "attributes": {"rate": 20, "classification": "W-2"}})
        t0 = body["item"]["updated_at"]
        time.sleep(0.005)

        code, body = _invoke(update, {"op": "update", "entity": "worker", "contact_id": "alice", "role": "barista",
                                      "updates": {"rate": 22}})
        assert code == 200, body
        assert body["item"]["rate"] == 22
        assert body["item"]["classification"] == "W-2"  # preserved
        assert body["item"]["updated_at"] > t0
        assert body["item"]["created_at"] == t0  # created_at preserved


def test_update_cannot_change_keys_or_created_at():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        put = load_lambda("manage_labor")
        update = load_lambda("manage_labor")

        code, body = _invoke(put, {"op": "put", "entity": "worker", "contact_id": "alice", "role": "barista",
                                   "attributes": {"rate": 20, "classification": "W-2"}})
        created = body["item"]["created_at"]

        code, body = _invoke(update, {"op": "update", 
            "entity": "worker", "contact_id": "alice", "role": "barista",
            "updates": {"contact_id": "mallory", "role": "ceo", "created_at": 1, "rate": 25},
        })
        assert code == 200, body
        assert body["item"]["contact_id"] == "alice"  # key immutable
        assert body["item"]["role"] == "barista"       # key immutable
        assert body["item"]["created_at"] == created   # immutable
        assert body["item"]["rate"] == 25              # the real change applied


def test_update_404():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        update = load_lambda("manage_labor")
        code, body = _invoke(update, {"op": "update", "entity": "worker", "contact_id": "nobody", "role": "ghost",
                                      "updates": {"rate": 1}})
        assert code == 404


def test_update_requires_updates():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        update = load_lambda("manage_labor")
        code, body = _invoke(update, {"op": "update", "entity": "worker", "contact_id": "alice", "role": "barista",
                                      "updates": {}})
        assert code == 400


def test_query_requires_hash_key():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        query = load_lambda("manage_labor")
        code, body = _invoke(query, {"op": "query", "entity": "worker"})
        assert code == 400
        assert "contact_id" in body["error"]




# ─── op arg ───

def test_op_required_and_unknown_rejected():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        tool = load_lambda("manage_labor")
        code, body = _invoke(tool, {"entity": "worker", "contact_id": "alice", "role": "barista"})
        assert code == 400
        assert "op is required" in body["error"] and "delete" in body["error"]
        code, body = _invoke(tool, {"op": "upsert", "entity": "worker",
                                    "contact_id": "alice", "role": "barista"})
        assert code == 400


def test_delete_removes_the_row():
    with scratch_env() as (out_dir, _):
        _wire_entity_stores(out_dir)
        tool = load_lambda("manage_labor")
        _invoke(tool, {"op": "put", "entity": "worker", "contact_id": "alice", "role": "barista",
                       "attributes": {"rate": 20, "classification": "W-2"}})
        code, body = _invoke(tool, {"op": "delete", "entity": "worker",
                                    "contact_id": "alice", "role": "barista"})
        assert code == 200, body
        assert body["deleted_files"] == []
        code, body = _invoke(tool, {"op": "get", "entity": "worker",
                                    "contact_id": "alice", "role": "barista"})
        assert code == 404


if __name__ == "__main__":
    for fn_name in [n for n in dir() if n.startswith("test_")]:
        globals()[fn_name]()
    print("ok")
