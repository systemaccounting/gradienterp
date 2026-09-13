"""provision_customer — orchestrator lambda.

Driven by tower's signup pipeline. Vends a new customer sub-account via
Service Catalog's AWS Control Tower Account Factory product. CT auto-baselines
+ applies preventive controls + sets up Config + IC. The OperatorOrchestration
role is auto-deployed to the new account by a service-managed stackset on the
customers OU (see prod/platform/management/operator_trust_stackset.tf), so
operator can assume directly into the new account without IAM trust widening.

Lambda then assumes OperatorOrchestration on the new account, seeds SSM tenant
metadata, and triggers codebuild for per-customer terraform.

Bash equivalent (for local ad-hoc use): .github/workflows/per-customer-apply.sh.

Event shape:
{
  "customer_id": "ken-cafe",                              required
  "owner_email": "ken@example.com",                       required (becomes SSO user)
  "owner_first_name": "Ken",                              optional (default = customer_id)
  "owner_last_name": "Customer",                          optional (default = "Customer")
  "business_name": "Ken's Cafe",                          optional (default = customer_id)
  "business_category": "cafe",                            optional (default = "test")
  "openly_operated": true,                                optional (default = true)
  "legal": {"name", "email", "phone", "street", ...},      optional — the business's legal profile
  "public": {"name", "street", ..., "links"},              optional — its public profile
  "reporting_schedule": "cron(0 9 1 * ? *)",              optional
  "tenant_aws_email": "ops+ken-cafe@gradienterp.cloud"    optional
}

Returns build_id; doesn't wait for codebuild to complete (can take 5-10min).
SC AF account vending typically takes 5-10min; lambda blocks on that.
"""

import json
import logging
import os
import time

import boto3

from aws import client as _aws_client, resource as _aws_resource

log = logging.getLogger()
log.setLevel(logging.INFO)

sts = _aws_client("sts")
codebuild = _aws_client("codebuild")
ddb = _aws_client("dynamodb")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
DIRECTORY_TABLE = os.environ.get("DIRECTORY_TABLE", "gerp-directory")   # the row every gerp reads to address this one

CODEBUILD_PROJECT = os.environ["CODEBUILD_PROJECT"]
HUB_CODEBUILD_PROJECT = os.environ.get("HUB_CODEBUILD_PROJECT", "")
OPERATOR_ORCHESTRATION_ROLE = "OperatorOrchestration"
REGION = os.environ.get("AWS_REGION", "us-east-1")

# The organizations this provisioner vends into, keyed by org id: the management role that may
# call Account Factory there, the product and path, the OU a gerp lands in and the OU a hub lands
# in. One entry today; a second organization is a second entry. `ORGS` is the map; the single
# variables are the same entry for the local runner.
ORGS = json.loads(os.environ["ORGS"]) if os.environ.get("ORGS") else {
    os.environ.get("ORG_ID", "default"): {
        "tower_provisioning_role": os.environ["TOWER_PROVISIONING_ROLE"],
        "af_product_id": os.environ["AF_PRODUCT_ID"],
        "af_path_id": os.environ["AF_PATH_ID"],
        "customers_ou": os.environ["CUSTOMERS_OU_MANAGED_NAME"],
        "customers_ous": json.loads(os.environ.get("CUSTOMERS_OUS") or "{}"),
        "hubs_ou": os.environ.get("HUBS_OU_MANAGED_NAME", ""),
    }
}
# the regions a gerp can be built in and each one's model (config.json REGIONS[region].model,
# compacted to region → model): the Marketplace agreement is on the region's base model
REGIONS = json.loads(os.environ.get("REGIONS") or "{}")
# the hubs, by region (config.json HUBS): the bus a gerp of that region is a spoke of, and the
# door its spoke edge is added through
# HUBS rides as {region: account} (a lambda's environment caps at 4KB); the hub's bus and door
# arns follow from the account and the region (prod/hub's names)
HUBS = {r: {"account": a, "region": r,
            "bus_arn": f"arn:aws:events:{r}:{a}:event-bus/{os.environ.get('STACK_PREFIX', 'gerp')}-events",
            "manage_edges_arn": f"arn:aws:lambda:{r}:{a}:function:{os.environ.get('STACK_PREFIX', 'gerp')}-hub-manage-edges"}
        for r, a in json.loads(os.environ.get("HUBS") or "{}").items()}


