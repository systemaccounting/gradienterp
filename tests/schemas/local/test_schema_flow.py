"""Local-mode tests for the schemas registry lambdas.

Covers the two agent tools' local (jsonl) path: write_schema (op: extend|merge) and
read_schema (source: local|canonical). The
load-bearing logic is extend_schema's guards (canonical-overwrite reject,
duplicate idempotency, event emission) — the rest are round-trips.

seed_schema and canonical_pull_invoke are boto3-only (no IS_LAMBDA/local
branch — seed_schema talks S3+DDB, canonical_pull_invoke invokes the agent
runtime), so they need moto/localstack and are out of scope for this harness.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env, read_jsonl, REPO_ROOT  # noqa: F401
sys.path.insert(0, str(REPO_ROOT / 'tests'))
from helpers.localaws import drain, rows as table_rows  # noqa: E402


def _invoke(lam, body):
    resp = lam.handler(body, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_extend_new_extension_writes_row_and_emits_event():
    with scratch_env() as out:
        extend = load_lambda("write_schema")
        code, body = _invoke(extend, {"op": "extend", 
            "registry": "chart_of_accounts",
            "bucket": "revenue",
            "name": "TIPS_REVENUE",
            "schema": True,
            "reason": "tip jar tracking",
        })
        assert code == 200, body
        assert body["ok"] is True and body.get("duplicate") is None

        rows = table_rows(os.environ["SCHEMA_TABLE"])
        assert len(rows) == 1, rows
        assert rows[0]["registry"] == "chart_of_accounts"
        assert rows[0]["bucket"] == "revenue"
        assert rows[0]["name"] == "TIPS_REVENUE"
        assert rows[0]["origin"] == "extension"
        assert rows[0]["bucket_name"] == "revenue#TIPS_REVENUE"

        events = [m["detail"] for m in drain(os.environ["_QUEUE_URL"], expected=99, tries=2)]
        assert len(events) == 1, events
        ev = events[0]
        assert ev["schema_version"] == 1
        assert ev["registry"] == "chart_of_accounts"
        assert ev["bucket"] == "revenue"
        assert ev["name"] == "TIPS_REVENUE"
        assert ev["reason"] == "tip jar tracking"
        assert ev["created_by"] == "agent_session"
        assert ev["openly_operated"] is False  # local default
        assert ev["customer_id"] == "test_customer"


def test_a_new_field_must_declare_what_it_is():
    """The registry is only a complete description of the data if every field says what it is.
    The agent adding a column is the one party who knows whether it holds money, a timing, a
    person or a credential — so extend_schema refuses without it, and a public reader can never
    meet a field nobody classified."""
    with scratch_env():
        extend = load_lambda("write_schema")
        base = {"registry": "contact_fields", "bucket": "common", "name": "nickname"}

        code, body = _invoke(extend, {"op": "extend", 
            **base, "schema": {"type": "string", "required": False}, "reason": "no class"})
        assert code == 400 and "class" in body["error"], body

        code, body = _invoke(extend, {"op": "extend", 
            **base, "schema": {"type": "string", "required": False, "class": "nonsense"},
            "reason": "bad class"})
        assert code == 400 and body["got"] == "nonsense", body

        code, body = _invoke(extend, {"op": "extend", 
            **base, "schema": {"type": "string", "required": False, "class": "subject"},
            "reason": "declared"})
        assert code == 200, body


def test_a_list_registry_needs_no_class():
    """chart_of_accounts carries `schema: true` — a membership marker ("this account exists"),
    not a field with properties. Whether a ledger row publishes is decided by the LEDGER's field
    classes, not by an account's name, so requiring one here would be a category error."""
    with scratch_env():
        extend = load_lambda("write_schema")
        code, body = _invoke(extend, {"op": "extend", 
            "registry": "chart_of_accounts", "bucket": "expense", "name": "STRIPE_FEES",
            "schema": True, "reason": "classified by owner as EXPENSE"})
        assert code == 200, body


def test_extend_duplicate_is_idempotent():
    with scratch_env() as out:
        extend = load_lambda("write_schema")
        payload = {
            "op": "extend",
            "registry": "contact_fields", "bucket": "vendor", "name": "loyalty_tier",
            "schema": {"type": "string", "required": False, "class": "operational"}, "reason": "first",
        }
        c1, b1 = _invoke(extend, payload)
        c2, b2 = _invoke(extend, {"op": "extend", **payload, "reason": "second"})
        assert c1 == 200 and b1.get("duplicate") is None, b1
        assert c2 == 200 and b2["duplicate"] is True, b2
        # the duplicate neither re-writes the row nor re-emits the event
        assert len(table_rows(os.environ["SCHEMA_TABLE"])) == 1
        assert len([m["detail"] for m in drain(os.environ["_QUEUE_URL"], expected=99, tries=2)]) == 1


