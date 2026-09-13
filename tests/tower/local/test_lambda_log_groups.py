"""A lambda's log group is declared once — by the shared lambda module, and nowhere else.

`modules/terraform/lambda` creates `/aws/lambda/<name>` for every function it deploys. A module
that also declares that name itself (to hang a subscription filter on, say) works on a gerp whose
state already carries both entries for the one group, and fails the first apply on a fresh gerp:
the second CreateLogGroup is ResourceAlreadyExistsException, and the vend stops there. The Irish
gerp's build hit this on `automate` and `machine_failed`.

A module that needs a function's log group reads the lambda module's `log_group` / `log_group_arn`
outputs. Text check, no terraform.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MODULES = REPO / "modules"
LAMBDA_MODULE = MODULES / "terraform" / "lambda"

LOG_GROUP = re.compile(r'resource\s+"aws_cloudwatch_log_group"\s+"(\w+)"\s*\{(.*?)\n\}', re.S)


def test_only_the_lambda_module_declares_a_lambda_log_group():
    offenders = []
    for tf in MODULES.glob("*/infra/*.tf"):
        for name, body in LOG_GROUP.findall(tf.read_text()):
            if "/aws/lambda/" in body:
                offenders.append(f"{tf.relative_to(REPO)}: aws_cloudwatch_log_group.{name}")
    assert not offenders, "lambda log groups declared outside modules/terraform/lambda:\n  " + "\n  ".join(offenders)


def test_the_lambda_module_exports_its_log_group():
    outputs = (LAMBDA_MODULE / "outputs.tf").read_text()
    assert re.search(r'output\s+"log_group"', outputs)
    assert re.search(r'output\s+"log_group_arn"', outputs)


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all lambda log group tests passed")
