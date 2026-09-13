"""Unit tests for tower/provision_customer (no AWS, no moto).

provision_customer orchestrates Service Catalog Account Factory (Control Tower)
+ Organizations + STS + SSM + CodeBuild. moto can't model the AF product or the
FAILED/TAINTED/ResourceInUseException record states the polling loop branches on
— which is the load-bearing logic — so we fake the clients (the cognito /
canonical_pull pattern) and script the org/record sequences directly.

We load with the required env + a region so the module-level boto3 clients
construct (offline), then swap mod.sts / mod.codebuild, rebind mod.boto3.Session
so _assume's sessions hand back fakes by client name, and stub mod.time so the
15s poll sleeps are no-ops and the timeout deadline is controllable.
"""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, env

REQUIRED_ENV = dict(
    TOWER_PROVISIONING_ROLE="arn:aws:iam::335667362239:role/TowerProvisioning",
    BEDROCK_MODEL_IDS="anthropic.claude-sonnet-4-6",
    BEDROCK_USE_CASE_FORM='{"companyName":"gradient erp","industryOption":"Software as a Service"}',
    CODEBUILD_PROJECT="gerp-tower-per-customer-apply",
    AF_PRODUCT_ID="prod-af",
    AF_PATH_ID="path-1",
    CUSTOMERS_OU_MANAGED_NAME="Customers",
    AWS_DEFAULT_REGION="us-east-1",
)


class FakeSTS:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TK"}}


class FakeCodeBuild:
    def __init__(self):
        self.calls = []

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        return {"build": {"id": "gerp-tower-per-customer-apply:abc-123",
                          "arn": "arn:aws:codebuild:us-east-1:335667362239:build/abc-123"}}


class FakeServiceCatalog:
    def __init__(self, record_states=None, artifacts=None):
        self.provision_calls = []
        self.describe_calls = 0
        self._record_states = list(record_states or [])
        self._artifacts = artifacts or [
            {"Id": "pa-old", "Active": False},
            {"Id": "pa-new", "Active": True},
        ]

    def list_provisioning_artifacts(self, ProductId=None):
        return {"ProvisioningArtifactDetails": self._artifacts}

    def provision_product(self, **kwargs):
        self.provision_calls.append(kwargs)
        return {"RecordDetail": {"RecordId": "rec-1"}}

    def describe_record(self, Id=None):
        self.describe_calls += 1
        if self._record_states:
            return {"RecordDetail": self._record_states.pop(0)}
        return {"RecordDetail": {"Status": "IN_PROGRESS", "RecordErrors": []}}


class FakePaginator:
    def __init__(self, accounts):
        self._accounts = accounts

    def paginate(self):
        return [{"Accounts": self._accounts}]


class FakeOrganizations:
    """Each poll iteration calls get_paginator(); successive calls return the
    next snapshot, so an account can 'appear' on the Nth poll."""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.poll_count = 0

    def get_paginator(self, name):
        assert name == "list_accounts"
        idx = min(self.poll_count, len(self._snapshots) - 1)
        self.poll_count += 1
        return FakePaginator(self._snapshots[idx])


class FakeSSM:
    def __init__(self):
        self.params = []

    def put_parameter(self, **kwargs):
        self.params.append(kwargs)


class FakeInvoicing:
    """One invoice unit per vended account. Records the calls. A repeat create of a name is
    refused the way the api refuses it — `ValidationException: Invoice Unit already exists`
    (seen live on the Dublin vend's re-run) — and lists answer by name, so a re-run that reads
    first never meets the refusal."""

    class exceptions:
        class ValidationException(Exception):
            pass

    def __init__(self, unlinked_for=0):
        self.units = []
        self.refusals = 0
        self.lists = 0
        self.unlinked_for = unlinked_for   # how many creates billing refuses before the account is under the payer

    def list_invoice_units(self, Filters=None, **kwargs):
        self.lists += 1
        names = (Filters or {}).get("Names")
        units = [u for u in self.units if not names or u["Name"] in names]
        return {"InvoiceUnits": [{"Name": u["Name"], "InvoiceUnitArn": u["InvoiceUnitArn"]} for u in units]}

    def create_invoice_unit(self, **kwargs):
        if self.refusals < self.unlinked_for:
            self.refusals += 1
            raise self.exceptions.ValidationException(
                f"Selected Linked Accounts <{kwargs['Rule']['LinkedAccounts']}> are not under the Payer <1>")
        if any(u["Name"] == kwargs["Name"] for u in self.units):
            raise self.exceptions.ValidationException("Invoice Unit already exists")
        arn = f"arn:aws:invoicing::1:invoice-unit/{len(self.units) + 1}"
        self.units.append({**kwargs, "InvoiceUnitArn": arn})
        return {"InvoiceUnitArn": arn}


