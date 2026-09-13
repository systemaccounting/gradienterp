"""`deploy.py stop` and `start`: the refusal before a build starts.

A build against the wrong row is the failure that matters here. `start` into a `closed` row
applies a stack under a `closure/close.py` schedule that closes the account on day 15; `stop` on
the seller's own gerp destroys the closure scripts, the hub's trust and the seller's books. The
check is one function over the row, so it runs here with rows and no AWS.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _deploy():
    spec = importlib.util.spec_from_file_location("deploy", REPO / "scripts" / "deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stop_takes_an_active_gerp_that_is_not_the_sellers():
    d = _deploy()
    ok = {"gerp_id": "westwood-c40fd8", "status": "active", "aws_account_id": "222165865776"}
    assert d.teardown_check(ok, "stop") == ""
    for status in ("provisioning", "stopped", "closing", "closed", "awaiting_payment", ""):
        why = d.teardown_check({**ok, "status": status}, "stop")
        assert why and (status or "unset") in why, f"stop on a {status or 'unset'} row must be refused by name"
    why = d.teardown_check({**ok, "gerp_id": d.SELLER_GERP}, "stop")
    assert why and d.SELLER_GERP in why and "seller" in why
    assert d.teardown_check(None, "stop")
    assert d.teardown_check({**ok, "aws_account_id": ""}, "stop")


def test_start_takes_a_stopped_gerp_and_names_the_closed_case():
    d = _deploy()
    ok = {"gerp_id": "westwood-c40fd8", "status": "stopped", "aws_account_id": "222165865776"}
    assert d.teardown_check(ok, "start") == ""
    for status in ("active", "provisioning", "closing", "awaiting_payment", ""):
        assert d.teardown_check({**ok, "status": status}, "start")
    why = d.teardown_check({**ok, "status": "closed"}, "start")
    assert "closure/close.py" in why and "provisioning" in why, "a closed row is reset by hand, and the refusal says how"
    assert d.teardown_check(None, "start")


def test_the_two_verbs_are_the_only_ones():
    d = _deploy()
    assert d.teardown_check({"gerp_id": "x", "status": "active", "aws_account_id": "1"}, "destroy")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all deploy-teardown tests passed")
