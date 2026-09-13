"""A vend or a closure that fails reaches a person: every tower function has an Errors alarm on
the ops topic, the build rule names the project and the three failing statuses and not SUCCEEDED,
the message carries the build's environment (CUSTOMER_ID, TF_ACTION), the async functions
publish their failure record, and the vend is never retried by Lambda."""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOWER = REPO / "prod" / "tower"
ALERTS = (TOWER / "alerts.tf").read_text()


def test_a_raise_anywhere_in_the_account_is_one_alarm_on_the_topic():
    """`AWS/Lambda Errors` with no dimension is the account's sum, so one alarm covers every
    function in the operator account (tower's seven and the other stacks'); the log group names
    the function. No per-function alarm exists, in tower or in the lambda module."""
    alarm = ALERTS[ALERTS.index('resource "aws_cloudwatch_metric_alarm" "operator_errors"'):]
    alarm = alarm[:alarm.index("\n}\n")]
    assert 'namespace           = "AWS/Lambda"' in alarm and 'metric_name         = "Errors"' in alarm
    assert "dimensions" not in alarm, "the account sum has no dimension"
    assert 'treat_missing_data  = "notBreaching"' in alarm
    assert "alarm_actions       = [aws_sns_topic.ops_alerts.arn]" in alarm
    assert "ok_actions          = [aws_sns_topic.ops_alerts.arn]" in alarm
    assert 'resource "aws_cloudwatch_metric_alarm" "lambda_errors"' not in ALERTS
    module = (REPO / "modules" / "terraform" / "lambda" / "main.tf").read_text()
    assert 'aws_cloudwatch_metric_alarm' not in module, "the lambda module owns no alarm"
    per_customer = (REPO / "prod" / "per_customer" / "main.tf").read_text()
    assert 'resource "aws_cloudwatch_metric_alarm" "errors"' in per_customer
    assert 'resource "aws_cloudwatch_metric_alarm" "error_lines"' in per_customer


def test_the_build_rule_matches_the_three_failing_statuses_and_carries_the_environment():
    rule = ALERTS[ALERTS.index('resource "aws_cloudwatch_event_rule" "build_failed"'):]
    rule = rule[:rule.index("\n}\n")]
    assert '"CodeBuild Build State Change"' in rule
    assert "aws_codebuild_project.per_customer.name" in rule
    statuses = re.search(r'"build-status"\s*=\s*\[([^\]]+)\]', rule).group(1)
    assert set(re.findall(r'"(\w+)"', statuses)) == {"FAILED", "STOPPED", "TIMED_OUT"}
    target = ALERTS[ALERTS.index('resource "aws_cloudwatch_event_target" "build_failed"'):]
    target = target[:target.index("\n}\n")]
    assert "environment.environment-variables" in target, "the message must carry CUSTOMER_ID and TF_ACTION"
    assert "logs.deep-link" in target
    assert "arn       = aws_sns_topic.ops_alerts.arn" in target


def test_the_vend_is_never_retried_and_the_async_functions_publish_their_failure():
    cfg = ALERTS[ALERTS.index('resource "aws_lambda_function_event_invoke_config" "provision_customer"'):]
    cfg = cfg[:cfg.index("\n}\n")]
    assert "maximum_retry_attempts = 0" in cfg, "a retried vend calls ProvisionProduct twice for one purchase"
    assert "destination = aws_sns_topic.ops_alerts.arn" in cfg
    bill = ALERTS[ALERTS.index('resource "aws_lambda_function_event_invoke_config" "bill_customer"'):]
    bill = bill[:bill.index("\n}\n")]
    assert "maximum_retry_attempts" not in bill, "a bill may be retried"
    assert "destination = aws_sns_topic.ops_alerts.arn" in bill
    # the roles that publish the record
    pol = ALERTS[ALERTS.index('resource "aws_iam_role_policy" "ops_alerts_destination"'):]
    for role in ("provision_customer", "bill_customer"):
        assert f"{role}" in pol[:pol.index("policy =")]


def test_the_topic_has_one_email_subscriber_and_the_publishers_it_admits():
    assert 'protocol  = "email"' in ALERTS and "endpoint  = var.ops_alerts_email" in ALERTS
    policy = ALERTS[ALERTS.index('resource "aws_sns_topic_policy" "ops_alerts"'):]
    policy = policy[:policy.index("\n}\n")]
    assert '"events.amazonaws.com"' in policy and '"cloudwatch.amazonaws.com"' in policy