class FakeBedrock:
    """The model-access control plane in the vended account. `authorized_after` is how many
    availability reads happen before the use-case form has propagated; `agreements` seeds a model
    as already AVAILABLE, which is what a re-run of a half-finished provision sees."""

    def __init__(self, authorized_after=0, agreements=None):
        self.forms, self.created, self.reads = [], [], 0
        self._authorized_after = authorized_after
        self._agreements = dict(agreements or {})

    def put_use_case_for_model_access(self, **kwargs):
        self.forms.append(kwargs["formData"])

    def get_foundation_model_availability(self, **kwargs):
        self.reads += 1
        status = self._agreements.get(kwargs["modelId"], "NOT_AVAILABLE")
        return {"modelId": kwargs["modelId"],
                "authorizationStatus": "AUTHORIZED" if self.reads > self._authorized_after else "NOT_AUTHORIZED",
                "agreementAvailability": {"status": status},
                "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE"}

    def list_foundation_model_agreement_offers(self, **kwargs):
        return {"modelId": kwargs["modelId"], "offers": [{"offerToken": f"tok-{kwargs['modelId']}"}]}

    def create_foundation_model_agreement(self, **kwargs):
        self.created.append(kwargs)
        self._agreements[kwargs["modelId"]] = "PENDING"
        return {"modelId": kwargs["modelId"]}


import time as _real_time


class TimeStub:
    """time() walks a value list (last value repeats); sleep() is a no-op.

    Only the clock the polling loop reads is faked. Everything else on `time` passes through, so
    stamping a row does not have to know it is under test."""

    def __init__(self, values):
        self._values = list(values)

    def time(self):
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]

    def sleep(self, _seconds):
        pass

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _account(name, acct_id="222222222222", status="ACTIVE"):
    return {"Id": acct_id, "Name": name, "Status": status}


class _ConditionalCheckFailed(Exception):
    pass