def _org(event):
    """The organization a vend goes into: the one the message names, or the first."""
    org_id = event.get("org") or next(iter(ORGS))
    if org_id not in ORGS:
        raise ValueError(f"no organization {org_id}; ORGS names {sorted(ORGS)}")
    return org_id, ORGS[org_id]

# Model access is per ACCOUNT. A vended account can reach no Anthropic model until it carries the
# use-case form (once, covers every anthropic model) and a Marketplace agreement per model. Both
# are API calls in the new account. The form is the operator's own statement to Anthropic, so it
# comes from tower's config; the model list has to name the base id of whatever per_customer
# applies (`us.anthropic.claude-sonnet-4-6` is an inference PROFILE over
# `anthropic.claude-sonnet-4-6`, and the agreement is on the model).
BEDROCK_USE_CASE_FORM = os.environ.get("BEDROCK_USE_CASE_FORM", "")
BEDROCK_MODEL_IDS = [m.strip() for m in os.environ.get("BEDROCK_MODEL_IDS", "").split(",") if m.strip()]


def _assume(role_arn, session_name, region=None):
    """A session on the role; in `region` when given — a gerp's account is pinned to its region,
    so anything written there (its tenant blob) goes through that region's endpoint."""
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _wait_for_role(role_arn, session_name, seconds=600):
    """Until the role assumes, or the deadline: a stackset's auto-deployment lands a few minutes
    after the account joins its OU."""
    deadline = time.time() + seconds
    while True:
        try:
            _assume(role_arn, session_name)
            return
        except Exception as e:  # noqa: BLE001 — AccessDenied until the stackset lands
            if time.time() >= deadline:
                raise RuntimeError(f"{role_arn} did not become assumable in {seconds}s: {e}") from None
            time.sleep(15)


def _invoice_unit_arn(invoicing, name):
    """The unit a prior run made, read by name; None when there is none."""
    units = invoicing.list_invoice_units(Filters={"Names": [name]}).get("InvoiceUnits", [])
    return units[0]["InvoiceUnitArn"] if units else None


def _create_invoice_unit(mgmt, customer_id, account_id):
    """One invoice unit holding this customer's linked account. Idempotent by name: a re-run
    reads the unit the first run made and skips the create.

    The name is what prints on the invoice and cannot be changed after creation —
    only deleted and remade — so it is the customer_id and nothing derived.
    """
    invoicing = mgmt.client("invoicing")
    name = f"gerp-{customer_id}"
    existing = _invoice_unit_arn(invoicing, name)
    if existing:
        log.info(f"invoice unit {name} already exists")
        return existing
    # A just-vended account is ACTIVE in Organizations seconds before billing lists it under the
    # payer, and the create is refused meanwhile ("Selected Linked Accounts … are not under the
    # Payer"). That is propagation, so it is waited out: a few minutes, then the refusal stands.
    deadline = time.time() + 300
    while True:
        try:
            return _create_invoice_unit_once(invoicing, name, customer_id, account_id)
        except invoicing.exceptions.ValidationException as e:
            if "not under the Payer" not in str(e) or time.time() > deadline:
                raise
            log.warning(f"invoice unit {name}: {account_id} not yet under the payer, waiting")
            time.sleep(20)


def _create_invoice_unit_once(invoicing, name, customer_id, account_id):
    unit = invoicing.create_invoice_unit(
        Name=name,
        # The receiver is the customer's OWN account, not management, and that
        # is what makes the invoice findable: a summary carries AccountId,
        # InvoiceId, BillingPeriod and amounts but NO unit name or arn, and
        # list-invoice-summaries selects by ACCOUNT_ID or INVOICE_ID only. Point
        # every unit at management and each month's invoices arrive in one list
        # with nothing to attribute them by. Pointed here, the selector IS the
        # attribution.
        #
        # It does not move who pays. Under consolidated billing the payer
        # account settles with AWS; the receiver decides who the document is
        # addressed to.
        InvoiceReceiver=account_id,
        Description=f"gradientERP instance {customer_id}",
        Rule={"LinkedAccounts": [account_id]},
        ResourceTags=[{"Key": "gerp:customer", "Value": customer_id}],
    )
    log.info(f"invoice unit {name} -> {unit['InvoiceUnitArn']}")
    return unit["InvoiceUnitArn"]
    # everything else raises. A gerp with no invoice unit is a gerp that runs
    # forever without being billed, and nothing downstream notices — a visible
    # provisioning failure is the cheaper outcome, and re-running is safe.


