"""End-to-end: stripe webhook → payments ingest → accounting's post_journal_entry.

Proves the cross-module contract the spec promises — the webhook lands as a real entry in
accounting's pending queue (no accountType → not yet in the ledger).

This used to load accounting's handler by hand and monkeypatch payments' `post_journal_entry` to
point at it, because in local mode the real invoke only appended to a jsonl. The cross-lambda invoke
dispatches in-process now (`modules/aws/aws.py` resolves `POST_JOURNAL_ENTRY_FN` to
`modules/accounting/lambdas/post_journal_entry`), so the seam under test is the shipping one and
the test wires nothing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, scratch_env

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from helpers.localaws import ledger_rows, pending_rows  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "testdata" / "stripe"


def test_stripe_charge_lands_in_accounting_pending():
    with scratch_env():
        ing = load_lambda("ingest_stripe")
        import hashlib, hmac, os
        from aws import client
        client("ssm").put_parameter(Name=f"{os.environ['SECRET_PARAM_PREFIX']}/stripe/signing_secret",
                                    Value="whsec_test_signing", Type="SecureString", Overwrite=True)
        raw = (FIXTURES / "charge.succeeded.json").read_text()
        mac = hmac.new(b"whsec_test_signing", b"1700000000." + raw.encode(), hashlib.sha256).hexdigest()
        resp = ing.handler({"body": raw, "headers": {"stripe-signature": f"t=1700000000,v1={mac}"}}, None)
        assert json.loads(resp["body"])["status"] == "posted", resp

        # no accountType → the entry sits in accounting's pending queue, not the ledger
        pending = pending_rows()
        assert len(pending) == 1, pending
        assert pending[0]["entry_id"] == "ch_3Tek05RBOqTW9S9W1jMOvxaj"
        assert ledger_rows() == []  # not promoted until classified

        # the legs the transform produced, carried across the module boundary intact
        legs = json.loads(pending[0]["line_items"])
        assert {(x["account"], x["side"]) for x in legs} == {
            ("CASH_IN_TRANSIT_STRIPE", "DEBIT"), ("SALES_REVENUE", "CREDIT")}
        assert all("accountType" not in x for x in legs)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all stripe→accounting e2e tests passed")