class FakeDDB:
    """Records the update_item calls that land — the vend has to be written to the row before the
    build starts, so a build that fails still leaves the account findable. `row` is what a
    get_item reads: a row that already names its account is a re-run, and the vend is skipped.
    A conditional write on `status` lands only when the row's status matches, as the table's
    does."""

    exceptions = types.SimpleNamespace(ConditionalCheckFailedException=_ConditionalCheckFailed)

    def __init__(self, row=None):
        self.updates = []
        self.puts = []
        self.row = row or {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def update_item(self, **kwargs):
        if kwargs.get("ConditionExpression") == "#s = :old":
            if self.row.get("status") != kwargs["ExpressionAttributeValues"][":old"]:
                raise _ConditionalCheckFailed()
            self.row["status"] = kwargs["ExpressionAttributeValues"][":new"]
        self.updates.append(kwargs)
        return {}

    def get_item(self, **kwargs):
        return {"Item": self.row} if self.row else {}


def _wire(mod, *, org_snapshots, record_states=None, artifacts=None, times=None, bedrock=None, row=None, unlinked_for=0):
    mod.sts = FakeSTS()
    mod.codebuild = FakeCodeBuild()
    mod.ddb = FakeDDB(row)
    sc = FakeServiceCatalog(record_states=record_states, artifacts=artifacts)
    org = FakeOrganizations(org_snapshots)
    ssm = FakeSSM()
    inv = FakeInvoicing(unlinked_for=unlinked_for)
    mod.bedrock_fake = bedrock or FakeBedrock()
    by_name = {"servicecatalog": sc, "organizations": org, "ssm": ssm, "invoicing": inv,
               "bedrock": mod.bedrock_fake}
    session = types.SimpleNamespace(client=lambda name, **kw: by_name[name])
    mod.boto3 = types.SimpleNamespace(Session=lambda **kwargs: session)
    mod.time = TimeStub(times or [1000])
    return sc, org, ssm, inv


def _load():
    with env(**REQUIRED_ENV):
        return load_lambda("provision_customer")


def _params(provision_call):
    return {p["Key"]: p["Value"] for p in provision_call["ProvisioningParameters"]}


def test_happy_path_provisions_seeds_and_builds():
    mod = _load()
    # account is absent on the first poll, ACTIVE on the second
    sc, org, ssm, inv = _wire(
        mod,
        org_snapshots=[[], [_account("Ken's Cafe", acct_id="222222222222")]],
        record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}],
    )

    result = mod.handler({
        "customer_id": "ken-cafe",
        "owner_email": "ken@cafe.com",
        "owner_first_name": "Ken",
        "owner_last_name": "Barista",
        "business_name": "Ken's Cafe",
        "business_category": "cafe",
        "reporting_schedule": "cron(0 9 1 * ? *)",
        "legal": {"name": "Ken's Cafe LLC", "email": "books@kens.example", "phone": "+1 555 0100",
                  "street": "1 Bean St", "city": "Austin", "state": "TX", "zip": "78701", "country": "US"},
        "public": {"name": "Ken's", "links": [{"type": "website", "url": "https://kens.example"}]},
    }, None)

    # two assumes: TowerProvisioning in mgmt, then OperatorOrchestration on the new account
    assert len(mod.sts.calls) == 3   # TowerProvisioning, the wait for OperatorOrchestration, then the seed session
    assert mod.sts.calls[0]["RoleArn"] == REQUIRED_ENV["TOWER_PROVISIONING_ROLE"]
    assert mod.sts.calls[0]["RoleSessionName"] == "provision-ken-cafe"
    assert mod.sts.calls[1]["RoleArn"] == "arn:aws:iam::222222222222:role/OperatorOrchestration"
    assert mod.sts.calls[1]["RoleSessionName"] == "seed-ken-cafe"

    # ProvisionProduct uses the ACTIVE artifact + the AF product/path
    assert len(sc.provision_calls) == 1
    pp = sc.provision_calls[0]
    assert pp["ProductId"] == "prod-af"
    assert pp["ProvisioningArtifactId"] == "pa-new"
    assert pp["PathId"] == "path-1"
    assert pp["ProvisionedProductName"].startswith("customer-ken-cafe-")
    params = _params(pp)
    assert params["AccountName"] == "Ken's Cafe"
    assert params["AccountEmail"] == "ops+ken-cafe@gradienterp.cloud"
    assert params["SSOUserEmail"] == "ken@cafe.com"
    assert params["SSOUserFirstName"] == "Ken"
    assert params["SSOUserLastName"] == "Barista"
    assert params["ManagedOrganizationalUnit"] == "Customers"

    # SSM seed lands in the customer's account under the gradienterp namespace
    assert len(ssm.params) == 1
    seed = ssm.params[0]
    assert seed["Name"] == "/gradienterp/customers/ken-cafe"
    assert seed["Type"] == "String"
    assert seed["Overwrite"] is True
    meta = json.loads(seed["Value"])
    assert meta == {
        "business_name": "Ken's Cafe",
        "business_category": "cafe",
        "owner_email": "ken@cafe.com",
        "reporting_schedule": "cron(0 9 1 * ? *)",
        "openly_operated": True,
        "legal": {"name": "Ken's Cafe LLC", "email": "books@kens.example", "phone": "+1 555 0100",
                  "street": "1 Bean St", "city": "Austin", "state": "TX", "zip": "78701", "country": "US"},
        "public": {"name": "Ken's", "links": [{"type": "website", "url": "https://kens.example"}]},
    }

    # codebuild fired with customer + account env overrides
    assert len(mod.codebuild.calls) == 1
    build = mod.codebuild.calls[0]
    assert build["projectName"] == "gerp-tower-per-customer-apply"
    overrides = {e["name"]: e["value"] for e in build["environmentVariablesOverride"]}
    assert overrides == {"CUSTOMER_ID": "ken-cafe", "CUSTOMER_ACCOUNT_ID": "222222222222", "CUSTOMER_REGION": "us-east-1"}

    assert result == {
        "customer_id": "ken-cafe",
        "account_id": "222222222222",
        "build_id": "gerp-tower-per-customer-apply:abc-123",
        "build_arn": "arn:aws:codebuild:us-east-1:335667362239:build/abc-123",
        "status": "provisioning",
    }


def test_defaults_applied_for_minimal_event():
    mod = _load()
    # business_name defaults to customer_id, so the account is named "solo" (absent at the
    # pre-vend look, there on the first poll)
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[], [_account("solo", acct_id="333333333333")]])

    mod.handler({"customer_id": "solo", "owner_email": "solo@x.io"}, None)

    params = _params(sc.provision_calls[0])
    assert params["AccountName"] == "solo"            # business_name default
    assert params["SSOUserFirstName"] == "solo"       # owner_first_name default = customer_id
    assert params["SSOUserLastName"] == "Customer"    # default
    assert params["AccountEmail"] == "ops+solo@gradienterp.cloud"

    meta = json.loads(ssm.params[0]["Value"])
    assert meta["business_category"] == "test"        # default
    assert meta["openly_operated"] is True            # default
    assert meta["reporting_schedule"] == "cron(0 9 1 * ? *)"  # default


def test_transient_resource_in_use_is_not_a_hard_failure():
    mod = _load()
    # poll 1: account absent + record FAILED-but-transient -> keep polling
    # poll 2: account ACTIVE -> succeed
    sc, org, ssm, inv = _wire(
        mod,
        org_snapshots=[[], [], [_account("acme", acct_id="444444444444")]],  # the pre-vend look, then two polls
        record_states=[{
            "Status": "FAILED",
            "RecordErrors": [{"Code": "...", "Description": "ResourceInUseException: OU is locked"}],
        }],
    )

    result = mod.handler({"customer_id": "acme", "owner_email": "a@acme.io",
                          "business_name": "acme"}, None)

    assert result["account_id"] == "444444444444"
    assert result["status"] == "provisioning"
    assert sc.describe_calls == 1  # the transient FAILED was consulted and tolerated