def _base_model(profile_id: str) -> str:
    """The base model under an inference profile: `eu.anthropic.claude-sonnet-4-6` →
    `anthropic.claude-sonnet-4-6`, `jp.anthropic.claude-sonnet-4-5-…` → `anthropic.…`; the
    Marketplace agreement is on the model. A geography prefix is the one segment before
    `anthropic.`; a bare model id passes through."""
    head, _, rest = profile_id.partition(".")
    return rest if rest.startswith("anthropic.") else profile_id


def _grant_model_access(new_session, customer_id, region=None):
    """The use-case form, then one agreement per model, in the customer's own account.

    Idempotent: the form is re-put (same content, no effect), and a model whose agreement is
    already AVAILABLE or PENDING is skipped. In practice the form is ORG-shared and write-once —
    `GetUseCaseForModelAccess` returns identical content in management and every member account,
    and a put against an account that has one is accepted and discarded (support case
    178559783400219) — so on a vended account the put is a no-op and the AUTHORIZED poll passes on
    its first read. It stays because a fresh org has no form, and nothing here knows which case it is in. Nothing waits for AVAILABLE — that takes minutes and
    the per_customer build that follows takes longer, so by the time an agent exists to call the
    model the agreement has settled. What IS waited on, briefly, is AUTHORIZED after the form: the
    offers call refuses until the form has propagated.

    Everything else raises. A gerp whose agent cannot reach a model is a gerp with a dead agent and
    nothing downstream that notices; a visible provisioning failure is the cheaper outcome, and
    re-running is safe.
    """
    region = region or REGION
    # the region's model (config.json REGIONS[region].model), agreed IN that region; with no
    # REGIONS at all (a bare deploy) the list is BEDROCK_MODEL_IDS
    if REGIONS:
        if not REGIONS.get(region):
            raise ValueError(f"{region} names no model in config.json REGIONS")
        model_ids = [_base_model(REGIONS[region])]
    else:
        model_ids = list(BEDROCK_MODEL_IDS)
    if not model_ids:
        log.info("no BEDROCK_MODEL_IDS — skipping model access")
        return []
    bedrock = new_session.client("bedrock", region_name=region)
    if BEDROCK_USE_CASE_FORM:
        bedrock.put_use_case_for_model_access(formData=BEDROCK_USE_CASE_FORM.encode())
        log.info(f"use-case form on file for {customer_id}")

    granted = []
    for model_id in model_ids:
        deadline = time.time() + 90
        while True:
            avail = bedrock.get_foundation_model_availability(modelId=model_id)
            if avail["authorizationStatus"] == "AUTHORIZED":
                break
            if time.time() > deadline:
                raise RuntimeError(
                    f"{model_id} authorizationStatus={avail['authorizationStatus']} on "
                    f"{customer_id} — the use-case form did not take")
            time.sleep(5)
        if avail["agreementAvailability"]["status"] in ("AVAILABLE", "PENDING"):
            log.info(f"{model_id} agreement already {avail['agreementAvailability']['status']}")
            continue
        offers = bedrock.list_foundation_model_agreement_offers(modelId=model_id)["offers"]
        bedrock.create_foundation_model_agreement(modelId=model_id, offerToken=offers[0]["offerToken"])
        log.info(f"{model_id} agreement created on {customer_id}")
        granted.append(model_id)
    return granted


def _active_artifact_id(sc, product_id):
    # AF product gets a new provisioning artifact on each CT update; pick the
    # currently-active one at runtime rather than hardcoding.
    artifacts = sc.list_provisioning_artifacts(ProductId=product_id)["ProvisioningArtifactDetails"]
    return next(a["Id"] for a in artifacts if a["Active"])