def test_the_vends_queue_runs_four_at_a_time_and_parks_the_third_failure():
    """Control Tower runs five account operations at once. The mapping caps the provisioner at
    four vends so the fifth signup waits on the queue with one slot left for a hand-run; the
    visibility timeout is six times the 900 s wall; the third failure parks the message, and
    the parked queue alarms on the ops topic. The BFF sends to the queue, not the function."""
    vends = (TOWER / "vends.tf").read_text()
    mapping = vends[vends.index('resource "aws_lambda_event_source_mapping" "vends"'):]
    mapping = mapping[:mapping.index("\n}\n")]
    assert "maximum_concurrency = 4" in mapping, "one Control Tower slot stays free for a hand-run"
    assert "function_name           = module.provision_customer.arn" in mapping
    queue = vends[vends.index('resource "aws_sqs_queue" "vends" {'):]
    queue = queue[:queue.index("\n}\n")]
    assert "visibility_timeout_seconds = 5400" in queue, "six times the provisioner's 900 s wall"
    assert "maxReceiveCount     = 3" in queue and "aws_sqs_queue.vends_failed.arn" in queue
    parked = vends[vends.index('resource "aws_cloudwatch_metric_alarm" "vends_parked"'):]
    assert "QueueName = aws_sqs_queue.vends_failed.name" in parked
    assert "alarm_actions       = [aws_sns_topic.ops_alerts.arn]" in parked
    bff = (REPO / "prod" / "gradienterp_cloud" / "main.tf").read_text()
    assert '"sqs:SendMessage"' in bff and ":tower-vends" in bff
    assert "function:tower-provision-customer" not in bff, "the card sends to the queue; nothing invokes the provisioner"


def test_the_two_capacity_alarms_fire_at_80_percent_to_the_topic():
    """Organizations publishes no usage metric: bill_customer publishes the org's accounts
    against quota L-E619E033 and the customers OU's against Control Tower's 1,000, as
    percentages on gerp/platform; each has an alarm at 80 on the ops topic, and the
    descriptions say what a person does. The function's role may publish that namespace only."""
    for label, metric, code in (("org_accounts_80pct", "OrgAccountsUsedPercent", "L-E619E033"),
                                ("customers_ou_80pct", "CustomersOuUsedPercent", "1,000")):
        alarm = ALERTS[ALERTS.index(f'resource "aws_cloudwatch_metric_alarm" "{label}"'):]
        alarm = alarm[:alarm.index("\n}\n")]
        assert 'namespace           = "gerp/platform"' in alarm and f'metric_name         = "{metric}"' in alarm
        assert 'statistic           = "Maximum"' in alarm and "period              = 86400" in alarm
        assert "threshold           = 80\n" in alarm and 'comparison_operator = "GreaterThanOrEqualToThreshold"' in alarm
        assert 'treat_missing_data  = "notBreaching"' in alarm
        assert "alarm_actions       = [aws_sns_topic.ops_alerts.arn]" in alarm
        assert "ok_actions          = [aws_sns_topic.ops_alerts.arn]" in alarm
        assert code in alarm, f"the description names the cap: {code}"
    main = (TOWER / "main.tf").read_text()
    policy = main[main.index('resource "aws_iam_role_policy" "bill_customer"'):]
    policy = policy[:policy.index("\n}\n")]
    assert '"cloudwatch:PutMetricData"' in policy and '"cloudwatch:namespace" = "gerp/platform"' in policy
    assert "CUSTOMERS_OU_ID         = data.terraform_remote_state.management.outputs.customers_ou_id" in main
    mgmt = (REPO / "prod" / "platform" / "management" / "tower_provisioning_role.tf").read_text()
    for action in ('"servicequotas:GetServiceQuota"', '"organizations:ListAccounts"', '"organizations:ListAccountsForParent"'):
        assert action in mgmt, action


def test_every_api_stage_logs_why_and_alarms_on_its_own_5xx():
    """Three stages sit in front of functions: the gerp's server (HTTP v2, modules/server), the
    owner app's BFF (HTTP v2), the read api (REST v1). Each writes a JSON access log with the
    fields that explain a 502 or a 401 the function never saw, at the platform's retention, and
    each has a 5xx alarm on the ops topic — the gateway's own failures, which no lambda log
    shows; the collector's threshold reader files them."""
    stages = {
        "modules/server/infra/main.tf": ("5xx", "var.ops_alerts_topic_arn", "var.log_retention_days"),
        "prod/gradienterp_cloud/main.tf": ("5xx", "local.config.OPS_ALERTS_TOPIC_ARN", "local.config.LOG_RETENTION_DAYS"),
        "prod/api_openlyoperated/api.tf": ("5XXError", "local.config.OPS_ALERTS_TOPIC_ARN", "local.config.LOG_RETENTION_DAYS"),
    }
    for path, (metric, topic, retention) in stages.items():
        text = (REPO / path).read_text()
        log = text[text.index("access_log_settings {"):]
        log = log[:log.index("\n  }\n")]
        for field in ("$context.integration.status", "$context.integrationErrorMessage", "$context.error.message", "$context.status"):
            assert field in log, f"{path}: the access log lacks {field}"
        assert f"retention_in_days = {retention}" in text, f"{path}: the access log group is not at the platform's retention"
        alarm = text[text.index('resource "aws_cloudwatch_metric_alarm" "gateway_5xx"'):]
        alarm = alarm[:alarm.index("\n}\n")]
        assert f'metric_name         = "{metric}"' in alarm and 'namespace           = "AWS/ApiGateway"' in alarm, path
        assert f"alarm_actions       = [{topic}]" in alarm and f"ok_actions          = [{topic}]" in alarm, path
        assert 'treat_missing_data  = "notBreaching"' in alarm, path
    assert "$context.authorizer.error" in (REPO / "modules/server/infra/main.tf").read_text(), "the JWT stage says why an authorizer refused"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all alerts tests passed")
