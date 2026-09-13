"""The collection watch: a charge succeeded, so a webhook is owed.

What this asserts is the loop closing — a settled invoice is silent, an unsettled one strikes the
SAME incident stream payments and invoicing already share, and the strike happens TWICE because
`create_inc_from_log` opens a first strike silently on purpose.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env  # noqa: E402


def _msg(**body):
    return {"Records": [{"body": json.dumps({"invoice_id": "1#abc", "provider": "stripe",
                                             "reference": "ch_1", **body})}]}


def _run(invoice, message=None):
    """Drive one watch message with a canned invoice read; returns (stdout, re-queued bodies)."""
    with scratch_env():
        mod = load_lambda("check_collection")
        queued = []
        mod.h.get_invoice = lambda _id: invoice
        mod.h.watch_collection = lambda body, delay=0: queued.append({**body, **({"_delay": delay} if delay else {})})
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.handler(message or _msg(), None)
        return buf.getvalue(), queued


def _lines(out):
    return [json.loads(l) for l in out.splitlines() if l.startswith("{")]


def test_a_settled_invoice_says_nothing_and_closes_anything_open():
    """The normal path: the webhook already landed. The `ok` is emitted even with no incident open
    — `_on_ok` returns early — because it is what closes the quiet task a slow delivery opened."""
    out, queued = _run({"status": "paid"})
    lines = _lines(out)
    assert [l["incident"] for l in lines] == ["ok"], lines
    assert lines[0]["subject"] == "collection:1#abc"
    assert queued == [], "a settled collection must not be re-queued"


def test_an_unsettled_invoice_strikes_and_looks_again():
    """First strike opens SILENTLY, so one check could never reach the owner. It re-queues itself
    rather than the machinery growing a new notify policy."""
    out, queued = _run({"status": "issued"})
    lines = _lines(out)
    assert lines[0]["incident"] == "fail"
    assert lines[0]["subject"] == "collection:1#abc", "same stream as charge_saved_method"
    assert "webhook has not arrived" in lines[0]["error"]
    assert len(queued) == 1 and queued[0]["attempt"] == 2


def test_the_last_attempt_stops_re_queueing():
    """The second strike is what notifies. Going round again would mail on every lap."""
    out, queued = _run({"status": "issued"}, _msg(attempt=2))
    assert _lines(out)[0]["incident"] == "fail"
    assert queued == [], "an incident is open and the owner has been told"


def test_a_vanished_invoice_is_dropped_not_reported():
    """Nothing to chase, and an incident about a receivable that no longer exists is unactionable."""
    out, queued = _run(None)
    assert _lines(out) == []
    assert queued == []


def test_a_read_failure_redrives_rather_than_losing_the_watch():
    """SQS retries and eventually dead-letters. Swallowing here would drop the only signal."""
    with scratch_env():
        mod = load_lambda("check_collection")

        def _boom(_id):
            raise RuntimeError("invoices unreachable")

        mod.h.get_invoice = _boom
        try:
            mod.handler(_msg(), None)
        except RuntimeError:
            return
        raise AssertionError("a failed invoice read must not be swallowed")


def test_a_charge_inside_its_pre_debit_hold_is_watched_again_without_a_strike():
    """An India charge completes after the bank's 26-hour notice. Until `hold_until` nothing is late:
    no incident line, the same attempt re-queued. After it, the ordinary strike."""
    import time as _t
    out, queued = _run({"status": "issued"}, _msg(hold_until=int(_t.time()) + 3600))
    assert _lines(out) == [], "no strike inside the hold"
    assert len(queued) == 1 and queued[0].get("attempt") is None and queued[0]["hold_until"] > _t.time()
    assert queued[0]["_delay"] == 900, "through the hold at SQS's longest delay"

    out, queued = _run({"status": "issued"}, _msg(hold_until=int(_t.time()) - 60))
    assert _lines(out) == [] and len(queued) == 1, "the attempt and its webhook get a grace past the hold"

    out, queued = _run({"status": "issued"}, _msg(hold_until=int(_t.time()) - 3600))
    [line] = _lines(out)
    assert line["incident"] == "fail" and queued[0]["attempt"] == 2
    assert "pre-debit notice" in line["error"], "a held charge's strike says the cardholder may have declined it"

    out, queued = _run({"status": "paid"}, _msg(hold_until=int(_t.time()) + 3600))
    assert _lines(out)[0]["incident"] == "ok" and queued == [], "settled inside the hold closes it"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all check_collection tests passed")
