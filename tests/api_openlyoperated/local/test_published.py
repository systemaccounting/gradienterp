"""published: a gerp's openly_operated flip, announced on the bus, lands on its row — only when it was
sent from the gerp's own account."""

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "gradienterp_cloud"))
from _helpers import scratch_env, rows  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("op_published", REPO / "prod/api_openlyoperated/lambdas/published/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(gerp_id):
    from aws import client
    client("dynamodb").put_item(TableName=os.environ["CUSTOMERS_TABLE"], Item={
        "gerp_id": {"S": gerp_id}, "status": {"S": "active"}, "aws_account_id": {"S": ACCT[gerp_id]}})


ACCT = {"cafe": "111111111111", "mallory": "222222222222"}


def test_the_flip_lands_on_the_row_and_a_stranger_is_skipped():
    with scratch_env():
        mod = _load()
        _row("cafe")
        out = mod.handler({"detail-type": "gerp.published", "account": ACCT["cafe"], "detail": {"gerp_id": "cafe", "at": "2026-09-04T00:00:00Z"}}, None)
        assert out == {"gerp_id": "cafe", "published": True}
        [r] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == "cafe"]
        assert r["published"] is True and r["published_at"] == "2026-09-04T00:00:00Z"
        out = mod.handler({"detail-type": "gerp.unpublished", "account": ACCT["cafe"], "detail": {"gerp_id": "cafe", "at": "2026-09-05T00:00:00Z"}}, None)
        assert out["published"] is False
        [r] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == "cafe"]
        assert r["published"] is False
        # a flip for a gerp with no row here, and an event of another kind
        assert mod.handler({"detail-type": "gerp.published", "account": ACCT["cafe"], "detail": {"gerp_id": "ghost"}}, None) == {"skipped": "no row", "gerp_id": "ghost"}
        assert mod.handler({"detail-type": "journal_entry.posted", "detail": {"gerp_id": "cafe"}}, None)["skipped"] == "not a publish flip"
        assert len(rows(os.environ["CUSTOMERS_TABLE"])) == 1


def test_a_flip_from_another_account_is_refused_and_the_row_stays():
    """Any account in the organization can put on the bus; only the gerp's own account flips its row."""
    with scratch_env():
        mod = _load()
        _row("cafe")
        _row("mallory")
        for account in (ACCT["mallory"], ""):
            out = mod.handler({"detail-type": "gerp.published", "account": account, "detail": {"gerp_id": "cafe"}}, None)
            assert out["refused"] and out["gerp_id"] == "cafe", account
        [r] = [r for r in rows(os.environ["CUSTOMERS_TABLE"]) if r["gerp_id"] == "cafe"]
        assert "published" not in r


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all published tests passed")
