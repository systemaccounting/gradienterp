"""Every terraform apply the operator starts, with the same result on whichever machine runs it.

    bash scripts/apply.sh --stack per_customer --gerp <gerp_id>|all [--action apply|stop] [--source-version V | --build] [--plan]
    bash scripts/apply.sh --stack hub --region <region> [--source-version V | --build] [--plan]
    bash scripts/apply.sh --stack <operator stack> [--plan]

`per_customer` and `hub` run in CodeBuild (`tower-per-customer`, `tower-hub`), from `source.zip`, the
latest `upload.sh source` of any tree, unless `--source-version` names another of its versions or
`--build` zips and uploads this tree first and builds from that version. The upload is the act of
choosing what the fleet builds from — one version serves every apply until the next — so the default
takes what was uploaded and `--build` is the laptop's shortcut, typed. The projects' own location is
`release/source.zip`, what a signup, a hub vend and a closure build from, so nothing started here
moves them.

The operator stacks run terraform in their own dir of the tree this script sits in: init, a plan to a
file, then the apply of that file. The log carries the resource addresses, the `Plan:` line and any
error, as the per-customer buildspec's does. Credentials are the `default` profile: the management
login on the laptop, the role a GitHub job writes there. `platform/management` needs the laptop's
own terraform.tfvars; every other stack reads its values from tracked files.

Builds that aren't started here: a signup's provisioning and a hub vend (`provision_customer`), a
closure's destroy (`closure/begin.py`, after the export and the approval) and a gerp's layer builds
(`modules/cmd`'s `build_layer`). So there is no destroy.
"""

import argparse
import concurrent.futures as cf
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy  # noqa: E402
from deploy import CONFIG, CUSTOMERS_TABLE, REPO, SOURCE_BUCKET, SOURCE_KEY, _boto, _customer_row  # noqa: E402

SELLER_GERP = CONFIG.get("SELLER_GERP", "gradienterp")

# a plan's one line per change (`  # module.x.aws_y.z will be updated in-place`), and not a comment of
# the buildspec's that CodeBuild echoes with the same indent
CHANGE_LINE = re.compile(r"^  # \S+ (will|must) be ")


def summary_line(line):
    return bool(CHANGE_LINE.match(line)) or line.startswith(("Plan:", "No changes"))


# the stacks terraform applies in place, by the name `--stack` takes
OPERATOR_STACKS = {
    "api_openlyoperated": "prod/api_openlyoperated",
    "dns": "prod/dns",
    "email": "prod/email",
    "gradienterp_cloud": "prod/gradienterp_cloud",
    "openlyoperated_biz": "prod/openlyoperated_biz",
    "optimizer": "prod/optimizer/infra",
    "platform/operator": "prod/platform/operator",
    "tower": "prod/tower",
    "platform/management": "prod/platform/management",
}


# ── a gerp's stack: the refusal before a build starts ────────────────────────────────────────
#
# `stop` runs tower-per-customer with TF_ACTION=stop: the export, the row set to `stopped` with its
# stashes removed, then the destroy of per_customer. An apply of a stopped row is the start, ending
# at `active`. The account, the row, the login and the export bucket stay through both; nothing here
# closes an account or vends one. The closure (TF_ACTION=destroy) is not these: it is the owner's, run
# by gradienterp's closure/begin.py, and a closed row is reset by hand.

def teardown_check(row, action):
    """Why a build must not start, or "" when it may. `row` is the gerp's customers row as plain
    values, or None when there is no such row."""
    if not row:
        return f"no row in {CUSTOMERS_TABLE} for that gerp"
    status = row.get("status", "")
    if status in ("closing", "closed"):
        return (f"row is {status}: the closure owns it. To bring it back, delete its closure/close.py "
                "schedule and set it to provisioning by hand first")
    if action == "stop":
        if row.get("gerp_id") == SELLER_GERP:
            return (f"{SELLER_GERP} is the seller's own gerp: its stack carries the closure scripts, "
                    "the hub's trust and the seller's books")
        if status != "active":
            return f"row is {status or 'unset'}; stop takes an active gerp"
    elif action not in ("apply", "plan"):
        return f"no such action: {action}"
    if not row.get("aws_account_id"):
        return "the row has no aws_account_id"
    return ""


