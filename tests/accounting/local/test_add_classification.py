"""accounting/add_classification — the owner classifying an account.

It has one side effect: extend the customer's chart_of_accounts registry through schemas'
extend_schema. The cross-lambda invoke dispatches in-process (modules/aws/aws.py), so these
run the REAL extend_schema against the REAL registry table and assert the row that lands — where
they used to swap a fake `lam` global and assert the payload that was handed over.

The classifications DDB is gone; the registry is the single source of truth for account → type.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import env, load_lambda, rows, scratch_env

def _registry():
    return [r for r in rows(os.environ["SCHEMA_TABLE"]) if r["registry"] == "chart_of_accounts"]


def test_extends_the_chart_with_the_owners_account():
    with scratch_env():
        mod = load_lambda("add_classification")
        resp = mod.handler({"account": "AWS", "account_type": "EXPENSE", "notes": "cloud bill"}, None)

        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"status": "added", "account": "AWS", "account_type": "EXPENSE",
                                            "schema_extended": True}

        row = next(r for r in _registry() if r["name"] == "AWS")
        assert row["bucket"] == "expense"          # account_type.lower()
        assert row["bucket_name"] == "expense#AWS"
        assert row["origin"] == "extension"        # never shadows the canonical baseline
        assert row["reason"] == "cloud bill"       # notes passed through
        assert row["schema"] is True


def test_the_new_account_becomes_postable():
    """The point of classifying: post_journal_entry's is_account() check rejects a name the
    registry has never seen, so the extension is what makes the next entry land."""
    with scratch_env():
        entry = {
            "entryId": "aws-1", "timestamp": "1700000000000", "source": "manual", "memo": "AWS",
            "lineItems": [
                {"account": "AWS",  "side": "DEBIT",  "amount": 42.80, "accountType": "EXPENSE"},
                {"account": "CASH", "side": "CREDIT", "amount": 42.80, "accountType": "ASSET"},
            ],
        }
        pje = load_lambda("post_journal_entry")
        assert pje.handler(entry, None)["statusCode"] == 400   # AWS is not in the chart yet

        load_lambda("add_classification").handler({"account": "AWS", "account_type": "EXPENSE"}, None)

        pje = load_lambda("post_journal_entry")   # fresh: the chart is cached per container
        assert pje.handler(entry, None)["statusCode"] == 200


def test_reason_defaults_when_notes_blank():
    with scratch_env():
        mod = load_lambda("add_classification")
        mod.handler({"account": "BLUE_BOTTLE_WHOLESALE", "account_type": "LIABILITY"}, None)

        row = next(r for r in _registry() if r["name"] == "BLUE_BOTTLE_WHOLESALE")
        assert row["bucket"] == "liability"
        assert row["reason"] == "classified by owner as LIABILITY"


def test_returns_200_when_extend_schema_fails():
    """The classification is the owner's answer in a conversation. A registry write that fails
    must not turn into an error at them — it is logged and the turn continues."""
    with scratch_env():
        with env(EXTEND_SCHEMA_FN="gerp-schemas-local-no_such_lambda"):
            mod = load_lambda("add_classification")
            resp = mod.handler({"account": "STRIPE_FEES", "account_type": "EXPENSE"}, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["account"] == "STRIPE_FEES"
        assert body["schema_extended"] is False
        assert not [r for r in _registry() if r["name"] == "STRIPE_FEES"]


def test_accepts_api_gateway_string_body():
    with scratch_env():
        mod = load_lambda("add_classification")
        resp = mod.handler(
            {"body": json.dumps({"account": "TIPS_REVENUE", "account_type": "REVENUE"})}, None)

        assert resp["statusCode"] == 200
        row = next(r for r in _registry() if r["name"] == "TIPS_REVENUE")
        assert row["bucket"] == "revenue"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all add_classification tests passed")