def test_extend_rejects_overwrite_of_canonical():
    with scratch_env():
        merge = load_lambda("write_schema")
        extend = load_lambda("write_schema")
        mc, mb = _invoke(merge, {"op": "merge", 
            "registry": "chart_of_accounts",
            "entries": [{"bucket": "asset", "name": "CASH", "schema": True}],
        })
        assert mc == 200, mb
        code, body = _invoke(extend, {"op": "extend", 
            "registry": "chart_of_accounts", "bucket": "asset", "name": "CASH",
            "schema": True, "reason": "trying to shadow canonical",
        })
        assert code == 409, body
        assert "canonical" in body["error"]


def test_extend_unknown_registry_400():
    with scratch_env():
        extend = load_lambda("write_schema")
        code, body = _invoke(extend, {"op": "extend", 
            "registry": "bogus", "bucket": "x", "name": "Y", "schema": True, "reason": "r",
        })
        assert code == 400, body
        assert "unknown registry" in body["error"]


def test_registry_list_comes_from_canonical():
    # The canonical bucket IS the list. A registry added later (item_fields, labor_fields)
    # has to be reachable without editing the tools — a hardcoded tuple froze the pull at
    # three registries while the seed wrote seven, so four of them could never be updated.
    with scratch_env():
        read_canonical = load_lambda("read_schema")
        code, body = _invoke(read_canonical, {"source": "canonical", })
        assert code == 200, body
        names = body["registries"]
        for expected in ("chart_of_accounts", "contact_fields", "calendar_fields",
                         "item_fields", "labor_fields", "note_fields", "task_fields"):
            assert expected in names, f"{expected} missing from {names}"
        # values, not fields — a different table, no owner approval
        assert "rule_params" not in names
        # the a2a public registry — operator-side, never a per-gerp registry
        assert "profile_fields" not in names


def test_item_fields_pulls_and_merges():
    # The registries the hardcoded tuple locked out: fetchable from canonical, mergeable
    # into the local registry, readable back.
    with scratch_env():
        read_canonical = load_lambda("read_schema")
        merge = load_lambda("write_schema")
        read_local = load_lambda("read_schema")

        code, body = _invoke(read_canonical, {"source": "canonical", "registry": "item_fields", "detail": True})
        assert code == 200, body
        bucket, fields = next(iter(body["content"].items()))
        name, schema = next(iter(fields.items()))

        code, _ = _invoke(merge, {"op": "merge", "registry": "item_fields",
                                  "entries": [{"bucket": bucket, "name": name, "schema": schema}]})
        assert code == 200

        code, body = _invoke(read_local, {"source": "local", "registry": "item_fields", "detail": True})
        assert code == 200, body
        entry = next(e for e in body["entries"] if e["name"] == name)
        assert entry["origin"] == "canonical"


def test_read_local_empty_registry():
    with scratch_env():
        read_local = load_lambda("read_schema")
        code, body = _invoke(read_local, {"source": "local", "registry": "calendar_fields"})
        assert code == 200, body
        assert body["registry"] == "calendar_fields"
        assert body["fields"] == {}, "a read is names and types unless detail=true"


def test_extend_then_read_local_roundtrip():
    with scratch_env():
        extend = load_lambda("write_schema")
        read_local = load_lambda("read_schema")
        _invoke(extend, {"op": "extend", 
            "registry": "chart_of_accounts", "bucket": "expense",
            "name": "SAAS_SUBSCRIPTIONS", "schema": True, "reason": "tools",
        })
        code, body = _invoke(read_local, {"source": "local", "registry": "chart_of_accounts", "detail": True})
        assert code == 200, body
        entry = next(e for e in body["entries"] if e["name"] == "SAAS_SUBSCRIPTIONS")
        assert entry["origin"] == "extension"
        assert entry["bucket"] == "expense"


def test_merge_canonical_then_read_local():
    with scratch_env():
        merge = load_lambda("write_schema")
        read_local = load_lambda("read_schema")
        code, body = _invoke(merge, {"op": "merge", "registry": "contact_fields", "entries": [
            {"bucket": "common", "name": "email", "schema": {"type": "string", "required": True, "class": "subject"}},
            {"bucket": "common", "name": "phone", "schema": {"type": "string", "required": False, "class": "subject"}},
        ]})
        assert code == 200, body
        assert body["merged_count"] == 2
        rc, rb = _invoke(read_local, {"source": "local", "registry": "contact_fields", "detail": True})
        assert rc == 200, rb
        assert {e["name"] for e in rb["entries"]} == {"email", "phone"}
        assert all(e["origin"] == "canonical" for e in rb["entries"])


