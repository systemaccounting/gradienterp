"""run_automation — a callsite handing a moment to the firm's own code.

The rule that makes any react callsite scriptable. What is worth pinning is that it ANNOUNCES rather
than invoking (a synchronous script inside an invoice transition could stall or fail the write), that
the script's rule instances are resolved HERE where the table is, and that what goes out is exactly
what `automate` reads.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules/rules"))
sys.path.insert(0, str(REPO / "modules/aws"))

import dispatch_rules as dr   # noqa: E402
import rules                  # noqa: E402


def _run(ctx, sent, rows=None, **param):
    dr.events = type("E", (), {"emit": staticmethod(lambda s, d, det: sent.append((s, d, det)))})
    dr.instances = type("I", (), {"AUTOMATION": "AUTOMATION",
                                  "at": staticmethod(lambda kind, subj: (rows or {}).get(subj, []))})
    return rules.run_instances(ctx, [{"pk": "INVOICE_STATUS#issued", "sk": "0100#collect",
                                      "name": "collect", "rule": "run_automation", "n": 100,
                                      "param": {"script": "collections/charge_next_card.py", **param}}],
                               modules=[dr])


def test_it_announces_what_automate_reads():
    """The detail IS automate's payload — the EventBridge target passes `$.detail` straight in, so
    a mismatch here means a transformer somewhere."""
    sent = []
    _run({"invoice_id": "INV-9", "to": "issued"}, sent)

    source, detail_type, detail = sent[0]
    assert (source, detail_type) == ("rules", "automation.requested")
    assert detail["script"] == "collections/charge_next_card.py"
    assert detail["params"] == {"invoice_id": "INV-9", "to": "issued"}


def test_the_engines_provenance_does_not_reach_the_script():
    """`run_instances` appends `rule_exec_id` to the ctx it was handed. That is a record of which
    rules ran, not something a script has any use for."""
    sent = []
    _run({"invoice_id": "INV-9"}, sent)
    assert "rule_exec_id" not in sent[0][2]["params"]


def test_the_scripts_instances_are_resolved_here():
    """`automate` holds no DDB. This runs inside a callsite lambda that already has the table, and a
    rule's params never depend on a result the script has not produced yet."""
    sent = []
    rows = {"collection_order": [{"pk": "AUTOMATION#collection_order", "rule": "retry_order"}]}
    _run({"invoice_id": "INV-9"}, sent, rows=rows, subjects=["collection_order", "collection_retry"])

    got = sent[0][2]["rules"]
    assert got["collection_order"] == rows["collection_order"]
    assert got["collection_retry"] == [], "a subject with nothing attached still travels, empty"


def test_no_subjects_means_no_rules_travel():
    sent = []
    _run({"invoice_id": "INV-9"}, sent)
    assert sent[0][2]["rules"] == {}


def test_it_is_resolvable_only_where_a_callsite_ignores_returns():
    """Two fences, both by LIBRARY. The four callsites that consume returns fold them into postings,
    where a dispatch row is a leg with no account, side or amount. And the `automation` callsite is
    absent on purpose: putting this library there would let a script dispatch at its own subject,
    which is the tight self-repeat nothing refuses."""
    import callsites as C
    consumes = {"pay_run", "close_shift", "invoice_line", "distribution"}
    for c in C.CALLSITES:
        if c.name in consumes:
            assert "dispatch_rules" not in c.libs, f"{c.name} folds returns into postings"
    listed = {c.name for c in C.CALLSITES if "dispatch_rules" in c.libs}
    assert listed == {"invoice_status", "invoice_tag"}, listed



# ─── add_scheduled_automation ───
#
# The sibling that schedules instead of calling. What matters is the same property `run_automation`
# has — the detail IS what the consumer reads, so no transformer sits between them — plus the two
# things unique to scheduling: a per-record name, and a bound that makes a repeat stop.

def _sched(ctx, sent, rows=None, **param):
    dr.events = type("E", (), {"emit": staticmethod(lambda s, d, det: sent.append((s, d, det)))})
    dr.instances = type("I", (), {"AUTOMATION": "AUTOMATION",
                                  "at": staticmethod(lambda kind, subj: (rows or {}).get(subj, []))})
    return rules.run_instances(
        ctx, [{"pk": "INVOICE_STATUS#unpaid", "sk": "0100#chase", "name": "chase",
               "rule": "add_scheduled_automation", "n": 100,
               "param": {"script": "collections/notice.py", **param}}],
        modules=[dr])


