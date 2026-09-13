"""scripts/awsacct.py writes the profiles that reach each account from the records that name them,
and owns only its own blocks of the AWS config file: every other line stays as it was, a run that
changes nothing leaves the bytes alone, and `--all` removes a gerp that no longer has an account.
`deploy.py`'s guard refuses a push or status through a profile answering from another account."""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import awsacct  # noqa: E402
import deploy  # noqa: E402

CONFIG = json.loads((REPO / "config.json").read_text())

HAND_WRITTEN = """[default]
region = us-east-1
cli_pager=
preview =
    cloudfront = true
# was us-west-1
[profile customer-gradienterp]
sso_session = gradienterp-sso
region = us-east-1

[profile hub-us-east-1-via-org]
role_arn = arn:aws:iam::582129522725:role/OperatorOrchestration
source_profile = operator-org
"""


def _rows():
    return [{"gerp_id": "gradienterp", "account": "867637277314", "region": "us-east-1", "status": "active"},
            {"gerp_id": "dublin-test-roasters-d542eb", "account": "832348493159", "region": "eu-west-1",
             "status": "active"}]


def _all_blocks(rows):
    blocks = {"operator-org": awsacct.operator_block()}
    for region in CONFIG["HUBS"]:
        name, fields, _ = awsacct.resolve(f"hub:{region}")
        blocks[name] = fields
    for r in rows:
        name, fields, _ = awsacct.resolve(r["gerp_id"], rows)
        blocks[name] = fields
    return blocks


def test_each_target_resolves_to_its_account_role_and_region():
    name, fields, account = awsacct.resolve("dublin-test-roasters-d542eb", _rows())
    assert (name, account) == ("gerp-dublin-test-roasters-d542eb", "832348493159")
    assert dict(fields) == {"role_arn": "arn:aws:iam::832348493159:role/OperatorOrchestration",
                            "source_profile": "operator-org", "region": "eu-west-1", "output": "json"}
    name, fields, account = awsacct.resolve("operator")
    assert account == CONFIG["OPERATOR_ACCOUNT_ID"]
    assert dict(fields)["role_arn"].endswith(":role/OrganizationAccountAccessRole")
    assert dict(fields)["source_profile"] == "default"
    hub = CONFIG["HUBS"]["eu-west-1"]
    assert awsacct.resolve("hub:eu-west-1")[2] == hub["account"]
    assert "credential_process" in dict(awsacct.resolve("management")[1])


def test_the_blocks_it_does_not_own_keep_every_byte():
    after = awsacct.merge(HAND_WRITTEN, _all_blocks(_rows()), prune=True)
    assert after.startswith(HAND_WRITTEN), "the hand-written lines, comments and indentation stay first and whole"
    assert "[profile hub-us-east-1-via-org]" in after, "a profile outside its names is left alone"
    assert "[profile gerp-gradienterp]" in after and "[profile hub-eu-west-1]" in after


def test_a_second_run_changes_nothing():
    blocks = _all_blocks(_rows())
    once = awsacct.merge(HAND_WRITTEN, blocks, prune=True)
    assert awsacct.merge(once, blocks, prune=True) == once


def test_a_block_is_rewritten_where_it_stands():
    stale = HAND_WRITTEN + "\n[profile gerp-gradienterp]\nrole_arn = arn:aws:iam::111111111111:role/X\n\n[profile zed]\nregion = x\n"
    after = awsacct.merge(stale, _all_blocks(_rows()), prune=True)
    assert "111111111111" not in after
    assert after.index("[profile gerp-gradienterp]") < after.index("[profile zed]")


def test_all_removes_a_gerp_that_no_longer_has_an_account_and_keeps_current():
    with_current = awsacct.merge(HAND_WRITTEN, {"current": [("#", "awsacct: gradienterp"),
                                                            *awsacct.resolve("gradienterp", _rows())[1]]})
    before = awsacct.merge(with_current, _all_blocks(_rows()), prune=True)
    after = awsacct.merge(before, _all_blocks(_rows()[:1]), prune=True)
    assert "gerp-dublin-test-roasters-d542eb" not in after
    assert "[profile current]\n# awsacct: gradienterp\n" in after


def test_the_deploy_guard_refuses_another_accounts_profile():
    row = {"gerp_id": "westwood-c40fd8", "aws_account_id": "222165865776"}
    why = deploy.target_refusal("westwood-c40fd8", row, "867637277314")
    assert "867637277314" in why and "222165865776" in why
    assert deploy.target_refusal("westwood-c40fd8", row, "222165865776") == ""
    assert deploy.target_refusal("nope", None, "222165865776")
    assert deploy.target_refusal("stub", {"aws_account_id": "None"}, "222165865776")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all awsacct tests passed")