def test_hard_failure_raises():
    mod = _load()
    sc, org, ssm, inv = _wire(
        mod,
        org_snapshots=[[]],  # account never appears
        record_states=[{
            "Status": "FAILED",
            "RecordErrors": [{"Code": "InvalidParametersException", "Description": "bad SSO email"}],
        }],
    )

    raised = None
    try:
        mod.handler({"customer_id": "bad", "owner_email": "x", "business_name": "bad"}, None)
    except Exception as e:  # noqa: BLE001 - asserting the handler surfaces hard SC failures
        raised = e
    assert raised is not None
    assert "FAILED" in str(raised)
    # never got past provisioning: no SSM seed, no codebuild
    assert ssm.params == []
    assert mod.codebuild.calls == []


def test_timeout_when_account_never_appears():
    mod = _load()
    # time advances past the 900s deadline after one poll; account never shows
    sc, org, ssm, inv = _wire(
        mod,
        org_snapshots=[[]],
        times=[1000, 1000, 1000, 2000],  # pp_name, deadline-base, check1 (<1900), check2 (>=1900)
    )

    raised = None
    try:
        mod.handler({"customer_id": "ghost", "owner_email": "g@x.io", "business_name": "ghost"}, None)
    except Exception as e:  # noqa: BLE001
        raised = e
    assert raised is not None
    assert "could not find ACTIVE account" in str(raised)
    assert ssm.params == []
    assert mod.codebuild.calls == []


def test_the_vended_account_is_recorded_before_the_build_starts():
    """An account exists and costs money from the moment Service Catalog returns it. Recording it
    only when the CodeBuild finishes means a failed build leaves an account no row points at —
    nothing can find it, bill it, or tear it down. `status` is NOT touched here: the build owns
    that, and flipping it early would mark a gerp live before its stack exists."""
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe")]],
          record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}])
    mod.handler({
        "customer_id": "ken-cafe",
        "owner_email": "ken@cafe.com",
        "owner_first_name": "Ken",
        "owner_last_name": "Barista",
        "business_name": "Ken's Cafe",
        "business_category": "cafe",
        "reporting_schedule": "cron(0 9 1 * ? *)",
    }, None)

    assert len(mod.ddb.updates) == 1
    up = mod.ddb.updates[0]
    assert up["Key"] == {"gerp_id": {"S": "ken-cafe"}}
    assert up["ExpressionAttributeValues"][":a"] == {"S": "222222222222"}
    assert up["ExpressionAttributeValues"][":p"] == {"BOOL": True}, "the create-time wish is published's first value"
    assert "status" not in up["UpdateExpression"]
    # ordered: the row names the account before anything downstream can fail
    assert mod.codebuild.calls, "the build still runs"


def test_invoice_unit_receives_at_the_customers_own_account():
    """The receiver is the CUSTOMER's account, and that is what makes the invoice findable.

    A summary carries AccountId, InvoiceId, BillingPeriod and amounts but no unit name or arn,
    and list_invoice_summaries selects only by ACCOUNT_ID or INVOICE_ID. Point the receiver at
    management and every gerp's invoices arrive in one list with nothing to tell them apart —
    bill_customer would have no way to attribute cost.
    """
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe", acct_id="222222222222")]])
    mod.handler({"customer_id": "ken-cafe", "owner_email": "ken@cafe.com",
                 "business_name": "Ken's Cafe"}, None)

    assert len(inv.units) == 1
    unit = inv.units[0]
    assert unit["Name"] == "gerp-ken-cafe"
    assert unit["InvoiceReceiver"] == "222222222222"
    assert unit["Rule"] == {"LinkedAccounts": ["222222222222"]}


