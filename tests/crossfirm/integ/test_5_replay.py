"""Case 5 — idempotency under replay.

Puppet's one remaining job: the same inbound event delivered twice. `receive_inbound` keys the
row on the event id, so a re-delivery overwrites (a MODIFY on the stream, which the router
ignores): one inbox row, one stamp, one settle. Here the first delivery is real — gradienterp
proposes, westwood's shelf rule accepts and both settle — and the second is the recipient's
own inbox row handed back to `receive_inbound` as the dispatcher would hand it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (GRADIENTERP, WESTWOOD, SHELF_ITEM, invoke, poll, agreement, inbox_rows, invoice_row,
                      rule_add, rule_delete, session, dash, thread_id, seed_westwood_shelf, both_up)


def test_the_same_inbound_event_twice_is_one_row_one_stamp_one_settle():
    why = both_up()
    if why:
        print(f"skip: {why}"); return
    seed_westwood_shelf(8)
    rule_add(WESTWOOD, "PROPOSAL#po", 100, "crossfirm-shelf", "accept_in_stock", {})
    thread = thread_id("replay")
    try:
        code, body = invoke(GRADIENTERP, "agreements", "request", {
            "kind": "po", "vendor": WESTWOOD, "thread": thread, "memo": "crossfirm-test",
            "lines": [{"description": "Oat Milk (case of 12)", "amount": 32, "sku": SHELF_ITEM, "qty": 1}]})
        th = body["terms_hash"]
        first = poll(lambda: agreement(WESTWOOD, thread, th), lambda r: r is not None and r.get("settled_time"))
        [row] = poll(lambda: inbox_rows(WESTWOOD, thread=thread, detail_type="po.proposed"), lambda r: len(r) == 1)
        invoice = poll(lambda: invoice_row(WESTWOOD, thread), lambda r: r is not None)

        # the replay: the dispatcher's own shape, the same id
        event = {"id": row["inbound_id"], "source": row["source"], "detail-type": row["detail_type"],
                 "account": row["from_account"], "detail": json.loads(row["detail"])}
        r = session(WESTWOOD).client("lambda").invoke(FunctionName=f"gerp-inbox-{dash(WESTWOOD)}-receive_inbound",
                                                       Payload=json.dumps(event).encode())
        assert not r.get("FunctionError")
        import time; time.sleep(20)
        assert len(inbox_rows(WESTWOOD, thread=thread, detail_type="po.proposed")) == 1
        after = agreement(WESTWOOD, thread, th)
        assert after["settled_time"] == first["settled_time"] and after["seller_stamp"] == first["seller_stamp"]
        assert invoice_row(WESTWOOD, thread) == invoice
    finally:
        rule_delete(WESTWOOD, "PROPOSAL#po", 100, "crossfirm-shelf")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all crossfirm case 5 tests passed")
