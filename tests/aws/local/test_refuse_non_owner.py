"""The owner routes' check (modules/aws/aws.py `refuse_non_owner`). A gerp's HTTP API authorizer
admits any account in the operator's pool; the routes that act as the owner answer only the sub in
the gerp's `owner_sub` parameter. A direct invoke or an IAM-signed request carries no claims and is
not refused. Each owner route calls it before anything else."""

import json
import os
import re
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "aws"))
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
import aws  # noqa: E402


def _event(sub=None):
    claims = {"sub": sub, "aud": "gerp-cloud"} if sub is not None else {"aud": "gerp-cloud"}
    return {"requestContext": {"http": {"method": "PUT"}, "authorizer": {"jwt": {"claims": claims}}}}


def _param(value):
    name = f"/gradienterp/customers/t-{uuid.uuid4().hex[:8]}/owner_sub"
    aws.client("ssm").put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
    return name


def _with_param(name, fn):
    old = os.environ.get("OWNER_SUB_PARAM")
    os.environ["OWNER_SUB_PARAM"] = name
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("OWNER_SUB_PARAM", None)
        else:
            os.environ["OWNER_SUB_PARAM"] = old


def test_a_request_without_claims_passes():
    assert aws.refuse_non_owner({}) is None
    assert aws.refuse_non_owner({"requestContext": {"http": {"method": "GET"}}}) is None
    assert aws.refuse_non_owner({"body": "{}"}) is None


def test_the_owner_passes_and_anyone_else_is_refused():
    name = _param("sub-owner")
    assert _with_param(name, lambda: aws.refuse_non_owner(_event("sub-owner"))) is None
    other = _with_param(name, lambda: aws.refuse_non_owner(_event("sub-stranger")))
    assert other["statusCode"] == 403 and "owner" in json.loads(other["body"])["error"]
    assert _with_param(name, lambda: aws.refuse_non_owner(_event()))["statusCode"] == 403


def test_a_parameter_that_cannot_be_read_refuses():
    missing = f"/gradienterp/customers/t-{uuid.uuid4().hex[:8]}/owner_sub"
    assert _with_param(missing, lambda: aws.refuse_non_owner(_event("sub-owner")))["statusCode"] == 403


def test_no_parameter_refuses_in_lambda_and_passes_off_it():
    os.environ.pop("OWNER_SUB_PARAM", None)
    assert aws.refuse_non_owner(_event("anyone")) is None
    was = aws.IN_LAMBDA
    aws.IN_LAMBDA = True
    try:
        assert aws.refuse_non_owner(_event("anyone"))["statusCode"] == 403
    finally:
        aws.IN_LAMBDA = was


def test_each_owner_route_checks_before_anything_else():
    for path in ("modules/settings/lambdas/tenant_settings/main.py",
                 "modules/invoicing/lambdas/manage_invoice/main.py",
                 "modules/automation/lambdas/automate/main.py"):
        src = (REPO / path).read_text()
        body = src[src.index("\ndef handler(event, context):"):]
        body = re.sub(r'^\ndef handler\(event, context\):\n(    """[\s\S]*?"""\n)?', "", body)
        first = [l for l in body.splitlines() if l.strip()][:3]
        assert first[0].strip().startswith("refused = refuse_non_owner(event)"), path
        assert first[1].strip() == "if refused:" and first[2].strip() == "return refused", path


def test_the_owner_routes_name_the_parameter_and_may_read_only_it():
    owner = "/gradienterp/customers/${var.gerp_id}/owner_sub"
    for path in ("modules/settings/infra/main.tf", "modules/invoicing/infra/main.tf", "modules/automation/infra/main.tf"):
        tf = (REPO / path).read_text()
        assert f'OWNER_SUB_PARAM' in tf and owner in tf, path
        block = tf[tf.index('resource "aws_iam_role_policy" "owner_sub"'):]
        assert '"ssm:GetParameter"' in block and "parameter/gradienterp/customers/${var.gerp_id}/owner_sub\"" in block, path


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all owner route tests passed")
