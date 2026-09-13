"""gradienterp's `closure/notice.py`: the mail an owner gets during the window, and when not.

Every step of the sequence re-reads before acting. A requested closure's notice reads the row
through the operator role and is quiet unless the row still reads as closing or closed — the
lifecycle rehearsal applies westwood back into its account inside the window, and a gerp that is
up again is not told it is shut. The address and the label come from `begin`; without an address
nothing is sent, which is how westwood's first notice went unsent (2026-09-06).
"""

import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "prod" / "gradienterp" / "automations" / "closure" / "notice.py"


class Ctx:
    def __init__(self):
        self.sent = []

    def call(self, name, params):
        assert name == "send_email", name
        self.sent.append(params)
        return {"ok": True}


def _load(status):
    spec = importlib.util.spec_from_file_location("closure_notice", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._row_status = lambda role, table, gerp_id: status
    os.environ["CLOSURE_REQUESTER_ROLE_ARN"] = "arn:aws:iam::1:role/closure"
    os.environ["CUSTOMERS_TABLE"] = "gerp-customers"
    return mod


def test_a_requested_closure_mails_the_owner_by_label_while_the_row_reads_closed():
    for status in ("close_requested", "closing", "closed"):
        ctx = Ctx()
        out = _load(status).run(ctx, "westwood-c40fd8", requested_by="owner-sub", closes_on="2026-09-21T03:41:44Z",
                                to="owner@westwood.example", label="westwood")
        assert out == {"gerp_id": "westwood-c40fd8", "to": "owner@westwood.example"}
        [mail] = ctx.sent
        assert mail["to"] == "owner@westwood.example"
        assert mail["subject"] == "your westwood gerp is closed, export ready until 2026-09-21"
        body = mail["body"]
        assert body.startswith("Your westwood gerp has been shut down at your request.\n\n")
        assert "https://gradienterp.cloud" in body and "closes on 2026-09-21" in body
        assert body.count("\n\n") == 3, "four paragraphs, blank lines between"


def test_a_gerp_that_is_up_again_is_not_told_it_is_shut():
    for status in ("active", "provisioning", "stopped", ""):
        ctx = Ctx()
        out = _load(status).run(ctx, "westwood-c40fd8", requested_by="owner-sub", closes_on="2026-09-21T03:41:44Z",
                                to="owner@westwood.example", label="westwood")
        assert out["skipped"].startswith("row is") and ctx.sent == []


def test_no_address_sends_nothing():
    ctx = Ctx()
    out = _load("closing").run(ctx, "westwood-c40fd8", requested_by="owner-sub", closes_on="2026-09-21T03:41:44Z")
    assert out["skipped"] == "no address to send to" and ctx.sent == []


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all closure notice tests passed")