class _Run:
    """One line per step, JSON, carrying the gerp and the seconds since the run began — the log
    is read per gerp (`{ $.customer_id = "westwood-c40fd8" }`), not per request id, and the
    elapsed seconds are the vend's clock: Account Factory, the invoice unit, the build handoff.
    A failure logs the step it died in with the error, then raises as before."""

    def __init__(self, customer_id):
        self.customer_id, self.t0, self.step = customer_id, time.monotonic(), "start"

    def mark(self, step, **fields):
        self.step = step
        log.info(json.dumps({"event": "provision.step", "customer_id": self.customer_id, "step": step,
                             "elapsed_s": round(time.monotonic() - self.t0, 1), **fields}))

    def failed(self, e):
        log.error(json.dumps({"event": "provision.failed", "customer_id": self.customer_id, "step": self.step,
                              "elapsed_s": round(time.monotonic() - self.t0, 1),
                              "error": f"{type(e).__name__}: {e}"[:600]}))


def _vend(mgmt, sc, customer_id, business_name, owner_email, owner_first_name, owner_last_name,
          tenant_aws_email, openly_operated, run=None, settings=None, ou=None, org_id="", record=True, region=None):
    """Steps 2 and 3: a new account through Account Factory, found by name, recorded on the row.

    An ACTIVE account already named `business_name` is this gerp's — a run that died between
    Account Factory and the row (the queue surfaces its message 90 minutes on) picks it up here
    and vends nothing. `ou` is the managed OU the account lands in (the org's customers OU, or
    its hubs OU); `record=False` for a hub, which has no row."""
    settings = settings or next(iter(ORGS.values()))
    ou = ou or settings["customers_ou"]
    org = mgmt.client("organizations")
    account_id = _active_account(org, business_name)
    if account_id:
        if run: run.mark("account_found", account_id=account_id)
        if record:
            _record_account(customer_id, account_id, openly_operated, org_id, region)
            if run: run.mark("account_recorded", account_id=account_id)
        return account_id
    artifact_id = _active_artifact_id(sc, settings["af_product_id"])
    pp_name = f"customer-{customer_id}-{int(time.time())}"
    record_detail = sc.provision_product(
        ProductId=settings["af_product_id"],
        ProvisioningArtifactId=artifact_id,
        PathId=settings["af_path_id"],
        ProvisionedProductName=pp_name,
        ProvisioningParameters=[
            {"Key": "AccountName", "Value": business_name},
            {"Key": "AccountEmail", "Value": tenant_aws_email},
            {"Key": "SSOUserEmail", "Value": owner_email},
            {"Key": "SSOUserFirstName", "Value": owner_first_name},
            {"Key": "SSOUserLastName", "Value": owner_last_name},
            {"Key": "ManagedOrganizationalUnit", "Value": ou},
        ],
    )["RecordDetail"]
    record_id = record_detail["RecordId"]
    if run: run.mark("account_factory", record_id=record_id, business_name=business_name)

    # 3. poll for the new account by NAME in Organizations. SC AF can return
    # describe-record status FAILED/TAINTED while the underlying CT operation
    # actually completes successfully (especially with ResourceInUseException
    # racing on the customers OU). Source of truth is whether an ACTIVE account
    # named `business_name` shows up — describe-record is just a hint.
    deadline = time.time() + 900  # 15 minutes
    account_id = None
    while time.time() < deadline:
        account_id = _active_account(org, business_name)
        if account_id:
            if run: run.mark("account_active", account_id=account_id)
            break
        # Check SC record for hard-failure-with-no-account (e.g., bad input).
        # ResourceInUseException is a transient lock and is NOT a hard fail —
        # ignore it; account will appear once the lock clears.
        r = sc.describe_record(Id=record_id)["RecordDetail"]
        state = r["Status"]
        errs = r.get("RecordErrors", [])
        is_transient = any("ResourceInUseException" in e.get("Description", "") for e in errs)
        if state in ("FAILED", "TAINTED") and not is_transient:
            raise Exception(f"ProvisionProduct {state}: {errs}")
        log.info(f"sc_status={state}, account not yet visible, polling...")
        time.sleep(15)
    if not account_id:
        raise Exception(f"could not find ACTIVE account named '{business_name}' after 15min")

    # Record the vended account on the row NOW, not when the build finishes. The account exists and
    # costs money from this moment; if the CodeBuild that follows fails, an account with no row
    # pointing at it is one nothing can find, bill, or tear down. `status` stays `provisioning` —
    # the buildspec flips it once the stack is up.
    if record:
        _record_account(customer_id, account_id, openly_operated, org_id, region)
        if run: run.mark("account_recorded", account_id=account_id)
    return account_id


