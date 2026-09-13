"""The per-customer buildspec writes the operator's table and topic in the operator's region.

The build exports the gerp's region as AWS_REGION so terraform and the calls into the gerp's
account land there. The operator's `gerp-customers` table and ops topic stay in the operator's
region, and a call that inherits AWS_REGION reaches for a table that is not there: the Irish
gerp's first full apply ended with `dynamodb:UpdateItem on …:eu-west-1:…:table/gerp-customers`
refused, after every resource was up. Each such call names `--region "${OPERATOR_REGION}"`,
kept from the build's own region before the override. Text check, no build.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILDSPEC = REPO / ".codebuild" / "per-customer.yml"


def test_operator_region_is_kept_before_the_override():
    body = BUILDSPEC.read_text()
    keep = body.index('export OPERATOR_REGION="${AWS_REGION}"')
    override = body.index('export AWS_REGION="${CUSTOMER_REGION}"')
    assert keep < override, "OPERATOR_REGION is read from AWS_REGION before the gerp's region replaces it"


def test_every_operator_side_call_names_the_operator_region():
    body = BUILDSPEC.read_text()
    calls = [m.group(0) for m in re.finditer(
        r'aws (dynamodb update-item|sns publish)[^\n]*(?:\\\n[^\n]*)*', body)]
    operator_side = [c for c in calls if "CUSTOMERS_TABLE" in c or "OPS_ALERTS_TOPIC_ARN" in c]
    assert operator_side, "the buildspec writes the customers table and the ops topic"
    bare = [c.split("\n")[0] for c in operator_side if '--region "${OPERATOR_REGION}"' not in c]
    assert not bare, "operator-side calls inheriting the gerp's region:\n  " + "\n  ".join(bare)


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all buildspec region tests passed")