def test_merge_unknown_registry_400():
    with scratch_env():
        merge = load_lambda("write_schema")
        code, body = _invoke(merge, {"op": "merge", "registry": "nope", "entries": []})
        assert code == 400, body


def test_read_canonical_chart_of_accounts():
    with scratch_env():
        read_canon = load_lambda("read_schema")
        code, body = _invoke(read_canon, {"source": "canonical", "registry": "chart_of_accounts", "detail": True})
        assert code == 200, body
        assert body["registry"] == "chart_of_accounts"
        # canonical chart_of_accounts is {bucket: [name, ...]}; expect the 5 buckets
        assert {"asset", "liability", "equity", "revenue", "expense"} <= set(body["content"].keys())


def test_read_canonical_unknown_registry_400():
    with scratch_env():
        read_canon = load_lambda("read_schema")
        code, body = _invoke(read_canon, {"source": "canonical", "registry": "bogus"})
        assert code == 400, body


def test_seed_writes_canonical_and_is_idempotent():
    with scratch_env() as out:
        seed = load_lambda("seed_schema")
        resp = seed.handler({"customer_id": "test_customer"}, None)
        assert resp["ok"] is True, resp
        seeded = resp["schema"]
        assert seeded["chart_of_accounts"] > 0
        assert set(seeded) >= {"chart_of_accounts", "contact_fields", "calendar_fields"}
        assert resp["rule_params"] > 0  # the GENERAL rule params seed in the same pass

        rows = table_rows(os.environ["SCHEMA_TABLE"])
        assert len(rows) == sum(seeded.values())  # schema rows; rule params go to a separate file
        assert all(r["origin"] == "canonical" for r in rows)
        rp = table_rows(os.environ["RULES_PARAMS_TABLE"])
        assert rp and all(r["pk"] == "GENERAL" and r["origin"] == "canonical" for r in rp)

        # idempotent per target: a second seed skips schema, rule params are up to date
        again = seed.handler({"customer_id": "test_customer"}, None)
        assert again == {"ok": True, "schema": "already seeded", "rule_params": "up to date"}
        assert len(table_rows(os.environ["SCHEMA_TABLE"])) == len(rows)


def test_rule_params_seed_appends_a_new_tax_year():
    # the weekly re-seed propagates an update: a new effective_from appends new GENERAL rows
    # (platform tax tables, no owner approval) — the same year is a no-op.
    with scratch_env() as out:
        seed = load_lambda("seed_schema")
        seed.handler({"customer_id": "test_customer"}, None)
        rp_2026 = table_rows(os.environ["RULES_PARAMS_TABLE"])
        assert {r["effective_from"] for r in rp_2026} == {"2026-01-01"}

        # canonical now publishes a newer tax year
        canon = out / "canon2027"
        canon.mkdir()
        base = json.loads((REPO_ROOT / "modules" / "schemas" / "data" / "rule_params.json").read_text())
        base["effective_from"] = "2027-01-01"
        (canon / "rule_params.json").write_text(json.dumps(base))
        os.environ["LOCAL_CANONICAL_DIR"] = str(canon)

        resp = load_lambda("seed_schema").handler({"customer_id": "test_customer"}, None)
        assert resp["rule_params"] == len(base["params"])  # appended the new year's rows
        rp = table_rows(os.environ["RULES_PARAMS_TABLE"])
        assert len(rp) == len(rp_2026) + len(base["params"])
        assert {r["effective_from"] for r in rp} == {"2026-01-01", "2027-01-01"}


def test_seed_then_read_local_has_canonical_accounts():
    with scratch_env():
        load_lambda("seed_schema").handler({"customer_id": "test_customer"}, None)
        read_local = load_lambda("read_schema")
        code, body = _invoke(read_local, {"source": "local", "registry": "chart_of_accounts", "detail": True})
        assert code == 200, body
        cash = next((e for e in body["entries"] if e["name"] == "CASH"), None)
        assert cash is not None, "CASH should be seeded from canonical"
        assert cash["origin"] == "canonical"
        assert cash["schema"] is True  # chart_of_accounts entries are bool


def test_seed_field_registry_schema_is_object():
    with scratch_env():
        load_lambda("seed_schema").handler({"customer_id": "test_customer"}, None)
        read_local = load_lambda("read_schema")
        code, body = _invoke(read_local, {"source": "local", "registry": "contact_fields", "detail": True})
        assert code == 200, body
        assert body["entries"], "contact_fields should have canonical entries"
        assert all(isinstance(e["schema"], dict) for e in body["entries"])