def _active_account(org, business_name):
    for page in org.get_paginator("list_accounts").paginate():
        for acct in page["Accounts"]:
            if acct["Name"] == business_name and acct["Status"] == "ACTIVE":
                return acct["Id"]
    return None


def _record_account(customer_id, account_id, openly_operated, org_id="", region=None):
    # `region`, `hub` and `org` say where the gerp lives: every arn built for it and every trust
    # it is admitted by reads them off the row rather than assuming the one region, hub and org
    region = region or REGION
    hub = HUBS.get(region, {})
    ddb.update_item(
        TableName=CUSTOMERS_TABLE,
        Key={"gerp_id": {"S": customer_id}},
        # `published` is the operator's mirror of the gerp's own flag, read by the directory and the
        # read api's publisher; the create-time wish is its first value, the settings toggle's
        # announcement every one after
        UpdateExpression="SET aws_account_id = :a, vended_at = :t, published = :p, #r = :r, hub = :h, org = :o",
        ExpressionAttributeNames={"#r": "region"},
        ExpressionAttributeValues={
            ":a": {"S": account_id},
            ":t": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            ":p": {"BOOL": bool(openly_operated)},
            ":r": {"S": region},
            ":h": {"S": hub.get("bus_arn", "")},
            ":o": {"S": org_id},
        },
    )
    # the directory row: what a sender anywhere reads to put an event on this gerp's hub
    # (modules/events `emit_to`). Its hub is its region's; `close_account` deletes the row
    ddb.put_item(TableName=DIRECTORY_TABLE, Item={
        "gerp_id": {"S": customer_id}, "hub": {"S": region}, "hub_bus_arn": {"S": hub.get("bus_arn", "")},
        "region": {"S": region}, "aws_account_id": {"S": account_id},
    })