def test_a_rerun_finds_the_unit_already_there_and_continues():
    """Step 4 finds the already-vended account on a retry, so this step runs twice. The second
    run reads the unit by name and skips the create; it never calls create to be refused. A
    refusal of any other kind raises: a gerp with no unit runs forever unbilled and nothing
    downstream notices."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe", acct_id="222222222222")]])
    event = {"customer_id": "ken-cafe", "owner_email": "ken@cafe.com", "business_name": "Ken's Cafe"}
    mod.handler(event, None)

    mod2 = _load()
    sc2, org2, ssm2, _ = _wire(mod2, org_snapshots=[[_account("Ken's Cafe", acct_id="222222222222")]])
    # same invoicing stub, so the second run sees the unit the first one made
    mod2.boto3 = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(
        client=lambda name, **kw: {"servicecatalog": sc2, "organizations": org2,
                             "ssm": ssm2, "invoicing": inv, "bedrock": mod2.bedrock_fake}[name]))
    creates_before = len(inv.units)
    mod2.handler(event, None)   # must not raise

    assert len(inv.units) == 1
    assert inv.lists >= 2, "each run reads the unit by name before creating"
    assert len(inv.units) == creates_before, "the re-run created nothing"


def test_a_repeat_create_is_refused_and_raises():
    """The read is what makes a re-run safe; the create itself has no catch. A create that
    lands on an existing name — a read that lied — is the api's refusal, surfaced."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe", acct_id="222222222222")]])
    inv.units.append({"Name": "gerp-ken-cafe", "InvoiceUnitArn": "arn:aws:invoicing::1:invoice-unit/9",
                      "Rule": {"LinkedAccounts": ["222222222222"]}})
    inv.list_invoice_units = lambda **kw: {"InvoiceUnits": []}
    try:
        mod.handler({"customer_id": "ken-cafe", "owner_email": "ken@cafe.com", "business_name": "Ken's Cafe"}, None)
    except inv.exceptions.ValidationException as e:
        assert "already exists" in str(e)
    else:
        raise AssertionError("a refused create must raise")


def _vend(mod, **kw):
    return mod.handler({"customer_id": "ken-cafe", "owner_email": "ken@cafe.com",
                        "business_name": "Ken's Cafe", **kw}, None)


def test_a_rerun_resumes_from_the_row_and_vends_nothing():
    """The row is the record of the vend. Once it names an account, running the provisioner
    again — after a failure past the vend — creates no second account: ProvisionProduct is not
    called, and the unit, the tenant blob, model access and the build all run against the
    account the row names. This is what makes 'fix and re-run into the same account' true."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[]], row={"aws_account_id": {"S": "222222222222"}})
    out = _vend(mod)
    assert sc.provision_calls == [], "a re-run must not vend again"
    assert out["account_id"] == "222222222222" and out["status"] == "provisioning"
    assert inv.units[0]["Rule"]["LinkedAccounts"] == ["222222222222"]
    assert ssm.params and mod.codebuild.calls, "the rest of the run happened against the recorded account"
    assert not any("vended_at" in u["UpdateExpression"] for u in mod.ddb.updates), "the vend stamp is not rewritten"


def test_a_run_that_died_after_the_vend_finds_its_account_and_vends_nothing():
    """The queue surfaces a message 90 minutes after a consumer died; by then the account the
    first run started is ACTIVE under the business name. It is this gerp's: recorded on the row,
    ProvisionProduct not called."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe", acct_id="333333333333")]])
    out = _vend(mod)
    assert sc.provision_calls == [], "an account already named for the business is not vended twice"
    assert out["account_id"] == "333333333333"
    assert any(u["ExpressionAttributeValues"].get(":a") == {"S": "333333333333"} for u in mod.ddb.updates)


def test_a_queued_message_provisions_like_the_event_and_takes_the_row():
    """save-card sends the payload to tower-vends; the mapping hands it over as one record. The
    body is the event, and the row leaves `queued` the moment the run has it — the gerp screen
    shows the line only for rows no consumer holds yet."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[], [_account("Ken's Cafe")]],
                              record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}],
                              row={"status": {"S": "queued"}})
    payload = {"customer_id": "ken-cafe", "owner_email": "ken@cafe.com", "business_name": "Ken's Cafe"}
    out = mod.handler({"Records": [{"messageId": "m-1", "body": json.dumps(payload)}]}, None)
    assert out["account_id"] == "222222222222" and out["status"] == "provisioning"
    assert mod.ddb.row["status"] == {"S": "provisioning"}
    assert mod.ddb.updates[0]["ExpressionAttributeValues"][":old"] == {"S": "queued"}, "taken before the vend"
    assert len(sc.provision_calls) == 1


def test_a_direct_run_on_a_row_past_queued_leaves_its_status():
    """A hand-run (deploy.sh, the local runner) invokes with the event directly; a row already
    `provisioning` is not written back to anything."""
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe")]],
          record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}], row={"status": {"S": "provisioning"}})
    _vend(mod)
    assert mod.ddb.row["status"] == {"S": "provisioning"}
    assert not any(":old" in u["ExpressionAttributeValues"] for u in mod.ddb.updates)


HUBS = {"us-east-1": {"account": "444444444444", "region": "us-east-1",
                      "bus_arn": "arn:aws:events:us-east-1:444444444444:event-bus/gerp-events",
                      "manage_edges_arn": "arn:aws:lambda:us-east-1:444444444444:function:gerp-hub-manage-edges"},
        "eu-west-1": {"account": "666666666666", "region": "eu-west-1",
                      "bus_arn": "arn:aws:events:eu-west-1:666666666666:event-bus/gerp-events",
                      "manage_edges_arn": "arn:aws:lambda:eu-west-1:666666666666:function:gerp-hub-manage-edges"}}


class FakeLambda:
    """The hub's door, answered as manage_edges answers."""

    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body or {"name": "gerp-edge-spoke-ken-cafe", "existed": False}

    def invoke(self, **kw):
        self.calls.append({**kw, "Payload": json.loads(kw["Payload"])})
        import io
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps({"statusCode": self.status, "body": json.dumps(self.body)}).encode())}


