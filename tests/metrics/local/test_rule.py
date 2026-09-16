"""record_metric: a callsite's moment recorded as a product event, by a row.

What is worth pinning: the rule reads the subject and the properties off the ctx by the names the
row gives, sends on the firm's bus with `via: rule`, and returns nothing — so a callsite that folds
returns into postings is untouched. A row naming a field the moment lacks records nothing and
raises nothing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import drained, scratch_env

import metric_rules
import rules


def _inst(param, n=300):
    return {"pk": "INVOICE_STATUS#paid", "sk": f"{n:04d}#joined", "rule": "record_metric", "param": param}


def test_the_moment_becomes_an_event_named_by_the_row():
    with scratch_env():
        ctx = {"invoice_id": "inv-7", "customer": "c_12", "total": 45.5, "from": "issued"}
        out = rules.run_instances(ctx, [_inst({"event": "member.joined", "subject": "customer",
                                               "properties": {"amount": "total", "was": "from"}})],
                                  modules=[metric_rules])
        assert out == [], "the rule returns nothing; the event is the effect"
        got = drained()
        assert got[0]["detail_type"] == "member.joined"
        d = got[0]["detail"]
        assert d["subject_id"] == "c_12" and d["via"] == "rule"
        assert d["properties"] == {"amount": "45.5", "was": "issued"}
        assert d["rule_exec_id"] == ctx["rule_exec_id"][-1], "provenance rides on the event"


def test_a_missing_subject_records_nothing_and_raises_nothing():
    with scratch_env():
        out = rules.run_instances({"invoice_id": "inv-7"},
                                  [_inst({"event": "member.joined", "subject": "contact_id"})],
                                  modules=[metric_rules])
        assert out == []
        assert not drained(expected=0)


def test_it_is_offered_with_its_params():
    spec = rules.spec(metric_rules.record_metric)
    assert set(spec) == {"event", "subject", "properties"}
    assert spec["event"]["required"] and spec["subject"]["required"] and not spec["properties"]["required"]


if __name__ == "__main__":
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
