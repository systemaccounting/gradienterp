"""`apply.sh --stack per_customer`: the refusal before a gerp's build starts.

A build against the wrong row is the failure that matters here. An apply into a `closed` row
stands a stack up under a `closure/close.py` schedule that closes the account on day 15; `stop` on
the seller's own gerp destroys the closure scripts, the hub's trust and the seller's books. The
check is one function over the row, so it runs here with rows and no AWS.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _apply():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("apply", REPO / "scripts" / "apply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stop_takes_an_active_gerp_that_is_not_the_sellers():
    a = _apply()
    ok = {"gerp_id": "westwood-c40fd8", "status": "active", "aws_account_id": "222165865776"}
    assert a.teardown_check(ok, "stop") == ""
    for status in ("provisioning", "stopped", "closing", "closed", "awaiting_payment", ""):
        why = a.teardown_check({**ok, "status": status}, "stop")
        assert why and (status or "unset") in why, f"stop on a {status or 'unset'} row must be refused by name"
    why = a.teardown_check({**ok, "gerp_id": a.SELLER_GERP}, "stop")
    assert why and a.SELLER_GERP in why and "seller" in why
    assert a.teardown_check(None, "stop")
    assert a.teardown_check({**ok, "aws_account_id": ""}, "stop")


def test_an_apply_takes_an_active_stopped_or_provisioning_gerp_and_never_a_closure():
    a = _apply()
    ok = {"gerp_id": "westwood-c40fd8", "status": "stopped", "aws_account_id": "222165865776"}
    for status in ("stopped", "active", "provisioning"):
        for action in ("apply", "plan"):
            assert a.teardown_check({**ok, "status": status}, action) == "", (status, action)
    for status in ("closing", "closed"):
        for action in ("apply", "plan", "stop"):
            why = a.teardown_check({**ok, "status": status}, action)
            assert why and "closure" in why, (status, action)
    why = a.teardown_check({**ok, "status": "closed"}, "apply")
    assert "closure/close.py" in why and "provisioning" in why, "a closed row is reset by hand, and the refusal says how"
    assert a.teardown_check(None, "apply")
    assert a.teardown_check({**ok, "aws_account_id": ""}, "apply")


def test_apply_plan_and_stop_are_the_only_actions():
    a = _apply()
    assert a.teardown_check({"gerp_id": "x", "status": "active", "aws_account_id": "1"}, "destroy")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all apply-teardown tests passed")
