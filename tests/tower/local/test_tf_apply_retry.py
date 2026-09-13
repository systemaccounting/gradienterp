"""`tf_apply` in the buildspec, run: one retry on an IAM race, loud, and nothing else retried.

The function is lifted out of `.codebuild/per-customer.yml` and run under bash with `terraform`,
`aws` and `sleep` replaced by functions that record their calls and answer from a scenario, so
what is checked is the bash that CodeBuild runs, not a description of it. A resource that tests
its role at create can lose the race with IAM on a fresh account (`CreateBrowser`, westwood's
re-apply, 2026-09-06); the second apply picks up from state. The retry is one, it mails ops the
resource's address, and an unrelated failure or a failed plan gets no retry at all.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
BUILDSPEC = REPO / ".codebuild" / "per-customer.yml"

RACE = """Error: creating Bedrock AgentCore Browser

  with module.agent.aws_bedrockagentcore_browser.this,
  on ../../modules/agent/infra/browser.tf line 46, in resource "aws_bedrockagentcore_browser" "this":

ValidationException: The execution role 'arn:aws:iam::1:role/x' does not have permission to write to S3 location 's3://b/p/'
"""
OTHER = """Error: creating Lambda Function

  with module.export.aws_lambda_function.fn,
  on ../../modules/export/infra/main.tf line 10, in resource "aws_lambda_function" "fn":

InvalidParameterValueException: The runtime parameter of python2.7 is no longer supported
"""
CLEAN = "Apply complete! Resources: 3 added, 0 changed, 0 destroyed.\n"


def _functions():
    """The RETRY_CAUSES line and the two functions, as the build phase's last command holds them."""
    cmd = yaml.safe_load(BUILDSPEC.read_text())["phases"]["build"]["commands"][-1]
    start = cmd.index("RETRY_CAUSES=")
    end = cmd.index('if [ "${TF_ACTION}" = "apply" ]')
    return cmd[start:end]


def _run(applies, plan_ok=True):
    """Run tf_apply with `applies` as the successive outputs terraform apply gives. Returns
    (exit code, stdout, number of applies, number of plans, the published messages)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for i, out in enumerate(applies):
            (d / f"apply.{i}").write_text(out)
        script = f"""
set +e
cd {tmp}
export CUSTOMER_ID=cafe-1a2b3c OPS_ALERTS_TOPIC_ARN=arn:aws:sns:us-east-1:1:ops CODEBUILD_BUILD_ID=b:1 AWS_REGION=us-east-1 TFVARS=""
n=0
terraform() {{
  case "$1" in
    plan)  echo plan >> {tmp}/calls; {"echo 'Plan: 3 to add, 0 to change, 0 to destroy.'; return 0" if plan_ok else "echo 'Error: Invalid count argument'; return 1"} ;;
    apply) echo apply >> {tmp}/calls; n=$((n+1)); cat {tmp}/apply.$((n-1)); grep -q '^Apply complete' {tmp}/apply.$((n-1)) ;;
  esac
}}
aws() {{ echo "$*" >> {tmp}/published; }}
sleep() {{ echo "sleep $1" >> {tmp}/calls; }}
{_functions()}
tf_apply "per_customer"
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        calls = (d / "calls").read_text().split() if (d / "calls").exists() else []
        published = (d / "published").read_text() if (d / "published").exists() else ""
        return r.returncode, r.stdout, calls.count("apply"), calls.count("plan"), published


def test_a_clean_apply_runs_once_and_says_nothing():
    rc, out, applies, plans, published = _run([CLEAN])
    assert rc == 0 and applies == 1 and plans == 1
    assert "apply.retry" not in out and published == ""


def test_an_iam_race_is_applied_once_more_and_mailed_with_the_resource():
    rc, out, applies, plans, published = _run([RACE, CLEAN])
    assert rc == 0 and applies == 2 and plans == 2, out
    [line] = [l for l in out.splitlines() if l.startswith("==> apply.retry")]
    assert "cause=does not have permission" in line
    assert "resource=module.agent.aws_bedrockagentcore_browser.this" in line
    assert "sns publish" in published and "recovered after retry: cafe-1a2b3c" in published
    assert "module.agent.aws_bedrockagentcore_browser.this" in published and "does not have permission" in published
    assert "==> terraform apply: per_customer (retry)" in out


def test_the_retry_is_one():
    rc, out, applies, plans, published = _run([RACE, RACE])
    assert rc != 0 and applies == 2 and plans == 2
    assert out.count("==> apply.retry") == 1 and published.count("sns publish") == 1


def test_an_unrelated_failure_is_not_retried():
    rc, out, applies, plans, published = _run([OTHER, CLEAN])
    assert rc != 0 and applies == 1 and "apply.retry" not in out and published == ""


def test_a_failed_plan_is_not_retried():
    rc, out, applies, plans, published = _run([CLEAN], plan_ok=False)
    assert rc != 0 and applies == 0 and plans == 1 and published == ""


def test_the_destroy_and_stop_paths_have_no_retry():
    cmd = yaml.safe_load(BUILDSPEC.read_text())["phases"]["build"]["commands"][-1]
    branch = cmd[cmd.index('echo "==> terraform destroy"'):]
    assert "tf_apply" not in branch and "RETRY" not in branch and "sns publish" not in branch
    assert not re.search(r"sns publish", cmd[:cmd.index("tf_apply() {")]), "the publish is inside the retry only"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all tf_apply retry tests passed")