def test_scheduling_announces_what_schedule_automation_reads():
    sent = []
    _sched({"invoice_id": "INV-9", "from": "issued"}, sent,
           expression="rate(3 days)", until="2026-09-15T00:00:00Z", names_it="invoice_id")
    assert len(sent) == 1
    source, detail_type, d = sent[0]
    assert (source, detail_type) == ("rules", "automation.scheduled")
    assert set(d) == {"script", "schedule_expression", "subject", "start_date", "end_date",
                      "one_shot", "params", "rules"}
    assert d["script"] == "collections/notice.py"
    assert d["schedule_expression"] == "rate(3 days)"
    assert d["end_date"] == "2026-09-15T00:00:00Z"


def test_names_it_puts_the_record_in_the_subject():
    """Without it two invoices reaching the same status compose one name, and the second is refused
    as a duplicate — a silent gap in the sequence for whichever came later."""
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)", names_it="invoice_id")
    assert sent[0][2]["subject"] == "INV-9"

    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)")
    assert sent[0][2]["subject"] == "", "no names_it means no per-record subject"


def test_a_missing_field_does_not_send_none_as_a_subject():
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)", names_it="nope")
    assert sent[0][2]["subject"] == ""


def test_at_is_one_shot_and_rate_is_not():
    """`at(...)` fires once, so the schedule removes itself. A rate has to be told when to stop."""
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="at(2026-09-01T09:00:00)")
    assert sent[0][2]["one_shot"] is True
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)")
    assert sent[0][2]["one_shot"] is False


def test_it_carries_the_scripts_rule_instances():
    """`automate` holds no table, so a scheduled script cannot look its own up when it runs."""
    sent = []
    rows = {"NOTICE#dunning": [{"pk": "AUTOMATION#NOTICE#dunning", "rule": "compose_notice"}]}
    _sched({"invoice_id": "INV-9"}, sent, rows=rows,
           expression="rate(3 days)", subjects=["NOTICE#dunning"])
    assert sent[0][2]["rules"] == rows


def test_the_provenance_field_does_not_reach_the_script():
    sent = []
    _sched({"invoice_id": "INV-9", "rule_exec_id": ["x"]}, sent, expression="rate(3 days)")
    assert "rule_exec_id" not in sent[0][2]["params"]


def test_both_dispatch_rules_are_offered():
    assert sorted(rules.offered_rules([dr])) == ["add_scheduled_automation", "run_automation"]



def test_a_relative_offset_resolves_when_the_callsite_fires():
    """A row is static and a schedule needs an instant. `after: "15 days"` has to mean fifteen days
    after THIS transition, not fifteen days after somebody wrote the row."""
    import datetime
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, after="15 days", names_it="invoice_id")
    d = sent[0][2]
    assert d["schedule_expression"].startswith("at(")
    assert d["one_shot"] is True
    when = datetime.datetime.strptime(d["schedule_expression"][3:-1], "%Y-%m-%dT%H:%M:%S")
    days = (when - datetime.datetime.utcnow()).days
    assert 14 <= days <= 15, days


def test_until_bounds_a_repeat_and_is_also_relative():
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)", until="15 days")
    d = sent[0][2]
    assert d["schedule_expression"] == "rate(3 days)"
    assert d["end_date"].endswith("Z") and d["one_shot"] is False


def test_a_fixed_date_still_passes_through():
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)", until="2026-09-15T00:00:00Z")
    assert sent[0][2]["end_date"] == "2026-09-15T00:00:00Z"


def test_neither_expression_nor_after_is_refused_rather_than_emitted():
    """A schedule with no when is a schedule that never fires, and emitting one would look like it
    worked."""
    sent = []
    out = _sched({"invoice_id": "INV-9"}, sent)
    assert sent == []
    assert out[0]["refused"]


def test_an_unparseable_offset_is_no_bound_rather_than_a_guess():
    sent = []
    _sched({"invoice_id": "INV-9"}, sent, expression="rate(3 days)", until="soon")
    assert sent[0][2]["end_date"] == ""


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all automation_rules tests passed")
