"""reset-dev — wipe a tenant's DATA back to a clean, freshly-provisioned state, keeping config.

The tenant self-describes what to reset: every stateful resource carries a `gerp:layer` tag
(set in each module's infra). ONE resourcegroupstaggingapi query enumerates the tables + buckets
AND says which layer each belongs to — the same self-describing-via-tags trick as `gerp:src-dir`
on lambdas. No hardcoded catalog, no name-pattern allowlist; a new module's table is picked up
automatically once it tags itself.

Layers (see the root AGENTS.md):
  config       schema / settings / rules-params — platform config, NEVER reset
  books        the accounting ledger + derived (balances, pending, statement CSVs)
  operational  the business registers (inventory, invoicing, purchasing, labor, …) + rule instances
  session      agent chat/email/session/upload state (transcripts, memory, uploaded docs)

reset-dev clears every layer EXCEPT config by default; `--layer <name>` targets one (e.g. reset
just `session` between test cases). Emptied, not dropped — DDB items scanned + batch-deleted (per
each table's real key schema), S3 objects + versions deleted — so table config / stream ARNs / bucket
policies survive and the tenant stays functional with empty books.

Reached by assuming a role into the hardcoded account (reassign to a dedicated dev account later).

  bash scripts/reset-dev.sh                 # clear books+operational+session (confirm prompt)
  bash scripts/reset-dev.sh --yes           # no prompt
  bash scripts/reset-dev.sh --layer session # just chat/memory/uploads
  bash scripts/reset-dev.sh --dry-run       # show the plan, touch nothing

NOTE: AgentCore Memory conversation events are not DDB/S3, so the tag query doesn't reach them;
clearing the `session` layer empties the saved-chat INDEX (the sidebar) and the S3SessionManager
state, but Memory transcripts age out on their own TTL.
"""

import argparse
import sys

# ── target account (reassign to a dedicated dev account later) ──
DEV_ACCOUNT_ID = "867637277314"        # gradienterp today
SOURCE_PROFILE = "operator-org"        # base creds that can assume into the account
ASSUME_ROLE = "OperatorOrchestration"  # the per-tenant role this profile assumes
REGION = "us-east-1"

TAG_KEY = "gerp:layer"
KEEP_LAYER = "config"


def session_for_account():
    import boto3
    base = boto3.Session(profile_name=SOURCE_PROFILE)
    creds = base.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{DEV_ACCOUNT_ID}:role/{ASSUME_ROLE}",
        RoleSessionName="reset-dev",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=REGION,
    )


def tagged(session):
    """{arn: layer} for every dynamodb:table + s3:bucket carrying gerp:layer — one query."""
    tagging = session.client("resourcegroupstaggingapi")
    out = {}
    token = ""
    while True:
        resp = tagging.get_resources(
            TagFilters=[{"Key": TAG_KEY}],
            ResourceTypeFilters=["dynamodb:table", "s3:bucket"],
            PaginationToken=token,
        )
        for r in resp["ResourceTagMappingList"]:
            layer = next(t["Value"] for t in r["Tags"] if t["Key"] == TAG_KEY)
            out[r["ResourceARN"]] = layer
        token = resp.get("PaginationToken", "")
        if not token:
            return out


def all_stateful(session):
    """Every DDB table + S3 bucket ARN in the account — to catch any missing a gerp:layer tag."""
    arns = set()
    ddb = session.client("dynamodb")
    for page in ddb.get_paginator("list_tables").paginate():
        for name in page["TableNames"]:
            arns.add(ddb.describe_table(TableName=name)["Table"]["TableArn"])
    for b in session.client("s3").list_buckets()["Buckets"]:
        arns.add(f"arn:aws:s3:::{b['Name']}")
    return arns


def table_name(arn):
    return arn.split(":table/", 1)[1]


def bucket_name(arn):
    return arn.split(":::", 1)[1]


def empty_table(session, name):
    """Scan the table's keys (schema-aware) and batch-delete every item. Returns count."""
    ddb = session.client("dynamodb")
    keys = [k["AttributeName"] for k in ddb.describe_table(TableName=name)["Table"]["KeySchema"]]
    names = {f"#k{i}": k for i, k in enumerate(keys)}   # alias to dodge reserved words
    proj = ", ".join(names)
    deleted = 0
    batch = []

    def flush():
        nonlocal deleted, batch
        while batch:
            chunk, batch = batch[:25], batch[25:]
            req = {name: [{"DeleteRequest": {"Key": k}} for k in chunk]}
            for _ in range(6):
                resp = ddb.batch_write_item(RequestItems=req)
                req = resp.get("UnprocessedItems") or {}
                if not req:
                    break
            deleted += len(chunk)

    for page in ddb.get_paginator("scan").paginate(
        TableName=name, ProjectionExpression=proj, ExpressionAttributeNames=names
    ):
        for item in page["Items"]:
            batch.append(item)
            if len(batch) >= 500:
                flush()
    flush()
    return deleted


def empty_bucket(session, name):
    """Delete every object version + delete-marker. Returns count."""
    s3 = session.client("s3")
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=name):
        objs = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=name, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
        deleted += len(objs)
    return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", help=f"clear only this layer (never '{KEEP_LAYER}')")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, touch nothing")
    args = ap.parse_args()
    if args.layer == KEEP_LAYER:
        sys.exit(f"{KEEP_LAYER} is config — refusing to reset it")

    session = session_for_account()
    layers = tagged(session)

    # discipline: a stateful resource with no gerp:layer tag would be silently skipped — surface it
    untagged = sorted(all_stateful(session) - set(layers))
    if untagged:
        print("!! WARNING — stateful resources with NO gerp:layer tag (NOT reset):")
        for a in untagged:
            print(f"     {a.split(':')[-1]}")
        print("   add the tag in the module's infra + apply so reset covers it.\n")

    def wanted(layer):
        return layer != KEEP_LAYER and (args.layer is None or layer == args.layer)

    targets = sorted(
        ((arn, layer) for arn, layer in layers.items() if wanted(layer)),
        key=lambda x: (x[1], x[0]),
    )
    if not targets:
        sys.exit(f"nothing matches (layer={args.layer!r})")

    print(f"account {DEV_ACCOUNT_ID} — will EMPTY (keep layer '{KEEP_LAYER}'):")
    for arn, layer in targets:
        kind = "table " if ":table/" in arn else "bucket"
        name = table_name(arn) if kind == "table " else bucket_name(arn)
        print(f"  [{layer:11}] {kind} {name}")
    kept = sorted(bucket_name(a) if ":::" in a else table_name(a)
                  for a, l in layers.items() if l == KEEP_LAYER)
    print(f"  keeping config: {', '.join(kept)}")

    if args.dry_run:
        print("\n--dry-run: nothing touched")
        return
    if not args.yes:
        if input(f"\ntype the account id ({DEV_ACCOUNT_ID}) to proceed: ").strip() != DEV_ACCOUNT_ID:
            sys.exit("aborted")

    print()
    for arn, layer in targets:
        if ":table/" in arn:
            name = table_name(arn)
            n = empty_table(session, name)
            print(f"  emptied table  {name}: {n} items")
        else:
            name = bucket_name(arn)
            n = empty_bucket(session, name)
            print(f"  emptied bucket {name}: {n} objects")
    print("\ndone — books are clean; config kept. re-seed to repopulate.")


if __name__ == "__main__":
    main()