def start_build(op, project, env, label, source_key=None, source_version=None):
    """StartBuild on `project` from `source_key` (default `source.zip`) at `source_version` (default its
    latest, looked up and passed so the build records it), then the phases as they change and the
    log's `==>` / `Plan:` lines at the end. Returns the build's final status."""
    cb = op.client("codebuild")
    key = source_key or SOURCE_KEY
    source_version = source_version or op.client("s3").head_object(Bucket=SOURCE_BUCKET, Key=key)["VersionId"]
    build = cb.start_build(projectName=project, sourceLocationOverride=f"{SOURCE_BUCKET}/{key}",
                           sourceVersion=source_version,
                           environmentVariablesOverride=[{"name": k, "value": v, "type": "PLAINTEXT"}
                                                         for k, v in env.items()])["build"]
    bid, t0, phase = build["id"], time.time(), ""
    print(f"==> {label}: build {bid.split(':')[-1]}, source {key} at {source_version}", flush=True)
    while True:
        b = cb.batch_get_builds(ids=[bid])["builds"][0]
        if b.get("currentPhase") != phase:
            phase = b.get("currentPhase")
            print(f"    [{int(time.time() - t0) // 60:02d}:{int(time.time() - t0) % 60:02d}] {phase}", flush=True)
        if b.get("buildComplete"):
            break
        time.sleep(20)
    status = b["buildStatus"]
    print(f"==> {status} after {int(time.time() - t0) // 60}m{int(time.time() - t0) % 60:02d}s")
    logs, group, stream = op.client("logs"), b.get("logs", {}).get("groupName"), b.get("logs", {}).get("streamName")
    if group and stream:
        kw, token = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}, None
        while True:
            r = logs.get_log_events(**kw)
            for e in r.get("events", []):
                line = e["message"].rstrip()
                if (line.startswith("==>") or summary_line(line) or "complete!" in line
                        or line.startswith(("marked ", "closed ", "stopped ")) or "Error" in line or "ERROR" in line):
                    print("    " + line[:200])
            if r.get("nextForwardToken") in (None, token):
                break
            token = kw["nextToken"] = r["nextForwardToken"]
    return status


def _run_build(op, gerp_id, account, action, region, source_key=None, source_version=None):
    """tower-per-customer for one gerp. `region` is the gerp's, off its row: the buildspec defaults to
    us-east-1 without it, and a gerp built elsewhere then plans against the wrong region."""
    return start_build(op, "tower-per-customer",
                       {"CUSTOMER_ID": gerp_id, "CUSTOMER_ACCOUNT_ID": account, "CUSTOMER_REGION": region,
                        "TF_ACTION": action},
                       f"{action} {gerp_id} ({account})", source_key, source_version)


def apply_gerp(op, gerp_id, action, source_version):
    """One gerp's build, refused first by its row; returns whether it ended where the action leaves it."""
    row = _customer_row(op, gerp_id)
    why = teardown_check(row, action)
    if why:
        print(f"refused {action} of {gerp_id}: {why}")
        return False
    starting = action == "apply" and row.get("status") == "stopped"
    status = _run_build(op, gerp_id, row["aws_account_id"], action, row.get("region") or "us-east-1",
                        source_version=source_version)
    if action == "plan":
        return status == "SUCCEEDED"
    row = _customer_row(op, gerp_id) or {}
    want = "stopped" if action == "stop" else "active"
    print(f"row: {row.get('status')}" + (f", stopped_at {row.get('stopped_at')}" if action == "stop" and row.get("stopped_at") else ""))
    if starting and row.get("status") == "active":
        print("the settings went with the stack: the onboarding walk runs again from the chat door")
    return status == "SUCCEEDED" and row.get("status") == want


def build_source(op, args):
    """`--build`: zip this tree, upload it, and name that version for the build — the two steps the
    laptop otherwise types, and the version pinned so a concurrent upload cannot slip in between."""
    if not args.build:
        return args.source_version
    if args.source_version:
        sys.exit("--build and --source-version name two different trees; pass one")
    subprocess.run(["bash", os.path.join(REPO, "scripts", "zip.sh"), "source"], check=True)
    return deploy.upload_source(op.client("s3"), release=False)


def cmd_per_customer(args):
    op = _boto(args.operator_profile)
    action = "plan" if args.plan else args.action
    args.source_version = build_source(op, args)
    if not args.gerp:
        sys.exit("--stack per_customer takes --gerp <gerp_id> or --gerp all")
    if args.gerp != "all":
        sys.exit(0 if apply_gerp(op, args.gerp, action, args.source_version) else 1)
    gerps = [r["gerp_id"] for r in deploy._active_rows(op)]

    def one(g):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = apply_gerp(op, g, action, args.source_version)
        return g, ok, buf.getvalue()

    # every gerp's build at once, each one's log printed whole as it finishes
    failed = []
    with cf.ThreadPoolExecutor(max_workers=max(1, len(gerps))) as ex:
        for g, ok, out in ex.map(one, gerps):
            print(out, end="")
            if not ok:
                failed.append(g)
    if failed:
        sys.exit(f"{len(failed)} of {len(gerps)} gerps did not end where {action} leaves them: {', '.join(failed)}")