def _add_edges(customer_id, account_id, run=None, region=None):
    """The gerp's one edge: a spoke on its region's hub, the rule that puts events addressed to
    it onto its own bus. Through the hub's door, cross-account. A sender elsewhere finds this hub
    in the directory row and puts here itself, so no other hub learns of the gerp. No hub for the
    region means no edge."""
    region = region or REGION
    home = HUBS.get(region, {})
    if not home.get("manage_edges_arn"):
        log.info(f"no hub for {region}; no edges for {customer_id}")
        return
    ask = {"op": "add", "kind": "spoke", "to": customer_id, "account_id": account_id}
    # the door is invoked through its own region's endpoint
    resp = _aws_client("lambda", region_name=home.get("region") or region).invoke(
        FunctionName=home["manage_edges_arn"], InvocationType="RequestResponse", Payload=json.dumps(ask).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError") or int(out.get("statusCode", 500)) != 200:
        # the gerp stands without its spoke: a person adds it (scripts/edge.sh); the vend goes
        # on, since the account and the build are not the edge
        log.error(json.dumps({"event": "provision.edge_failed", "customer_id": customer_id,
                              "hub": region, "kind": "spoke", "response": out}))
        return
    if run: run.mark("spoke_edge", account_id=account_id, hub=region,
                     rule=json.loads(out.get("body") or "{}").get("name"))


def handler(event, context):
    if "Records" in event:
        # the vends queue (one message a batch): the body is the payload save-card sent. A raise
        # returns the message to the queue; the third failure parks it on tower-vends-failed.
        event = json.loads(event["Records"][0]["body"])
    if event.get("kind") == "hub":
        # a hub: an account in the org's hubs OU running prod/hub, one per region (scripts/vend_hub.sh)
        run = _Run(f"hub-{event.get('hub_id') or event.get('region') or '?'}")
        try:
            return _provision_hub(event, run)
        except Exception as e:  # noqa: BLE001
            run.failed(e)
            raise
    run = _Run(event.get("customer_id", "?"))
    try:
        return _provision(event, run)
    except Exception as e:  # noqa: BLE001 — logged with its step, then raised as before
        run.failed(e)
        raise


def _provision_hub(event, run):
    """A hub's vend: the account through Account Factory into the hubs OU, then the hub build
    (prod/hub) against it. No row, no invoice unit, no tenant blob, no model access — a hub holds
    routing and nothing else. Its outputs go into config.json HUBS by hand; the build prints them."""
    region = event.get("region") or REGION
    hub_id = event.get("hub_id") or region
    name = f"gerp hub {hub_id}"
    email = event.get("tenant_aws_email", f"ops+hub-{hub_id}@gradienterp.cloud")
    org_id, settings = _org(event)
    if not settings.get("hubs_ou") or not HUB_CODEBUILD_PROJECT:
        raise ValueError("no hubs OU or hub build project configured for this organization")
    run.mark("start", business_name=name)
    mgmt = _assume(settings["tower_provisioning_role"], f"provision-hub-{hub_id}")
    account_id = _vend(mgmt, mgmt.client("servicecatalog"), f"hub-{hub_id}", name, email, "gerp", f"hub {hub_id}",
                       email, False, run, settings=settings, ou=settings["hubs_ou"], org_id=org_id, record=False)
    # the build assumes OperatorOrchestration in the new account, which the hubs OU's stackset
    # deploys minutes after the account lands there; a gerp's vend spends those minutes on its
    # invoice unit and model access, a hub has nothing between the vend and the build
    _wait_for_role(f"arn:aws:iam::{account_id}:role/{OPERATOR_ORCHESTRATION_ROLE}", f"hub-{hub_id}")
    run.mark("role_ready", account_id=account_id)
    build = codebuild.start_build(
        projectName=HUB_CODEBUILD_PROJECT,
        environmentVariablesOverride=[
            {"name": "HUB_ID", "value": hub_id, "type": "PLAINTEXT"},
            {"name": "HUB_ACCOUNT_ID", "value": account_id, "type": "PLAINTEXT"},
            {"name": "HUB_REGION", "value": region, "type": "PLAINTEXT"},
        ],
    )["build"]
    run.mark("build_started", account_id=account_id, build_id=build["id"])
    return {"hub_id": hub_id, "account_id": account_id, "region": region, "build_id": build["id"], "status": "provisioning"}


def _taken(customer_id):
    """The row leaves `queued` the moment a consumer has the message; a re-run of a row already
    past it changes nothing."""
    try:
        ddb.update_item(
            TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": customer_id}},
            UpdateExpression="SET #s = :new",
            ConditionExpression="#s = :old",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":new": {"S": "provisioning"}, ":old": {"S": "queued"}},
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        pass


def _provision(event, run):
    customer_id = event["customer_id"]
    owner_email = event["owner_email"]
    owner_sub = event.get("owner_sub")  # the owner's Cognito sub (account_id); stashed for the chat's owner check
    owner_first_name = event.get("owner_first_name", customer_id)
    owner_last_name = event.get("owner_last_name", "Customer")
    business_name = event.get("business_name", customer_id)
    business_category = event.get("business_category", "test")
    openly_operated = event.get("openly_operated", True)
    legal = event.get("legal") or {}        # the legal business profile, into the tenant blob
    public = event.get("public") or {}      # and the public one
    reporting_schedule = event.get("reporting_schedule", "cron(0 9 1 * ? *)")
    tenant_aws_email = event.get("tenant_aws_email", f"ops+{customer_id}@gradienterp.cloud")
    # where the gerp is built: the vend's region, in that region's customers OU (its own pin);
    # a region with no OU of its own is the first region's
    region = event.get("region") or REGION
    if REGIONS and region not in REGIONS:
        raise ValueError(f"{region} is not a region a gerp can be built in ({sorted(REGIONS)})")

    run.mark("start", owner_email=owner_email, business_name=business_name, region=region)
    _taken(customer_id)
    org_id, settings = _org(event)
    ou = (settings.get("customers_ous") or {}).get(region) or settings["customers_ou"]

    # 1. assume TowerProvisioning in mgmt — only mgmt principals can call SC AF
    mgmt = _assume(settings["tower_provisioning_role"], f"provision-{customer_id}")
    sc = mgmt.client("servicecatalog")

    # A re-run resumes. The row is the record of the vend: once it names an account, that account
    # is this gerp's, and running the vend again would create a second one for the same purchase
    # (a slot burned, an account nothing points at). Everything after the vend is idempotent, so
    # a run that failed past it — the invoice unit, the tenant blob, model access, the build — is
    # re-run by invoking this again with the same event.
    already = ddb.get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": customer_id}},
                           ProjectionExpression="aws_account_id").get("Item") or {}
    account_id = (already.get("aws_account_id") or {}).get("S")
    if account_id:
        run.mark("resume", account_id=account_id)
    else:
        account_id = _vend(mgmt, sc, customer_id, business_name, owner_email, owner_first_name,
                           owner_last_name, tenant_aws_email, openly_operated, run,
                           settings=settings, ou=ou, org_id=org_id, region=region)
    # its edge: the spoke on its region's hub, so events addressed to this gerp reach its bus
    _add_edges(customer_id, account_id, run, region)

    # 4. one invoice unit for this account, so AWS issues a REAL invoice per gerp each month — own
    # invoice id, own tax — instead of folding it into the org's consolidated bill; bill_customer
    # reads that invoice's TotalAmount and bills at 1.2x. Free: AWSInvoicing has no chargeable
    # products. HERE, not in the billing run: a unit only invoices the months it existed for.
    # Invoicing is a management-account API, so it rides the mgmt session.
    run.mark("invoice_unit", account_id=account_id)
    _create_invoice_unit(mgmt, customer_id, account_id)

    run.mark("assume_customer_account", account_id=account_id)
    # 5. assume OperatorOrchestration on new account directly. Trust was
    # established by the customers-OU stackset during CT baselining — no
    # mgmt-session double-hop, no IAM trust widening. The stackset lands the role a few
    # minutes after the account joins the OU; a fast vend gets here first
    _wait_for_role(f"arn:aws:iam::{account_id}:role/{OPERATOR_ORCHESTRATION_ROLE}", f"seed-{customer_id}")
    new_session = _assume(
        f"arn:aws:iam::{account_id}:role/{OPERATOR_ORCHESTRATION_ROLE}",
        f"seed-{customer_id}",
        region,
    )

    # 6. seed SSM tenant metadata (per-customer in their own account)
    new_ssm = new_session.client("ssm")
    new_ssm.put_parameter(
        Name=f"/gradienterp/customers/{customer_id}",
        Type="String",
        Value=json.dumps({
            "business_name": business_name,
            "business_category": business_category,
            "owner_email": owner_email,
            "reporting_schedule": reporting_schedule,
            "openly_operated": openly_operated,
            **({"legal": legal} if legal else {}),
            **({"public": public} if public else {}),
        }),
        Overwrite=True,
    )
    run.mark("tenant_blob", account_id=account_id)

    # Stash the owner's Cognito sub same-account so the web chat (modules/agent)
    # recognizes the owner without a cross-account lookup. The chat lambda reads
    # /gradienterp/customers/<id>/owner_sub. Skipped if the caller didn't pass it
    # (e.g. CLI provisioning); the chat then authorizes only linked contacts.
    if owner_sub:
        new_ssm.put_parameter(
            Name=f"/gradienterp/customers/{customer_id}/owner_sub",
            Type="String",
            Value=owner_sub,
            Overwrite=True,
        )
        log.info(f"stashed owner_sub for {customer_id}")

    # 6b. model access, in the customer's account — the agent per_customer deploys is dead without it
    run.mark("model_access", account_id=account_id)
    _grant_model_access(new_session, customer_id, region)

    # 7. trigger codebuild, in the gerp's region
    build = codebuild.start_build(
        projectName=CODEBUILD_PROJECT,
        environmentVariablesOverride=[
            {"name": "CUSTOMER_ID", "value": customer_id, "type": "PLAINTEXT"},
            {"name": "CUSTOMER_ACCOUNT_ID", "value": account_id, "type": "PLAINTEXT"},
            {"name": "CUSTOMER_REGION", "value": region, "type": "PLAINTEXT"},
        ],
    )["build"]
    run.mark("build_started", account_id=account_id, build_id=build["id"])

    return {
        "customer_id": customer_id,
        "account_id": account_id,
        "build_id": build["id"],
        "build_arn": build["arn"],
        "status": "provisioning",
    }
