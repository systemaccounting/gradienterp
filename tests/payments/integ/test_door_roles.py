"""Integration test — each webhook door runs as its own role, against DEPLOYED gerps.

`ingest_stripe`, `ingest_square` and `ingest_paypal` answer anyone on the internet, so each holds
only what its door reads and calls. The IAM policy simulator holds each role to that: its own
parameters, dedup table and invokes allowed; a write to a parameter, another processor's secret, the
owner's Stripe key and a contacts write denied. Then an unsigned POST to each live door has to be
refused as unsigned (400) or unconfigured (401): a read the role lacks would surface as a 500.

Run: `bash scripts/test.sh --env integ --module payments` (or this file). The gerps default to the
three live ones (PAYMENTS_INTEG_GERPS, comma-separated); a gerp whose profile doesn't resolve is
skipped. Reads and simulates only; the POSTs write nothing.
"""

import json
import os
import urllib.error
import urllib.request

GERPS = os.environ.get("PAYMENTS_INTEG_GERPS", "gradienterp,westwood-c40fd8,dublin-test-roasters-d542eb").split(",")

DOORS = {
    "ingest_stripe": {"provider": "stripe", "params": ["stripe/signing_secret", "stripe/signing_secret_previous"],
                      "not_its": "square/signing_secret"},
    "ingest_square": {"provider": "square", "params": ["square/signing_secret", "square/signing_secret_previous"],
                      "not_its": "stripe/signing_secret"},
    "ingest_paypal": {"provider": "paypal", "params": ["paypal/webhook_id", "paypal_client_id", "paypal_secret"],
                      "not_its": "stripe/signing_secret"},
}

_rows = {}


def _row(gerp):
    if gerp not in _rows:
        import boto3
        item = boto3.Session(profile_name="operator-org", region_name="us-east-1").client("dynamodb").get_item(
            TableName="gerp-customers", Key={"gerp_id": {"S": gerp}}).get("Item") or {}
        _rows[gerp] = {k: next(iter(v.values())) for k, v in item.items()}
    return _rows[gerp]


def _session(gerp):
    import boto3
    return boto3.Session(profile_name=f"gerp-{gerp}", region_name=_row(gerp).get("region") or "us-east-1")


def _reachable(gerp):
    try:
        _session(gerp).client("sts").get_caller_identity()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"skip {gerp}: gerp-{gerp} does not resolve ({e})")
        return False


def _decisions(iam, role_arn, region, action, resources):
    r = iam.simulate_principal_policy(
        PolicySourceArn=role_arn, ActionNames=[action], ResourceArns=resources,
        # the org's region guard is conditioned on the request's region
        ContextEntries=[{"ContextKeyName": "aws:RequestedRegion", "ContextKeyValues": [region],
                         "ContextKeyType": "string"}])
    out = {}
    for res in r["EvaluationResults"]:
        for rr in res.get("ResourceSpecificResults") or [{"EvalResourceName": res["EvalResourceName"],
                                                          "EvalResourceDecision": res["EvalDecision"]}]:
            out[rr["EvalResourceName"]] = rr["EvalResourceDecision"]
    return out


def test_each_door_runs_as_its_own_role_holding_only_its_own():
    for gerp in GERPS:
        if not _reachable(gerp):
            continue
        ses, region = _session(gerp), _row(gerp).get("region") or "us-east-1"
        account = ses.client("sts").get_caller_identity()["Account"]
        lam, iam = ses.client("lambda"), ses.client("iam")
        slug = gerp.replace("_", "-")
        secrets = f"arn:aws:ssm:{region}:{account}:parameter/gradienterp/customers/{gerp}/secrets"
        fn_arn = lambda name: f"arn:aws:lambda:{region}:{account}:function:{name}"  # noqa: E731
        for door, d in DOORS.items():
            cfg = lam.get_function_configuration(FunctionName=f"gerp-payments-{slug}-{door}")
            env = cfg["Environment"]["Variables"]
            role_arn = cfg["Role"]
            assert role_arn.endswith(f":role/gerp-payments-{slug}-{door}"), (gerp, door, role_arn)
            where = f"{gerp} {door}"

            allowed = {
                "ssm:GetParameter": [f"{secrets}/{p}" for p in d["params"]],
                "lambda:InvokeFunction": [fn_arn(env["POST_JOURNAL_ENTRY_FN"])],
                "dynamodb:PutItem": [f"arn:aws:dynamodb:{region}:{account}:table/{env['WEBHOOK_LOG_TABLE']}"],
            }
            for action, resources in allowed.items():
                for res, decision in _decisions(iam, role_arn, region, action, resources).items():
                    assert decision == "allowed", (where, action, res, decision)

            denied = {
                "ssm:PutParameter": [f"{secrets}/{d['params'][0]}"],
                "ssm:GetParameter": [f"{secrets}/{d['not_its']}", f"{secrets}/stripe_billing"],
                "lambda:InvokeFunction": [fn_arn(env["CONTACTS_PUT_FN"])],
            }
            for action, resources in denied.items():
                for res, decision in _decisions(iam, role_arn, region, action, resources).items():
                    assert decision in ("implicitDeny", "explicitDeny"), (where, action, res, decision)
            print(f"  {where}: {cfg['Role'].split('/')[-1]}")


def test_an_unsigned_post_to_each_live_door_is_refused_not_an_error():
    for gerp in GERPS:
        if not _reachable(gerp):
            continue
        lam = _session(gerp).client("lambda")
        slug = gerp.replace("_", "-")
        for door, d in DOORS.items():
            base = lam.get_function_configuration(
                FunctionName=f"gerp-payments-{slug}-{door}")["Environment"]["Variables"]["WEBHOOK_BASE_URL"]
            # an id and a type, so a door that parses before it verifies (PayPal) reaches its reads
            body = json.dumps({"id": "evt_integ_unsigned", "event_id": "evt_integ_unsigned",
                               "type": "charge.succeeded", "event_type": "PAYMENT.CAPTURE.COMPLETED"}).encode()
            req = urllib.request.Request(f"{base}/webhooks/{d['provider']}", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    status, body = r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read().decode()
            assert status in (400, 401), (gerp, door, status, body)
            print(f"  {gerp} {door}: {status} {json.loads(body or '{}').get('error')}")


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all payments door role integ tests passed")