def cmd_hub(args):
    hub = CONFIG["HUBS"].get(args.region or "")
    if not hub:
        sys.exit(f"--stack hub takes --region, one of config.json HUBS: {', '.join(CONFIG['HUBS'])}")
    op = _boto(args.operator_profile)
    action = "plan" if args.plan else "apply"
    args.source_version = build_source(op, args)
    status = start_build(op, "tower-hub", {"HUB_ID": args.region, "HUB_ACCOUNT_ID": hub["account"],
                                           "HUB_REGION": hub["region"], "TF_ACTION": action},
                         f"{action} hub {args.region} ({hub['account']})", source_version=args.source_version)
    sys.exit(0 if status == "SUCCEEDED" else 1)


def _terraform(argv, cwd, env):
    return subprocess.run(["terraform", *argv], cwd=cwd, env=env, capture_output=True, text=True)


def _failed(proc):
    """What a failed plan or apply prints: terraform's errors (stderr) and the change lines. A plan's
    stdout carries attribute values, and on GitHub this log is public."""
    kept = [ln for ln in proc.stdout.splitlines() if summary_line(ln)]
    print("\n".join(kept + [proc.stderr.rstrip()]))


def apply_stack(stack_dir, plan_only, profile="default"):
    """init, plan to a file, apply that file, in `stack_dir` of this tree. Returns the exit code."""
    cwd = os.path.join(REPO, stack_dir)
    env = {**os.environ, "AWS_PROFILE": profile}
    init = _terraform(["init", "-input=false", "-no-color"], cwd, env)
    if init.returncode != 0:
        print(init.stdout + init.stderr)
        return init.returncode
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = os.path.join(tmp, "tf.plan")
        print(f"==> terraform plan: {stack_dir}", flush=True)
        plan = _terraform(["plan", "-input=false", "-no-color", f"-out={plan_file}"], cwd, env)
        if plan.returncode != 0:
            _failed(plan)
            return plan.returncode
        lines = [ln for ln in plan.stdout.splitlines() if summary_line(ln)]
        print("\n".join(lines))
        if plan_only or any(ln.startswith("No changes") for ln in lines):
            return 0
        # Nothing waits for approval: whoever started the apply already decided. If that changes,
        # the plan is on screen above and this is where the apply would wait for a yes.
        #
        # if input(f"apply {stack_dir}? [y/N] ").strip().lower() != "y":
        #     print("not applied")
        #     return 1
        print(f"==> terraform apply: {stack_dir}", flush=True)
        applied = _terraform(["apply", "-input=false", "-no-color", plan_file], cwd, env)
        if applied.returncode != 0:
            _failed(applied)
        else:
            print("\n".join(ln for ln in applied.stdout.splitlines() if "complete!" in ln))
        return applied.returncode


def main():
    ap = argparse.ArgumentParser(description="every terraform apply the operator starts")
    ap.add_argument("--stack", required=True, choices=["per_customer", "hub", *OPERATOR_STACKS])
    ap.add_argument("--env", default="prod", choices=["prod"], help="prod, until the integ organization exists")
    ap.add_argument("--plan", action="store_true", help="stop after the plan")
    ap.add_argument("--gerp", help="per_customer: a gerp id, or all (every active gerp, at once)")
    ap.add_argument("--action", default="apply", choices=["apply", "stop"],
                    help="per_customer: apply (a stopped row's is the start) or stop")
    ap.add_argument("--region", help="hub: the hub's region, a key of config.json HUBS")
    ap.add_argument("--source-version", help="per_customer, hub: a version of source.zip (default: its latest)")
    ap.add_argument("--build", action="store_true",
                    help="per_customer, hub: zip and upload this tree first, then build from that version")
    ap.add_argument("--operator-profile", default="operator-org")
    args = ap.parse_args()
    if args.stack == "per_customer":
        cmd_per_customer(args)
    elif args.stack == "hub":
        cmd_hub(args)
    else:
        sys.exit(apply_stack(OPERATOR_STACKS[args.stack], args.plan))


if __name__ == "__main__":
    main()