def test_seed_missing_canonical_files_skips_cleanly():
    with scratch_env() as out:
        # point the canonical dir at the empty scratch dir (no *.json there)
        os.environ["LOCAL_CANONICAL_DIR"] = str(out)
        seed = load_lambda("seed_schema")
        resp = seed.handler({"customer_id": "test_customer"}, None)
        assert resp == {"ok": True, "schema": {}, "rule_params": "not found"}, resp
        assert table_rows(os.environ["SCHEMA_TABLE"]) == []


class _FakeAgentCore:
    """Captures invoke_agent_runtime calls — canonical_pull_invoke is fire-and-forget."""

    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"statusCode": 200}


def test_canonical_pull_invokes_agent_with_prompt():
    with scratch_env():
        mod = load_lambda("canonical_pull_invoke")
        mod.agentcore = _FakeAgentCore()
        resp = mod.handler({}, None)
        assert resp == {"ok": True, "customer_id": "test_customer"}, resp
        assert len(mod.agentcore.calls) == 1
        call = mod.agentcore.calls[0]
        # The arn must be SPLIT: the runtime, plus the endpoint name as `qualifier`. Passing the
        # full runtime-endpoint arn makes AWS append `/runtime-endpoint/DEFAULT` to an
        # already-qualified arn and the invoke is denied. This test previously asserted the whole
        # arn was passed — it encoded the bug, and the cron failed every week in prod while green.
        assert call["agentRuntimeArn"] == mod.RUNTIME_ARN
        assert "/runtime-endpoint/" not in call["agentRuntimeArn"]
        assert call["qualifier"] == mod.QUALIFIER
        assert call["runtimeSessionId"].startswith("canonical-pull-")
        payload = json.loads(call["payload"].decode())
        assert "read_schema" in payload["prompt"]
        assert "write_schema" in payload["prompt"]


def test_canonical_pull_uses_fresh_session_each_fire():
    with scratch_env():
        mod = load_lambda("canonical_pull_invoke")
        mod.agentcore = _FakeAgentCore()
        mod.handler({}, None)
        mod.handler({}, None)
        sids = [c["runtimeSessionId"] for c in mod.agentcore.calls]
        assert len(set(sids)) == 2  # a fresh session id per weekly fire


def test_missing_or_unknown_discriminators_are_refused():
    """One door per direction: the read names its source, the write names its op, and a call
    that names neither is told what the choices are before anything is touched."""
    with scratch_env():
        write = load_lambda("write_schema")
        read = load_lambda("read_schema")
        code, body = _invoke(write, {"registry": "chart_of_accounts"})
        assert code == 400 and "extend or merge" in body["error"], body
        code, body = _invoke(write, {"op": "upsert", "registry": "chart_of_accounts"})
        assert code == 400, body
        code, body = _invoke(read, {"registry": "chart_of_accounts"})
        assert code == 400 and "local or canonical" in body["error"], body
        code, body = _invoke(read, {"source": "ddb", "registry": "chart_of_accounts"})
        assert code == 400, body


def test_extend_names_what_is_missing_and_local_read_needs_a_registry():
    with scratch_env():
        write = load_lambda("write_schema")
        read = load_lambda("read_schema")
        code, body = _invoke(write, {"op": "extend", "registry": "chart_of_accounts", "name": "X"})
        assert code == 400 and "bucket" in body["error"] and "reason" in body["error"], body
        code, body = _invoke(read, {"source": "local"})
        assert code == 400 and "registry is required" in body["error"], body


def test_a_read_is_names_and_types_unless_detail_is_asked():
    """A registry in full is 10–16KB and the model read ten of them in a session to learn field
    names. The default read answers that in ~1KB; detail=true is the entry as stored."""
    with scratch_env():
        read = load_lambda("read_schema")
        code, body = _invoke(read, {"source": "canonical", "registry": "item_fields"})
        assert code == 200, body
        bucket, fields = next(iter(body["fields"].items()))
        name, typ = next(iter(fields.items()))
        assert isinstance(typ, str) and typ.split("+")[0] in ("string", "number", "boolean", "array", "object", "integer")
        assert "description" not in str(body["fields"]), "descriptions only ride with detail=true"
        code, full = _invoke(read, {"source": "canonical", "registry": "item_fields", "detail": True})
        assert isinstance(full["content"][bucket][name], dict)
        code, lst = _invoke(read, {"source": "canonical"})
        assert "registries" in lst, "the list itself is untouched"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all schema tests passed")
