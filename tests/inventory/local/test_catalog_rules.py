"""Local tests for the catalog rules — where an item's revenue lands.

The decision lives in a RULE, not in create_item, so it is a named surface the agent can find and
re-point (`rule_params` op set) instead of logic buried in a lambda. But the rule fires at CREATE and
its answer is STAMPED on the item row — so the posting path never infers anything, and the ledger
never depends on a guess made somewhere else.

Asserts: the default falls out of the item's own shape (capacity sells time → a service), an
explicit value beats the rule, and an owner's param row beats the code default.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, load_tool, scratch_env

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))       # the engine
sys.path.insert(0, str(REPO_ROOT / "modules" / "inventory"))   # catalog_rules


def _invoke(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _write_instance(out_dir, rule, param):
    """What `manage_rules` (op: add) writes: a rule instance matching `ITEM_CREATED#*` — the firm's own catalog rule.
    It REPLACES the canonical one, so the firm's accounts win. scratch_env gives each test its own
    instance table, so the row can't leak into another."""
    from aws import table as _t
    _t(os.environ["RULE_INSTANCES_TABLE"]).put_item(Item={
        "pk": "ITEM_CREATED#*", "sk": f"0100#{rule}", "n": 100,
        "name": rule, "rule": rule, "param": param,
    })


def test_stock_item_defaults_to_sales_revenue():
    with scratch_env() as (out_dir, _logs):
        create = load_tool("create_item")
        code, body = _invoke(create, {"item_id": "1#beans", "name": "Beans", "unit_cost": 12.5})
        assert code == 200, body
        assert body["item"]["revenue_account"] == "SALES_REVENUE"   # a counted good sells goods


def test_capacity_item_defaults_to_service_revenue():
    # the default falls out of the item's OWN shape — an availability_rule means it sells time
    with scratch_env() as (out_dir, _logs):
        create = load_tool("create_item")
        code, body = _invoke(create, {
            "item_id": "1#room_101", "name": "Room 101", "unit_cost": 40, "unit_price": 180,
            "availability_rule": "FREQ=DAILY", "availability_duration": 86400,
        })
        assert code == 200, body
        assert body["item"]["revenue_account"] == "SERVICE_REVENUE"


def test_explicit_value_beats_the_rule():
    with scratch_env() as (out_dir, _logs):
        create = load_tool("create_item")
        code, body = _invoke(create, {
            "item_id": "1#tips", "name": "Tips", "unit_cost": 0,
            "revenue_account": "OTHER_INCOME",
        })
        assert code == 200, body
        assert body["item"]["revenue_account"] == "OTHER_INCOME"


def test_the_firms_own_instance_beats_the_canonical_one():
    # the whole point of putting it in a rule: the firm re-points it by writing a row, no deploy
    with scratch_env() as (out_dir, _logs):
        _write_instance(out_dir, "revenue_account", {"capacity": "OTHER_INCOME"})
        create = load_tool("create_item")   # loaded AFTER the row exists
        code, body = _invoke(create, {
            "item_id": "1#chair_1", "name": "Chair 1", "unit_cost": 0, "unit_price": 60,
            "availability_rule": "FREQ=DAILY", "availability_duration": 3600,
        })
        assert code == 200, body
        assert body["item"]["revenue_account"] == "OTHER_INCOME"   # not SERVICE_REVENUE

        # …and the un-repointed half still falls back to its code default
        code, body = _invoke(create, {"item_id": "1#mug", "name": "Mug", "unit_cost": 3})
        assert body["item"]["revenue_account"] == "SALES_REVENUE"


def test_the_account_is_on_the_row_not_inferred_later():
    # the posting path must never have to guess — the item SAYS where its revenue goes
    with scratch_env() as (out_dir, _logs):
        create, get = load_tool("create_item"), load_tool("get_stock")
        _invoke(create, {"item_id": "1#beans", "name": "Beans", "unit_cost": 12.5})
        code, body = _invoke(get, {"item_id": "1#beans"})
        assert body["items"][0]["revenue_account"] == "SALES_REVENUE"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all catalog rule tests passed")