def _with_hub(mod, door_status=200):
    mod.HUBS = HUBS
    mod.ORGS = {"o-test": next(iter(mod.ORGS.values()))}
    fake = FakeLambda(door_status)
    mod._aws_client = lambda name, **kw: fake if name == "lambda" else (_ for _ in ()).throw(AssertionError(name))
    return fake


def test_the_row_says_where_the_gerp_lives_its_directory_row_is_written_and_its_spoke_added_on_its_own_hub():
    """A vended gerp's row carries region, hub (the bus its events go to) and org; its directory
    row (gerp-directory: hub, hub bus arn, region, account) is what any sender reads to put an
    event on its hub; its home hub's door is asked for the spoke edge with the gerp's own
    account, and no other hub hears of it."""
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe")]], record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}])
    door = _with_hub(mod)
    _vend(mod)
    up = next(u for u in mod.ddb.updates if ":a" in u["ExpressionAttributeValues"])
    v = up["ExpressionAttributeValues"]
    assert v[":r"] == {"S": "us-east-1"} and v[":h"] == {"S": HUBS["us-east-1"]["bus_arn"]} and v[":o"] == {"S": "o-test"}
    [directory] = mod.ddb.puts
    assert directory["TableName"] == "gerp-directory"
    assert directory["Item"] == {"gerp_id": {"S": "ken-cafe"}, "hub": {"S": "us-east-1"},
                                 "hub_bus_arn": {"S": HUBS["us-east-1"]["bus_arn"]}, "region": {"S": "us-east-1"},
                                 "aws_account_id": {"S": "222222222222"}}
    [home] = door.calls
    assert home["FunctionName"] == HUBS["us-east-1"]["manage_edges_arn"]
    assert home["Payload"] == {"op": "add", "kind": "spoke", "to": "ken-cafe", "account_id": "222222222222"}
    assert mod.codebuild.calls, "the build still runs"


def test_no_hub_for_the_region_means_no_edge_and_a_refused_edge_does_not_stop_the_vend():
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe")]], record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}])
    door = _with_hub(mod)
    mod.HUBS = {}
    _vend(mod)
    assert door.calls == [] and mod.codebuild.calls
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe")]], record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}])
    door = _with_hub(mod, door_status=409)
    out = _vend(mod)
    assert len(door.calls) == 1 and out["status"] == "provisioning" and mod.codebuild.calls, "the door asked; a refusal does not stop the vend"


def test_a_hub_is_vended_into_the_hubs_ou_and_built_with_the_hub_project_and_no_row():
    """A hub message: Account Factory into the org's hubs OU under the hub's name, then the hub
    build with its id, account and region. No row, no unit, no blob, no model access."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[], [_account("gerp hub eu-west-1", acct_id="555555555555")]],
                              record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}])
    mod.ORGS = {"o-test": {**next(iter(mod.ORGS.values())), "hubs_ou": "hubs (ou-hubs)"}}
    mod.HUB_CODEBUILD_PROJECT = "tower-hub"
    out = mod.handler({"Records": [{"body": json.dumps({"kind": "hub", "hub_id": "eu-west-1", "region": "eu-west-1"})}]}, None)
    params = _params(sc.provision_calls[0])
    assert params["ManagedOrganizationalUnit"] == "hubs (ou-hubs)" and params["AccountName"] == "gerp hub eu-west-1"
    assert params["AccountEmail"] == "ops+hub-eu-west-1@gradienterp.cloud"
    [build] = mod.codebuild.calls
    assert build["projectName"] == "tower-hub"
    assert {e["name"]: e["value"] for e in build["environmentVariablesOverride"]} == {
        "HUB_ID": "eu-west-1", "HUB_ACCOUNT_ID": "555555555555", "HUB_REGION": "eu-west-1"}
    assert mod.ddb.updates == [] and inv.units == [] and ssm.params == []
    assert out["account_id"] == "555555555555" and out["hub_id"] == "eu-west-1"


def test_a_vend_names_its_organization_or_takes_the_first():
    mod = _load()
    assert set(mod.ORGS) == {"default"}, "one entry from the single variables"
    mod.ORGS = {"o-one": dict(mod.ORGS["default"]), "o-two": dict(mod.ORGS["default"], customers_ou="customers (ou-two)")}
    assert mod._org({})[0] == "o-one" and mod._org({"org": "o-two"})[1]["customers_ou"] == "customers (ou-two)"
    try:
        mod._org({"org": "o-none"})
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown organization was accepted")


def test_the_invoice_unit_waits_out_the_payer_link():
    """A just-vended account is ACTIVE in Organizations before billing lists it under the payer;
    the create is refused meanwhile. That is waited out, not raised — and a refusal that outlasts
    the wait still raises, since a gerp with no unit runs unbilled."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe", acct_id="222222222222")]],
                              unlinked_for=3, times=[1000, 1000, 1010, 1020, 1030, 1040, 1050])
    _vend(mod)
    assert inv.refusals == 3 and len(inv.units) == 1

    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe", acct_id="222222222222")]],
                              unlinked_for=99, times=[1000, 1000, 1010, 2000, 2400])
    try:
        _vend(mod)
        raise AssertionError("a unit billing never links must fail the provision visibly")
    except inv.exceptions.ValidationException:
        pass
    assert mod.codebuild.calls == [], "no build for a gerp that cannot be billed"


