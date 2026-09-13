"""Smoke tests for manage_tasks (one tool, op picks the verb) in local mode — the (task_id, sk) reshape.

Covers: put/get round-trip (HEADER item), update -> changelog rows, the delivery ratchet
(once-only + open_flag drop + open-children guard), parents + cycle refusal, query by
FK / open / parent, scan returns headers only, retired-field rejection.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env


def _invoke(mod, op, body):
    resp = mod.handler({"body": json.dumps({"op": op, **body})}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_put_get_roundtrip_header():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        code, body = _invoke(mt, "put", {
            "task_id": "t1",
            "content": "call ken about oat milk",
            "contact_id": "ken",
            "due_date": "2026-05-22",
            "quote": 1785600000000,
        })
        assert code == 200, body
        t = body["task"]
        assert t["task_id"] == "t1" and t["sk"] == "HEADER"
        assert t["open_flag"] == "1"
        assert t["quote"] == 1785600000000
        assert "delivery" not in t and "resolved_at" not in t

        code, body = _invoke(mt, "get", {"task_id": "t1"})
        assert code == 200, body
        assert body["task"]["content"] == "call ken about oat milk"


def test_update_appends_changelog_and_requote():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "t2", "content": "descale machine", "quote": 100})
        code, body = _invoke(mt, "update", {"task_id": "t2", "updates": {"quote": 200, "category": "maintenance"}})
        assert code == 200, body
        assert body["task"]["quote"] == 200

        code, body = _invoke(mt, "get", {"task_id": "t2", "history": True})
        assert code == 200, body
        hist = body["history"]
        assert len(hist) == 1
        changes = hist[0]["changes"]
        assert changes["quote"] == {"from": 100, "to": 200}
        assert changes["category"]["to"] == "maintenance"


def test_delivery_ratchet_once_only():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "t3", "content": "one and done"})
        code, body = _invoke(mt, "update", {"task_id": "t3", "deliver": "now"})
        assert code == 200, body
        assert body["task"]["delivery"] > 0
        assert "open_flag" not in body["task"]

        code, body = _invoke(mt, "update", {"task_id": "t3", "deliver": "now"})
        assert code == 409, body

        code, body = _invoke(mt, "get", {"task_id": "t3", "history": True})
        assert any("delivery" in h["changes"] for h in body["history"])


def test_open_child_blocks_parent_delivery():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "parent1", "content": "ship the feature"})
        _invoke(mt, "put", {"task_id": "kid1", "content": "write the tests", "parents": ["parent1"]})

        code, body = _invoke(mt, "update", {"task_id": "parent1", "deliver": "now"})
        assert code == 409 and "kid1" in body["error"], body

        code, _ = _invoke(mt, "update", {"task_id": "kid1", "deliver": "now"})
        assert code == 200
        code, body = _invoke(mt, "update", {"task_id": "parent1", "deliver": "now"})
        assert code == 200, body


def test_parent_cycle_refused():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "a", "content": "a"})
        _invoke(mt, "put", {"task_id": "b", "content": "b", "parents": ["a"]})
        code, body = _invoke(mt, "update", {"task_id": "a", "updates": {"parents": ["b"]}})
        assert code == 400 and "cycle" in body["error"], body

        code, body = _invoke(mt, "put", {"task_id": "c", "content": "c", "parents": ["c"]})
        assert code == 400 and "cycle" in body["error"], body


def test_query_modes():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "q1", "content": "open one", "contact_id": "ken", "due_date": "2026-08-02"})
        _invoke(mt, "put", {"task_id": "q2", "content": "open two", "due_date": "2026-08-01"})
        _invoke(mt, "put", {"task_id": "q3", "content": "kid of q1", "parents": ["q1"]})
        _invoke(mt, "update", {"task_id": "q2", "deliver": "now"})

        code, body = _invoke(mt, "query", {"open": True})
        ids = [t["task_id"] for t in body["tasks"]]
        assert "q1" in ids and "q3" in ids and "q2" not in ids

        code, body = _invoke(mt, "query", {"parent": "q1"})
        assert [t["task_id"] for t in body["tasks"]] == ["q3"]

        code, body = _invoke(mt, "query", {"contact_id": "ken"})
        assert [t["task_id"] for t in body["tasks"]] == ["q1"]

        code, body = _invoke(mt, "query", {"open": True, "contact_id": "ken"})
        assert code == 400


def test_scan_headers_only():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "s1", "content": "s1"})
        _invoke(mt, "update", {"task_id": "s1", "updates": {"category": "pos"}})   # -> a changelog row exists
        code, body = _invoke(mt, "scan", {})
        assert code == 200
        assert [t["sk"] for t in body["tasks"]] == ["HEADER"]


def test_retired_fields_rejected():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")

        _invoke(mt, "put", {"task_id": "r1", "content": "r1"})
        code, body = _invoke(mt, "update", {"task_id": "r1", "updates": {"severity": "down"}})
        assert code == 400 and "retired" in body["error"], body
        code, body = _invoke(mt, "update", {"task_id": "r1", "updates": {"resolved_at": "now"}})
        assert code == 400 and "retired" in body["error"], body


def test_update_requires_something():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "u1", "content": "u1"})
        code, body = _invoke(mt, "update", {"task_id": "u1"})
        assert code == 400
        code, body = _invoke(mt, "update", {"task_id": "missing", "updates": {"content": "x"}})
        assert code == 404


def test_put_persists_the_private_values_of_a_templated_task():
    """the put copies a WHITELIST of fields, so a new one is silently dropped until it is
    added — which is exactly what happened to `private_values` and only surfaced in a live smoke,
    because the collector test asserted on the payload SENT rather than on what persisted."""
    with scratch_env():
        mt = load_lambda("manage_tasks")
        mt = load_lambda("manage_tasks")
        code, body = _invoke(mt, "put", {
            "content": "[bug][tanners] reserve 409ed booking $1",
            "category": "escalation",
            "private_values": ["the Henderson wedding"],
        })
        assert code == 200, body
        task_id = body["task"]["task_id"]

        code, body = _invoke(mt, "get", {"task_id": task_id})
        assert code == 200, body
        assert body["task"]["private_values"] == ["the Henderson wedding"]
        assert "Henderson" not in body["task"]["content"]



# ─── tags ───
#
# Two things a task could not say before: what a firm calls it, and why it ended up closed. The
# open/closed ratchet stays binary; tags carry the vocabulary and the changelog carries the journey.

def _declare(tag):
    """A tag has to be declared before it can be applied — the registry is where a vocabulary
    accumulates, so an undeclared one is refused rather than invented per task."""
    import os
    from aws import table
    table(os.environ["SCHEMA_TABLE"]).put_item(
        Item={"registry": "task_tags", "bucket_name": f"common#{tag}", "description": tag})


def test_a_declared_tag_can_be_added_and_comes_back():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "check the charge"})
        _declare("approved")

        code, body = _invoke(mt, "update", {"task_id": "t9", "add_tag": "approved", "applied_by": "owner"})
        assert code == 200, body
        assert body["tags"] == ["approved"]

        code, got = _invoke(mt, "get", {"task_id": "t9", "history": True})
        assert [t["tag"] for t in got["tags"]] == ["approved"]
        assert got["tags"][0]["applied_by"] == "owner"
        assert got["tags"][0]["applied_at"]


def test_an_undeclared_tag_is_refused():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "x"})
        code, body = _invoke(mt, "update", {"task_id": "t9", "add_tag": "whatever"})
        assert code == 400
        assert "not declared" in body["error"]


def test_tags_are_a_set_and_do_not_displace_each_other():
    """The line invoice tags draw and this keeps: exclusivity leads to ordering, ordering to legal
    transitions, and a task already has its one ordered thing."""
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "x"})
        for t in ("investigating", "approved"):
            _declare(t)
            _invoke(mt, "update", {"task_id": "t9", "add_tag": t})
        _, got = _invoke(mt, "get", {"task_id": "t9", "history": True})
        assert [t["tag"] for t in got["tags"]] == ["approved", "investigating"]


def test_the_changelog_keeps_a_removal_the_tag_row_loses():
    """Tag rows are current state; the changelog is the journey. Deleting drops the row, so without
    the changelog entry the fact it was ever applied would be gone."""
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "x"})
        _declare("approved")
        _invoke(mt, "update", {"task_id": "t9", "add_tag": "approved"})
        code, body = _invoke(mt, "update", {"task_id": "t9", "delete_tag": "approved"})
        assert code == 200, body

        _, got = _invoke(mt, "get", {"task_id": "t9", "history": True})
        assert got["tags"] == [], "the row is gone"
        moves = [h["changes"]["tag"] for h in got["history"] if "tag" in h.get("changes", {})]
        assert moves == [{"from": None, "to": "approved"}, {"from": "approved", "to": None}]


def test_removing_a_tag_it_does_not_carry_says_so():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "x"})
        code, body = _invoke(mt, "update", {"task_id": "t9", "delete_tag": "approved"})
        assert code == 404


def test_tag_rows_stay_out_of_the_history():
    """They share the partition with the changelog and are not events in it."""
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "x"})
        _declare("approved")
        _invoke(mt, "update", {"task_id": "t9", "add_tag": "approved"})
        _, got = _invoke(mt, "get", {"task_id": "t9", "history": True})
        assert all("tag#" not in str(h.get("sk", "")) for h in got["history"])


def test_every_task_carrying_a_tag_is_one_query():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _declare("approved")
        for t in ("t1", "t2", "t3"):
            _invoke(mt, "put", {"task_id": t, "content": t})
        for t in ("t1", "t3"):
            _invoke(mt, "update", {"task_id": t, "add_tag": "approved"})

        code, body = _invoke(mt, "query", {"tag": "approved"})
        assert code == 200, body
        assert sorted(t["task_id"] for t in body["tasks"]) == ["t1", "t3"]


def test_a_close_can_state_its_reason_in_one_call():
    """The case the whole feature exists for: closed AND why, so a script can tell a human's
    approval from the system closing on success."""
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "close the account?"})
        _declare("approved")
        code, body = _invoke(mt, "update", {"task_id": "t9", "add_tag": "approved", "deliver": "now"})
        assert code == 200, body
        assert body["task"]["delivery"]
        _, got = _invoke(mt, "get", {"task_id": "t9", "history": True})
        assert [t["tag"] for t in got["tags"]] == ["approved"]


def test_one_tag_at_a_time():
    with scratch_env():
        mt = load_lambda("manage_tasks")
        _invoke(mt, "put", {"task_id": "t9", "content": "x"})
        code, body = _invoke(mt, "update", {"task_id": "t9", "add_tag": "a", "delete_tag": "b"})
        assert code == 400 and "one tag at a time" in body["error"]


def test_an_investigation_writes_back_through_the_door():
    """An investigator — a person, the operator gerp's agent, a managed one — writes what it read,
    why, and what to change, and names itself; the registry admits the four, and two findings on
    one task read side by side in the changelog."""
    with scratch_env():
        mt = load_lambda("manage_tasks")
        code, body = _invoke(mt, "put", {"task_id": "t-alarm", "content": "[alarm][westwood] stock_move_failed", "category": "alarm"})
        assert code == 200, body
        code, body = _invoke(mt, "update", {"task_id": "t-alarm", "updates": {
            "investigated_by": "local", "finding": "3 lines: raised_at main.py:_move:66, response 502",
            "root_cause": "the stock move calls inventory before the item exists", "proposed_fix": "create the item on receipt"}})
        assert code == 200, body
        t = body["task"]
        assert t["investigated_by"] == "local" and t["proposed_fix"] == "create the item on receipt"
        code, body = _invoke(mt, "update", {"task_id": "t-alarm", "updates": {
            "investigated_by": "gradienterp", "finding": "same 3 lines", "root_cause": "as local", "proposed_fix": "as local"}})
        assert code == 200 and body["task"]["investigated_by"] == "gradienterp"
        code, body = _invoke(mt, "get", {"task_id": "t-alarm", "history": True})
        assert code == 200
        assert sum(1 for h in body["history"] if "investigated_by" in json.dumps(h)) >= 2, "both findings are in the changelog"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all tasks-flow tests passed")