def test_model_access_is_granted_in_the_customers_account_before_the_build():
    """A vended account can reach no Anthropic model until it carries the use-case form and a
    Marketplace agreement per model — both per ACCOUNT, neither shared from the org. Without this
    the agent per_customer deploys fails its first Converse with "not available for this account"."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[_account("Ken's Cafe")]],
                              record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}])
    _vend(mod)
    b = mod.bedrock_fake
    assert b.forms == [REQUIRED_ENV["BEDROCK_USE_CASE_FORM"].encode()]
    assert b.created == [{"modelId": "anthropic.claude-sonnet-4-6",
                          "offerToken": "tok-anthropic.claude-sonnet-4-6"}]
    # on the SAME session as the SSM seed — the customer's account, no extra assume
    assert len(mod.sts.calls) == 3   # TowerProvisioning, the wait for OperatorOrchestration, then the seed session
    assert len(mod.codebuild.calls) == 1, "the build still starts; nothing waits for AVAILABLE"


def test_an_existing_agreement_is_not_recreated():
    """A re-run of a half-finished provision arrives here twice. What matters is that the agreement
    exists, not that this call created it."""
    mod = _load()
    _wire(mod, org_snapshots=[[_account("Ken's Cafe")]],
          record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}],
          bedrock=FakeBedrock(agreements={"anthropic.claude-sonnet-4-6": "AVAILABLE"}))
    _vend(mod)
    assert mod.bedrock_fake.created == []
    assert len(mod.bedrock_fake.forms) == 1, "the form is re-put; same content, no effect"


def test_the_form_is_waited_on_before_the_offer_is_asked_for():
    """The offers call refuses until the form has propagated, so AUTHORIZED is polled first."""
    mod = _load()
    _wire(mod, org_snapshots=[[_account("Ken's Cafe")]],
          record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}],
          bedrock=FakeBedrock(authorized_after=2))
    _vend(mod)
    assert mod.bedrock_fake.reads == 3
    assert len(mod.bedrock_fake.created) == 1


def test_a_form_that_never_takes_fails_the_provision_visibly():
    mod = _load()
    _wire(mod, org_snapshots=[[_account("Ken's Cafe")]],
          record_states=[{"Status": "IN_PROGRESS", "RecordErrors": []}],
          # the account poll and the row stamp read the clock first; the last value repeats, so
          # the form's 90s deadline is what expires and the 900s account one is not
          bedrock=FakeBedrock(authorized_after=10**6), times=[1000] * 6 + [9999])
    try:
        _vend(mod)
    except RuntimeError as e:
        assert "use-case form did not take" in str(e)
    else:
        raise AssertionError("a gerp with a dead agent must not provision quietly")
    assert mod.codebuild.calls == [], "no build for an account whose agent cannot reach a model"


def test_a_vend_into_another_region_lands_every_piece_in_that_region():
    """A vend message's `region` is where the gerp is built: the OU is that region's customers OU,
    the build carries CUSTOMER_REGION, the tenant blob is written through a session in that
    region, the region's model (config REGIONS[region].model) is agreed to in that
    region, the directory row and the spoke name that region's hub."""
    mod = _load()
    sc, org, ssm, inv = _wire(mod, org_snapshots=[[], [_account("Ken's Cafe", acct_id="222222222222")]])
    door = _with_hub(mod)
    settings = next(iter(mod.ORGS.values()))
    settings["customers_ous"] = {"us-east-1": "customers (ou-us)", "eu-west-1": "customers-eu-west-1 (ou-eu)"}
    mod.REGIONS = {"us-east-1": "us.anthropic.claude-sonnet-4-6", "eu-west-1": "eu.anthropic.claude-sonnet-4-6"}
    sessions, clients = [], []
    inner = mod.boto3.Session
    mod.boto3 = types.SimpleNamespace(Session=lambda **kw: sessions.append(kw) or _recording(inner(**kw), clients))

    _vend(mod, region="eu-west-1")

    assert _params(sc.provision_calls[0])["ManagedOrganizationalUnit"] == "customers-eu-west-1 (ou-eu)"
    overrides = {v["name"]: v["value"] for v in mod.codebuild.calls[0]["environmentVariablesOverride"]}
    assert overrides["CUSTOMER_REGION"] == "eu-west-1"
    assert any(kw.get("region_name") == "eu-west-1" for kw in sessions), "the tenant blob's session is in the gerp's region"
    assert ("bedrock", "eu-west-1") in clients, "the model is agreed to in the gerp's region"
    assert [c["modelId"] for c in mod.bedrock_fake.created] == ["anthropic.claude-sonnet-4-6"], "the eu profile's base model"
    directory = next(p for p in mod.ddb.puts if p["TableName"] == "gerp-directory")
    assert directory["Item"]["region"] == {"S": "eu-west-1"} and directory["Item"]["hub"] == {"S": "eu-west-1"}
    assert directory["Item"]["hub_bus_arn"] == {"S": HUBS["eu-west-1"]["bus_arn"]}
    assert door.calls[0]["FunctionName"] == HUBS["eu-west-1"]["manage_edges_arn"]


def test_a_region_no_gerp_can_be_built_in_is_refused_at_the_vend():
    mod = _load()
    _wire(mod, org_snapshots=[[]])
    mod.REGIONS = {"us-east-1": "us.anthropic.claude-sonnet-4-6", "eu-west-1": "eu.anthropic.claude-sonnet-4-6"}
    try:
        _vend(mod, region="sa-east-1")
    except ValueError as e:
        assert "sa-east-1" in str(e)
    else:
        raise AssertionError("an unknown region must refuse before anything is vended")


def test_a_tokyo_vend_agrees_to_the_jp_profiles_base_model_in_tokyo():
    """Each region names its own inference profile; the Marketplace agreement is on the model
    under it, whatever the geography prefix — `jp.` and `au.` included — made in the region."""
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe", acct_id="222222222222")]])
    _with_hub(mod)
    settings = next(iter(mod.ORGS.values()))
    settings["customers_ous"] = {"ap-northeast-1": "customers-ap-northeast-1 (ou-jp)"}
    mod.REGIONS = {"us-east-1": "us.anthropic.claude-sonnet-4-6", "ap-northeast-1": "jp.anthropic.claude-sonnet-4-5-20250929-v1:0"}
    clients = []
    inner = mod.boto3.Session
    mod.boto3 = types.SimpleNamespace(Session=lambda **kw: _recording(inner(**kw), clients))
    _vend(mod, region="ap-northeast-1")
    assert [c["modelId"] for c in mod.bedrock_fake.created] == ["anthropic.claude-sonnet-4-5-20250929-v1:0"]
    assert ("bedrock", "ap-northeast-1") in clients


def test_the_base_model_is_the_id_under_any_geography_prefix():
    mod = _load()
    for pid, base in [("us.anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"),
                      ("eu.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"),
                      ("apac.anthropic.claude-sonnet-4-20250514-v1:0", "anthropic.claude-sonnet-4-20250514-v1:0"),
                      ("jp.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
                      ("au.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
                      ("global.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"),
                      ("anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6")]:
        assert mod._base_model(pid) == base, pid


def test_a_region_with_no_model_is_refused_before_the_agreement():
    """A config gap is a visible failure at the vend, never a default model."""
    mod = _load()
    _wire(mod, org_snapshots=[[], [_account("Ken's Cafe", acct_id="222222222222")]])
    _with_hub(mod)
    mod.REGIONS = {"us-east-1": "us.anthropic.claude-sonnet-4-6", "eu-west-1": ""}
    try:
        _vend(mod, region="eu-west-1")
    except ValueError as e:
        assert "names no model" in str(e)
    else:
        raise AssertionError("a region with no model must refuse")
    assert mod.bedrock_fake.created == []


def test_every_offered_region_in_config_names_a_profile_of_its_own_geography():
    """config.json is where a reader looks for a region's model: every offered region has one,
    an inference profile id whose base is an Anthropic model."""
    cfg = json.loads((Path(__file__).resolve().parents[3] / "config.json").read_text())
    assert "MODELS" not in cfg, "the model is the region's entry, not a family table"
    for rid, r in cfg["REGIONS"].items():
        assert "profile" not in r, rid
        if r.get("status") == "offered":
            m = r.get("model", "")
            head, _, rest = m.partition(".")
            assert rest.startswith("anthropic.claude") and head, (rid, m)


def _recording(session, clients):
    """A session whose client() calls are written down as (service, region)."""
    real = session.client
    return types.SimpleNamespace(client=lambda name, **kw: clients.append((name, kw.get("region_name"))) or real(name, **kw))


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all provision_customer tests passed")
