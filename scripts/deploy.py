"""Artifact deploys — TF owns shape, S3 owns bytes.

The build recipe stays terraform's: each module's archive_file writes
modules/<m>/infra/.build/<fn>.zip during a refreshless plan, and this script
pushes THOSE zips. The fleet self-describes via the `gerp:src-dir` lambda tag
(one query = membership + which dir builds each function). Deployed state is
never recorded anywhere — it's read live from CodeSha256.

verbs (run via `bash scripts/deploy.sh …`):

  status [--gerp G] [--profile P]
      tag query × get-function CodeSha256 × artifact latest-version checksum.
      states: in-sync / artifact-ahead (pushed, not deployed) / repo-ahead
      (local zip differs from artifact — push needed) / no-artifact.

  push [--gerp G] [--dirs modules/x/lambdas/y ...] [--notes "..."] [--profile P]
      refresh .build zips (terraform plan -refresh=false), then for each fleet
      function whose zip sha differs from the artifact's latest version:
      put-object (new version, sha256 checksum) + provenance annotation
      {src_dir, src_sha256, built_at} + release annotation (--notes; the
      agent-readable "what's in this version") + update-function-code from
      that exact version, then wait for LastUpdateStatus.

  stop --gerp <gerp_id> / start --gerp <gerp_id>
      a gerp's stack between sessions: `stop` runs tower-per-customer with
      TF_ACTION=stop (the export, the row set to stopped, the destroy) and
      `start` runs the apply into the stopped row. The account and the row
      stay; the closure is not this (see the section above cmd_stop).

Cross-account: the artifact bucket is operator-side org-read; functions live in
the gerp's account. `--gerp` names the gerp (default gradienterp) and `--profile`
the profile that reaches it (default `gerp-<gerp>`, written by
`bash scripts/awsacct.sh --all`). Before anything is built or uploaded, the
profile's account has to be the one on the gerp's row: a profile pointing
elsewhere — `current` switched by another session — is refused with both ids.
"""

import argparse
import ast
import base64
import concurrent.futures as cf
import hashlib
import io
import json
import os
import sys
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = json.load(open(os.path.join(REPO, "config.json")))
BUCKET = f"{CONFIG['STACK_PREFIX']}-artifacts-{CONFIG['OPERATOR_ACCOUNT_ID']}"   # us-east-1's; the others carry the region


def bucket_for(region: str) -> str:
    """Lambda takes its package from a bucket in its own region: the first region's is the bare
    name, every other region's is suffixed (prod/tower regions.tf, modules/terraform/lambda)."""
    return BUCKET if not region or region == "us-east-1" else f"{BUCKET}-{region}"

# the gradienterp.cloud owner web app: the BFF lambda serves the bundled SPA, so a web change is a
# lambda code change. It sources from the artifact bucket like every module lambda (main.tf:
# data.aws_s3_object.bff), and lives in the OPERATOR account (function + bucket both there).
# A deployable unit is the directory holding the app, so this is the BFF's src-dir: it is what the
# function's `gerp:src-dir` tag says, what `--dirs` takes, and what provenance records. The bundle
# reaches one level up for the web/ files the lambda serves — a detail of THIS unit's build, which
# `build_artifact` routes to `build_webapp`.
WEBAPP_DIR = "prod/gradienterp_cloud/bff"
WEBAPP_ROOT = os.path.dirname(WEBAPP_DIR)
BFF_FN = f"{CONFIG['STACK_PREFIX']}-cloud-bff"
BFF_KEY = f"{WEBAPP_ROOT}/bff.zip"  # terraform reads this key; it does not track the src-dir
# purchase-terms.txt is not optional: `_html` inlines it into EVERY page, so a bundle without it
# 500s the whole site rather than one route.
BFF_FILES = ["index.html", "support.html", "card.html", "paid.html", "app.js", "vendor/lit-html.js", "art/gradientERP-lockup.svg", "llms.txt",
             "robots.txt", "purchase-terms.txt"]
TAG_KEY = "gerp:src-dir"


def _boto(profile):
    import boto3
    return boto3.Session(profile_name=profile)


def fleet(session):
    """{function_name: src_dir} from the one tag query."""
    tagging = session.client("resourcegroupstaggingapi")
    out = {}
    token = ""
    while True:
        resp = tagging.get_resources(
            TagFilters=[{"Key": TAG_KEY}],
            ResourceTypeFilters=["lambda:function"],
            PaginationToken=token,
        )
        for r in resp["ResourceTagMappingList"]:
            name = r["ResourceARN"].rsplit(":", 1)[1]
            src = next(t["Value"] for t in r["Tags"] if t["Key"] == TAG_KEY)
            out[name] = src
        token = resp.get("PaginationToken", "")
        if not token:
            return out


# ─── the builder — no terraform anywhere in the push path ───
#
# A lambda's bundle is declared by its own code: the import graph IS the
# manifest. python zips = the src dir's .py files + every transitively
# resolved local import (searched: src dir → lambdas root → module root →
# any module's shared dirs). node zips = the whole dir (package.json marks
# it), node_modules included. Anything else: exit 1, wth is this.
# Zips are deterministic (sorted entries, zeroed timestamps) so an unchanged
# source builds an identical artifact and push no-ops.

PROVIDED = set(sys.stdlib_module_names) | {
    "boto3", "botocore",           # the runtime ships them
    "dateutil", "urllib3", "six", "jmespath", "s3transfer",  # boto3's vendored deps, importable in-runtime
}
EXCLUDE_DIRS = {"__pycache__", ".DS_Store"}


def _local_imports(path, lambdas_root, module_root, seen):
    src_dir = os.path.dirname(path)
    tree = ast.parse(open(path).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    import glob as _g
    for n in sorted(names - PROVIDED):
        if n in seen:
            continue
        candidates = [os.path.join(src_dir, n + ".py"),
                      os.path.join(lambdas_root, n + ".py"),
                      os.path.join(module_root, n + ".py")]
        candidates += sorted(_g.glob(os.path.join(REPO, "modules", "*", n + ".py")))
        candidates += sorted(_g.glob(os.path.join(REPO, "modules", "*", "lambdas", "*", n + ".py")))
        hits = [c for c in candidates if os.path.isfile(c)]
        # the cross-module glob tiers may only ever match ONE file — two modules both
        # exporting a shareable <n>.py would bundle whichever sorts first, silently.
        # first two tiers (src dir, lambdas root) are positional and can shadow freely.
        #
        # ...and if a positional tier HIT, ambiguity further out is not ambiguity: the local file
        # wins by position, which is exactly what several lambdas each carrying their own
        # `provider_stripe.py` relies on. Only complain when nothing local settled it.
        positional = [c for c in candidates[:3] if os.path.isfile(c)]
        globbed = [h for h in hits if h not in candidates[:3]]
        if not positional and len(set(globbed)) > 1:
            raise RuntimeError(f"{os.path.relpath(path, REPO)}: import '{n}' is AMBIGUOUS "
                               f"across modules: {sorted(set(globbed))} — add a match case")
        hit = hits[0] if hits else None
        if hit is None:
            # vendored third-party (modules/<m>/vendor/<pkg>) — the code imports it after a
            # ./vendor sys.path insert; bundle the whole self-contained vendor tree, no recursion.
            vend = os.path.join(module_root, "vendor")
            if os.path.isdir(os.path.join(vend, n)) or os.path.isfile(os.path.join(vend, n + ".py")):
                seen["__vendor__"] = vend
                continue
            raise RuntimeError(f"{os.path.relpath(path, REPO)}: unresolvable import '{n}'")
        seen[n] = os.path.realpath(hit)
        _local_imports(seen[n], lambdas_root, module_root, seen)
    return seen


def _zip_deterministic(entries):
    """entries: {arcname: abs_path | bytes}, sorted, zeroed dates → byte-stable zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc in sorted(entries):
            zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            zi.external_attr = 0o644 << 16
            src = entries[arc]
            zf.writestr(zi, src if isinstance(src, bytes) else open(src, "rb").read())
    return buf.getvalue()


def botocore_models(*services):
    """The current service models for `services`, from this checkout's botocore, as zip entries
    under botocore_data/ — for a lambda whose runtime boto3 predates a field it sends. The
    function's env sets AWS_DATA_PATH=/var/task/botocore_data and botocore reads these first.
    Gzipped models are written out plain so an older loader reads them too."""
    import glob as _g
    import gzip
    import botocore
    root = os.path.join(os.path.dirname(botocore.__file__), "data")
    out = {}
    for svc in services:
        for path in sorted(_g.glob(os.path.join(root, svc, "*", "*"))):
            rel = os.path.relpath(path, root)
            if rel.endswith(".gz"):
                out["botocore_data/" + rel[:-3]] = gzip.open(path, "rb").read()
            else:
                out["botocore_data/" + rel] = path
    return out


def build_node(root):
    """Node lambda: zip the whole dir (node_modules included), hygiene-excluded."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for f in sorted(filenames):
            if f in EXCLUDE_DIRS:
                continue
            p = os.path.join(dirpath, f)
            entries[os.path.relpath(p, root)] = p
    return _zip_deterministic(entries)


def build_py(root, extra=None):
    """Python lambda: the dir's .py files + the transitively-resolved local import
    graph (+ a module's vendor/ tree when an import lands there). `extra`:
    {arcname: abs_path} a per-path case adds — a data file the code opens."""
    lambdas_root = os.path.dirname(root)
    module_root = os.path.dirname(lambdas_root)
    entries = {f: os.path.join(root, f) for f in sorted(os.listdir(root)) if f.endswith(".py")}
    seen = {}
    for p in list(entries.values()):
        _local_imports(p, lambdas_root, module_root, seen)
    entries.update(extra or {})
    vend = seen.pop("__vendor__", None)
    for n, p in seen.items():
        entries.setdefault(n + ".py", p)
    if vend:
        for dirpath, dirnames, filenames in os.walk(vend):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for f in sorted(filenames):
                if f.endswith(".py"):
                    pth = os.path.join(dirpath, f)
                    entries["vendor/" + os.path.relpath(pth, vend)] = pth
    return _zip_deterministic(entries)


def build_artifact(src_dir):
    """Deterministic zip bytes for one lambda src dir: pick the builder, run any
    hardcoded per-path prep (it's a build script; explicit cases at any count are
    fine), build."""
    root = os.path.join(REPO, src_dir)

    if os.path.exists(os.path.join(root, "package.json")):
        build = build_node
    elif any(f.endswith(".py") for f in os.listdir(root)):
        build = build_py
    else:
        sys.exit(f"{src_dir}: neither package.json nor .py — wth is this")

    match src_dir:
        case _ if src_dir == WEBAPP_DIR:
            # The BFF's tag names this dir, but its bundle is main.py PLUS the web/ files it
            # serves from /var/task/web — one directory up. Built generically it comes out
            # without them, uploads to the same key `_push_webapp` writes, and the next apply
            # pins the function to a bundle whose every page 500s.
            return build_webapp()
        # case "modules/agent/lambdas/chat":
        #     prune_node_dev_files(root)   # do things first; `build` is already chosen
        case "modules/mcp/lambdas/manage_mcp":
            # the vendor catalog is modules/mcp's PR-editable data file; the door reads it from
            # data/providers.json inside its zip. The AgentCore models ride too: the runtime's
            # boto3 lags the fields the door sends (clientAuthenticationMethod).
            return build_py(root, {"data/providers.json": os.path.join(REPO, "modules/mcp/data/providers.json"),
                                   **botocore_models("bedrock-agentcore-control", "bedrock-agentcore")})
        case "modules/mcp/lambdas/complete_mcp_auth":
            return build_py(root, botocore_models("bedrock-agentcore"))
        case "modules/automation/lambdas/automate":
            # automate reads the catalog's prefixes: the ones that route a tool to the vendors' gateway
            return build_py(root, {"data/providers.json": os.path.join(REPO, "modules/mcp/data/providers.json")})
        case _:
            pass

    return build(root)


def sha256_b64_bytes(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def src_tree_sha(src_dir):
    """Hash of the source dir's tracked content (order-stable) — provenance, not the zip."""
    h = hashlib.sha256()
    root = os.path.join(REPO, src_dir)
    for dirpath, dirnames, filenames in sorted(os.walk(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "node_modules"))
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


def replicated(s3, key, zip_sha, bucket, seconds=90):
    """The region bucket's latest version once replication has carried the checksum pushed; the
    version id there is its own. None when it has not arrived in time — an older checksum under
    the same key is the previous push, never deployed as this one."""
    deadline = time.time() + seconds
    while True:
        v, sha = artifact_head(s3, key, bucket)
        if sha == zip_sha:
            return v
        if time.time() >= deadline:
            return None
        time.sleep(3)


def artifact_head(s3, key, bucket=BUCKET):
    """(version_id, sha256_b64) of the latest artifact version, or (None, None)."""
    try:
        attrs = s3.get_object_attributes(Bucket=bucket, Key=key,
                                         ObjectAttributes=["Checksum"])
        return attrs.get("VersionId"), attrs.get("Checksum", {}).get("ChecksumSHA256")
    except s3.exceptions.NoSuchKey:
        return None, None
    except Exception as e:
        if "NoSuchKey" in str(e) or "NotFound" in str(e):
            return None, None
        raise


def deployed_shas(lam):
    """{function_name: CodeSha256} for every function in the account, in one paginated walk.

    `CodeSha256` is the only field either caller wants, and `ListFunctions` carries it, 50 per page —
    so a 135-function fleet is 3 requests instead of 135. Lambda's MANAGEMENT quota is account-wide
    and far below the invoke quota, so a per-function fan-out throttles: `status` run right after a
    fleet `push` died with TooManyRequestsException after botocore had already retried four times.

    Reads `$LATEST`, which is what both callers compare against."""
    out = {}
    for page in lam.get_paginator("list_functions").paginate():
        for f in page["Functions"]:
            out[f["FunctionName"]] = f["CodeSha256"]
    return out


def sync_state(deployed, artifact, local):
    """The three-way compare a row reports: what the repo builds, what the bucket holds, and what
    the function runs. `artifact-ahead` is the one that matters most — it says the bucket and the
    function disagree, which is what a terraform apply resolves, in the bucket's favour."""
    if artifact is None:
        return "no-artifact"
    if deployed is None:
        return "no-function"   # tagged, but not in the account — deleted, or never applied
    if deployed == artifact:
        return "in-sync" if (local in (None, artifact)) else "repo-ahead"
    if local == artifact:
        return "artifact-ahead"  # pushed but this function not updated
    return "repo-ahead"


def target_refusal(gerp_id, row, caller_account):
    """Why a push or status for `gerp_id` must not run through a profile answering from
    `caller_account`, or "" when it may."""
    if not row:
        return f"no row in gerp-customers for {gerp_id}"
    account = row.get("aws_account_id")
    if not account or account == "None":
        return f"{gerp_id}'s row has no aws_account_id"
    if caller_account != account:
        return (f"the profile answers from {caller_account}, and {gerp_id} is in {account} — "
                f"`bash scripts/awsacct.sh --all` writes gerp-{gerp_id}")
    return ""


def _target_session(args):
    """The session for `--gerp`, through `--profile` (default `gerp-<gerp>`), once its account is the
    one on the gerp's row."""
    args.profile = args.profile or f"gerp-{args.gerp}"
    session = _boto(args.profile)
    caller = session.client("sts").get_caller_identity()["Account"]
    why = target_refusal(args.gerp, _customer_row(_boto(args.operator_profile), args.gerp), caller)
    if why:
        sys.exit(f"refused: {why}")
    return session


def cmd_status(args):
    session = _target_session(args)
    # the gerp's region is the profile's: its functions take their packages from the region's
    # bucket, which S3 replication fills from the us-east-1 one every push writes (prod/tower
    # regions.tf); the deploy step waits for the replica to carry what was pushed
    region = session.region_name or "us-east-1"
    bucket = bucket_for(region)
    fns = fleet(session)
    lam = session.client("lambda")
    s3 = session.client("s3")
    shas = deployed_shas(lam)

    def one(item):
        # The pool still earns its place for the two per-function costs left here: an S3 HeadObject,
        # whose limits are orders of magnitude above Lambda's, and a local zip build, which is CPU.
        name, src = item
        _, art = artifact_head(s3, src + ".zip")
        return name, src, sync_state(shas.get(name), art, sha256_b64_bytes(build_artifact(src)))

    def webapp_row():
        """The BFF answers to no tag query here: it lives in the OPERATOR account while the fleet
        this walks lives in the customer's, so without an explicit row nothing ever compares it.
        Read with operator creds; an unreadable row is PRINTED rather than dropped, since a
        silently missing row is the blind spot itself."""
        try:
            op = _boto(args.operator_profile)
            _, art = artifact_head(op.client("s3"), BFF_KEY)
            deployed = op.client("lambda").get_function_configuration(
                FunctionName=BFF_FN)["CodeSha256"]
            return BFF_FN, WEBAPP_DIR, sync_state(deployed, art, sha256_b64_bytes(build_webapp()))
        except Exception as e:  # noqa: BLE001
            return BFF_FN, WEBAPP_DIR, f"unreadable ({type(e).__name__})"

    rows = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        rows = list(ex.map(one, sorted(fns.items()))) + [webapp_row()]
    width = max(len(r[0]) for r in rows)
    counts = {}
    for name, src, state in rows:
        counts[state] = counts.get(state, 0) + 1
        if state != "in-sync" or args.all:
            print(f"{name:<{width}}  {state:<14}  {src}")
    print("--")
    print("  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))


def build_webapp():
    """The BFF bundle: bff/main.py + its shared-lib imports at root, and the web/ files the lambda
    serves from /var/task/web.

    The import walk is the same one `build_py` runs, seeded from `bff/main.py` alone — the local
    runner lives in `tests/server/bff/` and does not ship."""
    d = os.path.join(REPO, WEBAPP_ROOT)
    entries = {"main.py": os.path.join(d, "bff/main.py")}
    seen = {}
    _local_imports(entries["main.py"], os.path.join(d, "bff"), d, seen)
    seen.pop("__vendor__", None)
    for n, p in seen.items():
        entries.setdefault(n + ".py", p)
    for f in BFF_FILES:
        entries[f"web/{f}"] = os.path.join(d, "web", f)
    return _zip_deterministic(entries)


def _push_webapp(args):
    """The gradienterp.cloud owner web app is a special src-dir: the BFF lambda serves the bundled
    SPA, so a web change is a lambda code change. Same shape as the fleet push (build → artifact
    bucket → update-function-code), but the BFF + bucket both live in the OPERATOR account, so
    operator creds do both. tf sources from the bucket, so a later apply is a pointer-sync no-op."""
    op = _boto(args.operator_profile)
    s3, lam = op.client("s3"), op.client("lambda")
    blob = build_webapp()
    zip_sha = sha256_b64_bytes(blob)
    _, art_sha = artifact_head(s3, BFF_KEY)
    deployed = lam.get_function_configuration(FunctionName=BFF_FN)["CodeSha256"]
    if zip_sha == art_sha == deployed:
        print(f"ok   {BFF_FN}: already current")
        return
    if zip_sha != art_sha:
        version = s3.put_object(Bucket=BUCKET, Key=BFF_KEY, Body=blob,
                                ChecksumAlgorithm="SHA256")["VersionId"]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        prov = json.dumps({"src_dir": WEBAPP_DIR, "built_at": now, "zip_sha256": zip_sha})
        s3.put_object_annotation(Bucket=BUCKET, Key=BFF_KEY, VersionId=version,
                                 AnnotationName="provenance", AnnotationPayload=prov.encode())
        if args.notes:
            s3.put_object_annotation(Bucket=BUCKET, Key=BFF_KEY, VersionId=version,
                                     AnnotationName="release", AnnotationPayload=args.notes.encode())
    else:
        version = artifact_head(s3, BFF_KEY)[0]
    if deployed != zip_sha:
        lam.update_function_code(FunctionName=BFF_FN, S3Bucket=BUCKET, S3Key=BFF_KEY,
                                 S3ObjectVersion=version)
        lam.get_waiter("function_updated_v2").wait(FunctionName=BFF_FN)
        print(f"PUSH {BFF_FN}: version {version[:12]}… deployed")
    else:
        print(f"push {BFF_FN}: artifact refreshed, function already current")


def cmd_push(args):
    dirs = list(args.dirs) if args.dirs else None
    # the gerp is checked before anything builds or uploads, the BFF included; a push of the BFF
    # alone reaches only the operator account and names no gerp
    session = None if dirs is not None and set(dirs) <= {WEBAPP_DIR} else _target_session(args)
    # The owner web app is outside the fleet enumeration below — `fleet()` is a tag query scoped to
    # the session's account and the BFF lives in the operator's, and its bundle needs the web/ files
    # `build_py` knows nothing about. So a bare push has to reach it EXPLICITLY: without this it
    # walks 131 functions, omits the 132nd, and says it pushed the fleet.
    if dirs is None or WEBAPP_DIR in dirs:
        _push_webapp(args)
        if dirs is not None:
            dirs = [d for d in dirs if d != WEBAPP_DIR]
            if not dirs:
                return
    # the gerp's region is the profile's: its functions take their packages from the region's
    # bucket, which S3 replication fills from the us-east-1 one every push writes (prod/tower
    # regions.tf); the deploy step waits for the replica to carry what was pushed
    region = session.region_name or "us-east-1"
    bucket = bucket_for(region)
    fns = fleet(session)
    # Two orthogonal phases, both on unless one is explicitly asked for (default = both, the
    # historical behavior): PUSH = build + upload the artifact to the bucket (the versioned
    # truth; works for any src-dir, new or existing, no function needed); DEPLOY = point the
    # live function at the bucket's current artifact (update-function-code; needs the function).
    # `--push` alone stages/seeds; `--deploy` alone re-pins bucket truth without a rebuild.
    do_push = args.push or not (args.push or args.deploy)
    do_deploy = args.deploy or not (args.push or args.deploy)

    src_to_name = {s: n for n, s in fns.items()}
    targets = sorted(set(dirs)) if dirs is not None else sorted(fns.values())

    lam = session.client("lambda")
    # One listing beats one call per target once there is more than a handful of them, and a fleet
    # push is 135. A `--dirs` push of one or two stays cheaper asking directly.
    shas = deployed_shas(lam) if do_deploy and len(targets) > 2 else None
    # bucket WRITES are operator-only (bucket-owner IAM; the org policy grants read only) —
    # tenant creds fan the update-function-code, operator creds put the artifacts.
    s3 = _boto(args.operator_profile).client("s3")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def upload(src, blob, zip_sha):
        """Put the artifact + provenance (and release, if --notes) annotations; return version id."""
        key = src + ".zip"
        version = s3.put_object(Bucket=BUCKET, Key=key, Body=blob, ChecksumAlgorithm="SHA256")["VersionId"]
        prov = json.dumps({"src_dir": src, "src_sha256": src_tree_sha(src),
                           "built_at": now, "zip_sha256": zip_sha})
        s3.put_object_annotation(Bucket=BUCKET, Key=key, VersionId=version,
                                 AnnotationName="provenance", AnnotationPayload=prov.encode())
        if args.notes:
            s3.put_object_annotation(Bucket=BUCKET, Key=key, VersionId=version,
                                     AnnotationName="release", AnnotationPayload=args.notes.encode())
        return version

    def work(src):
        name = src_to_name.get(src)          # None → not deployed yet (new lambda)
        key = src + ".zip"
        cur_version, art_sha = artifact_head(s3, key)   # current bucket artifact (None if never pushed)
        parts = []

        if do_push:
            blob = build_artifact(src)
            zip_sha = sha256_b64_bytes(blob)
            if zip_sha != art_sha:
                cur_version = upload(src, blob, zip_sha)
                art_sha = zip_sha
                parts.append(f"pushed {cur_version[:12]}…")
            else:
                parts.append("bucket current")
        if bucket != BUCKET and art_sha is not None:
            # a gerp elsewhere deploys from its region's replica, under that bucket's version id
            cur_version = replicated(s3, key, art_sha, bucket)
            if cur_version is None:
                parts.append(f"not yet replicated to {bucket}")
                return f"{name or src}: {', '.join(parts)}"

        if do_deploy:
            if name is None or (shas is not None and name not in shas):
                parts.append("no function yet — apply to create")
            elif art_sha is None:
                parts.append("no artifact in bucket — push first")
            else:
                deployed = (shas.get(name) if shas is not None
                            else lam.get_function_configuration(FunctionName=name)["CodeSha256"])
                if deployed != art_sha:
                    lam.update_function_code(FunctionName=name, S3Bucket=bucket, S3Key=key,
                                             S3ObjectVersion=cur_version)
                    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
                    parts.append(f"deployed {cur_version[:12]}…")
                else:
                    parts.append("function current")

        return f"{name or src}: {', '.join(parts)}"

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for line in ex.map(work, targets):
            print(line)


def _gerp_sessions(op, only=None):
    """(gerp_id, account_id, session) for every gerp that runs: an `active` row with a real
    account — the stub rows the BFF makes with vending off have none — assumed into through
    OperatorOrchestration off the operator session. `only` limits it to one gerp."""
    ddb = op.client("dynamodb")
    rows, kw = [], {"TableName": "gerp-customers",
                    "FilterExpression": "#s = :a AND attribute_exists(aws_account_id)",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":a": {"S": "active"}}}
    while True:
        resp = ddb.scan(**kw)
        rows += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    import boto3
    for r in sorted(rows, key=lambda r: r["gerp_id"]["S"]):
        gerp_id, account = r["gerp_id"]["S"], r["aws_account_id"]["S"]
        if only and gerp_id != only:
            continue
        c = op.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/OperatorOrchestration",
                                         RoleSessionName=f"deploy-image-{gerp_id}"[:64])["Credentials"]
        yield gerp_id, account, boto3.Session(aws_access_key_id=c["AccessKeyId"],
                                              aws_secret_access_key=c["SecretAccessKey"],
                                              aws_session_token=c["SessionToken"])


def _update_runtimes(ctl, digest, label):
    """Every runtime in one account onto `digest`, its named endpoints re-pinned."""
    for rt in ctl.list_agent_runtimes()["agentRuntimes"]:
        rid = rt["agentRuntimeId"]
        cur = ctl.get_agent_runtime(agentRuntimeId=rid)
        new_uri = f"{CONFIG['OPERATOR_ACCOUNT_ID']}.dkr.ecr.us-east-1.amazonaws.com/agentcore@{digest}"
        if cur.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri") == new_uri:
            print(f"    {label}: runtime {rt['agentRuntimeName']} already on this image")
            continue
        # UpdateAgentRuntime REPLACES config wholesale — carry every field get returns
        # that update accepts, changing ONLY the image. Dropping one (e.g. the env vars)
        # ships a container that can't boot.
        kw = {"agentRuntimeId": rid,
              "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": new_uri}},
              "roleArn": cur["roleArn"],
              "networkConfiguration": cur["networkConfiguration"]}
        for f in ("description", "protocolConfiguration", "environmentVariables",
                  "authorizerConfiguration", "requestHeaderConfiguration", "lifecycleConfiguration"):
            if cur.get(f):
                kw[f] = cur[f]
        upd = ctl.update_agent_runtime(**kw)
        version = upd["agentRuntimeVersion"]
        print(f"    {label}: runtime {rt['agentRuntimeName']} → version {version}")
        for _ in range(60):  # the new version must reach READY before an endpoint can pin it
            st = ctl.get_agent_runtime(agentRuntimeId=rid, agentRuntimeVersion=version)["status"]
            if st == "READY":
                break
            time.sleep(5)
        else:
            sys.exit(f"{label}: runtime version {version} never reached READY")
        for ep in ctl.list_agent_runtime_endpoints(agentRuntimeId=rid)["runtimeEndpoints"]:
            if ep["name"] == "DEFAULT":
                continue  # DEFAULT auto-tracks agent updates; only named endpoints pin
            ctl.update_agent_runtime_endpoint(agentRuntimeId=rid,
                                              endpointName=ep["name"],
                                              agentRuntimeVersion=version)
            print(f"      endpoint {ep['name']} → pinned {version}")


def cmd_image(args):
    """Build → push the agent image (auto-incremented vNN tag; immutable-tag history) →
    CLI-update a gerp's runtime to the new digest → re-pin its endpoint to the new runtime
    version. No terraform: the runtime's tf config reads most_recent from ECR, so the next apply
    of any gerp agrees with what this just did.

    Which gerp: the operator's own by default (`--gerp gradienterp`) — an image lands on the
    dogfood first, then `--gerp westwood-…` for the staging pair, then `--all` for every active
    gerp. A fleet-wide push is said, never implied. `--no-build` skips the build and moves the
    named gerps onto the image already at the top of ECR."""
    import subprocess
    op = _boto(args.operator_profile)
    ecr = op.client("ecr")

    # next immutable version tag from the repo's own history
    tags = []
    token = None
    while True:
        kw = {"repositoryName": "agentcore", "maxResults": 1000}
        if token:
            kw["nextToken"] = token
        resp = ecr.describe_images(**kw)
        for img in resp["imageDetails"]:
            tags += [t for t in img.get("imageTags", []) if t.startswith("v") and t[1:].isdigit()]
        token = resp.get("nextToken")
        if not token:
            break
    latest = max((int(t[1:]) for t in tags), default=0)
    if args.no_build:
        tag = f"v{latest}"
        print(f"==> walking the fleet onto {tag} (no build)")
    else:
        tag = f"v{latest + 1}"
        uri = f"{CONFIG['OPERATOR_ACCOUNT_ID']}.dkr.ecr.us-east-1.amazonaws.com/agentcore:{tag}"
        print(f"==> building + pushing {tag}")
        for cmd in (["bash", "scripts/docker.sh", "--build"],
                    ["bash", "scripts/docker.sh", "--push", uri]):
            r = subprocess.run(cmd, cwd=REPO, env={**os.environ, "AWS_PROFILE": args.operator_profile},
                               capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"{' '.join(cmd)} failed:\n{r.stderr[-1500:]}")
    digest = ecr.describe_images(repositoryName="agentcore",
                                 imageIds=[{"imageTag": tag}])["imageDetails"][0]["imageDigest"]
    print(f"==> {tag} = {digest}")

    walked = 0
    for gerp_id, account, ses in _gerp_sessions(op, only=None if args.all else args.gerp):
        print(f"==> {gerp_id} ({account})")
        _update_runtimes(ses.client("bedrock-agentcore-control"), digest, gerp_id)
        walked += 1
    if not args.all and not walked:
        sys.exit(f"no active gerp named {args.gerp} with an account")
    print(f"deployed {tag} to {walked} gerp(s) — a warm chat session stays on the old container; fresh sessions get {tag}")


# ── stop / start: a gerp's stack between sessions ────────────────────────────────────────────
#
# `stop` runs tower-per-customer with TF_ACTION=stop: the export, the row set to `stopped` with its
# stashes removed, then the destroy of per_customer. `start` runs the apply that finds a stopped
# row and ends at `active`. The account, the row, the login and the export bucket stay through both;
# nothing here closes an account or vends one. The closure (TF_ACTION=destroy) is not these: it is
# the owner's, run by gradienterp's closure/begin.py, and a closed row is reset by hand.

SELLER_GERP = CONFIG.get("SELLER_GERP", "gradienterp")
CUSTOMERS_TABLE = "gerp-customers"


def teardown_check(row, verb):
    """Why a build must not start, or "" when it may. `row` is the gerp's customers row as plain
    values, or None when there is no such row."""
    if not row:
        return f"no row in {CUSTOMERS_TABLE} for that gerp"
    status = row.get("status", "")
    if verb == "stop":
        if row.get("gerp_id") == SELLER_GERP:
            return (f"{SELLER_GERP} is the seller's own gerp: its stack carries the closure scripts, "
                    "the hub's trust and the seller's books, and its applies are local")
        if status != "active":
            return f"row is {status or 'unset'}; stop takes an active gerp"
    elif verb == "start":
        if status != "stopped":
            return (f"row is {status or 'unset'}; start takes a stopped gerp. A closed row is the "
                    "closure's: delete its closure/close.py schedule and set it to provisioning by hand first")
    else:
        return f"no such verb: {verb}"
    if not row.get("aws_account_id"):
        return "the row has no aws_account_id"
    return ""


def _customer_row(op, gerp_id):
    item = op.client("dynamodb").get_item(TableName=CUSTOMERS_TABLE, Key={"gerp_id": {"S": gerp_id}}).get("Item")
    return {k: next(iter(v.values())) for k, v in item.items()} if item else None


def _run_build(op, gerp_id, account, action, region):
    """StartBuild on tower-per-customer, then the phases as they change and the log's `==>` lines
    at the end. Returns the build's final status. `region` is the gerp's, off its row: the buildspec
    defaults to us-east-1 without it, and a gerp built elsewhere then plans against the wrong region."""
    cb = op.client("codebuild")
    build = cb.start_build(projectName="tower-per-customer", environmentVariablesOverride=[
        {"name": "CUSTOMER_ID", "value": gerp_id, "type": "PLAINTEXT"},
        {"name": "CUSTOMER_ACCOUNT_ID", "value": account, "type": "PLAINTEXT"},
        {"name": "CUSTOMER_REGION", "value": region, "type": "PLAINTEXT"},
        {"name": "TF_ACTION", "value": action, "type": "PLAINTEXT"},
    ])["build"]
    bid, t0, phase = build["id"], time.time(), ""
    print(f"==> {action} {gerp_id} ({account}): build {bid.split(':')[-1]}", flush=True)
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
                if (line.startswith("==>") or line.startswith("Plan:") or "complete!" in line
                        or line.startswith(("marked ", "closed ", "stopped ")) or "Error" in line or "ERROR" in line):
                    print("    " + line[:200])
            if r.get("nextForwardToken") in (None, token):
                break
            token = kw["nextToken"] = r["nextForwardToken"]
    return status


def cmd_stop(args):
    op = _boto(args.operator_profile)
    row = _customer_row(op, args.gerp)
    why = teardown_check(row, "stop")
    if why:
        sys.exit(f"not stopping {args.gerp}: {why}")
    status = _run_build(op, args.gerp, row["aws_account_id"], "stop", row.get("region") or "us-east-1")
    row = _customer_row(op, args.gerp) or {}
    print(f"row: {row.get('status')}" + (f", stopped_at {row.get('stopped_at')}" if row.get("stopped_at") else ""))
    if status != "SUCCEEDED" or row.get("status") != "stopped":
        sys.exit(1)


def cmd_start(args):
    op = _boto(args.operator_profile)
    row = _customer_row(op, args.gerp)
    why = teardown_check(row, "start")
    if why:
        sys.exit(f"not starting {args.gerp}: {why}")
    status = _run_build(op, args.gerp, row["aws_account_id"], "apply", row.get("region") or "us-east-1")
    row = _customer_row(op, args.gerp) or {}
    print(f"row: {row.get('status')}" + (f", gateway_url {row.get('gateway_url')}" if row.get("gateway_url") else ""))
    if status != "SUCCEEDED" or row.get("status") != "active":
        sys.exit(1)
    print("the settings went with the stack: the onboarding walk runs again from the chat door")


def cmd_assets(args):
    """Sync the demo-asset gifs (prod/gradienterp_cloud/assets/) to the assets bucket and
    invalidate what changed on the CloudFront distro. Content is THIS command's job; the bucket,
    the distro and DNS are the gradienterp_cloud stack's shape — the lambda-code split, applied
    to media. Etag comparison makes a clean run a no-op, so this is safe to run reflexively."""
    import hashlib as _hl
    import mimetypes
    assets_dir = os.path.join(REPO, args.dir)
    bucket = "gradienterp-cloud-assets-185369506315"
    alias = "assets.gradienterp.cloud"
    cache = "public, max-age=2592000"  # 30 days — re-cuts are rare, and the upload invalidates its exact paths at the edge anyway

    ses = _boto(args.profile)
    s3 = ses.client("s3")
    changed = []
    for name in sorted(os.listdir(assets_dir)):
        path = os.path.join(assets_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            body = f.read()
        md5 = _hl.md5(body).hexdigest()
        try:
            remote = s3.head_object(Bucket=bucket, Key=name)["ETag"].strip(chr(34))
        except s3.exceptions.ClientError:
            remote = None
        if remote == md5:
            print(f"  in sync   {name}")
            continue
        ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
        s3.put_object(Bucket=bucket, Key=name, Body=body, ContentType=ct, CacheControl=cache)
        print(f"  uploaded  {name} ({len(body) / 1048576:.1f}M)")
        changed.append("/" + name)

    if not changed:
        print("assets in sync — nothing invalidated")
        return
    cf = ses.client("cloudfront")
    dist = next(d["Id"] for d in cf.list_distributions()["DistributionList"].get("Items", [])
                if alias in d.get("Aliases", {}).get("Items", []))
    inv = cf.create_invalidation(DistributionId=dist, InvalidationBatch={
        "Paths": {"Quantity": len(changed), "Items": changed},
        "CallerReference": str(time.time())})
    print(f"invalidated {len(changed)} path(s) on {dist} ({inv['Invalidation']['Id']}) — live at the edge in ~1min")


def cmd_source(args):
    """Build and upload the CodeBuild source zip — the repo subset `tower-per-customer` runs
    prod/per_customer terraform out of.

    Content is this command's job; the bucket is prod/tower's shape. Same split as lambda code
    and the demo assets, and for the same reason: an apply should not be what ships a code
    change. It also unhooks tower's PLAN from a locally-built file — the object used to be an
    aws_s3_object with `etag = filemd5(...)`, so tower could not even plan unless someone had
    run the build script first.

    The zip must carry every module prod/per_customer instantiates or `terraform init` inside
    CodeBuild fails before it plans (tests/tower/local/test_codebuild_source.py holds that).
    """
    import hashlib as _hl
    import subprocess
    script = os.path.join(REPO, "scripts", "build-codebuild-source.sh")
    zip_path = os.path.join(REPO, "prod", "tower", ".build", "per-customer-source.zip")
    bucket = f"{CONFIG['STACK_PREFIX']}-codebuild-source-{CONFIG['OPERATOR_ACCOUNT_ID']}"
    key = "per-customer-source.zip"

    subprocess.run(["bash", script], check=True, cwd=REPO, capture_output=True)
    with open(zip_path, "rb") as f:
        body = f.read()
    md5 = _hl.md5(body).hexdigest()

    s3 = _boto(args.profile).client("s3")
    try:
        live = s3.head_object(Bucket=bucket, Key=key)["ETag"].strip('"')
    except Exception:
        live = ""
    if live == md5:
        print(f"{key}: unchanged ({md5[:12]}…)")
        return
    out = s3.put_object(Bucket=bucket, Key=key, Body=body)
    print(f"{key}: pushed {md5[:12]}… ({len(body):,} bytes, version {out.get('VersionId', '-')[:12]}…)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="verb", required=True)
    st = sub.add_parser("status")
    st.add_argument("--gerp", default="gradienterp", help="the gerp whose fleet is compared (default: the operator's own)")
    st.add_argument("--profile", default=None, help="the profile reaching it (default: gerp-<gerp>)")
    st.add_argument("--operator-profile", default="operator-org",
                    help="account holding the BFF + artifact bucket")
    st.add_argument("--all", action="store_true", help="print in-sync rows too")
    st.set_defaults(fn=cmd_status)
    ps = sub.add_parser("push")
    ps.add_argument("--dirs", nargs="*", help="src dirs to push (default: whole fleet)")
    ps.add_argument("--notes", default="", help="agent-readable release annotation")
    ps.add_argument("--push", action="store_true",
                    help="phase: build + upload the artifact to the bucket only (seeds a new lambda; no function update)")
    ps.add_argument("--deploy", action="store_true",
                    help="phase: point the live function at the bucket's current artifact only (no rebuild). Default (neither flag) does both.")
    ps.add_argument("--gerp", default="gradienterp", help="the gerp whose functions take the push (default: the operator's own)")
    ps.add_argument("--profile", default=None, help="the profile reaching it (default: gerp-<gerp>)")
    ps.add_argument("--operator-profile", default="operator-org")
    ps.set_defaults(fn=cmd_push)
    im = sub.add_parser("image")
    im.add_argument("--gerp", default="gradienterp", help="the gerp whose runtime takes the image (default: the operator's own)")
    im.add_argument("--all", action="store_true", help="every active gerp — said, never implied")
    im.add_argument("--no-build", action="store_true", help="no build: move the named gerps onto the image already at the top of ECR")
    im.add_argument("--operator-profile", default="operator-org")
    im.set_defaults(fn=cmd_image)
    for verb, fn, what in (("stop", cmd_stop, "export, then destroy the stack; the row reads stopped"),
                           ("start", cmd_start, "apply the stack into a stopped gerp; the row reads active")):
        sp = sub.add_parser(verb, help=what)
        sp.add_argument("--gerp", required=True, help="the gerp, by id — never the seller's own")
        sp.add_argument("--operator-profile", default="operator-org")
        sp.set_defaults(fn=fn)
    ass = sub.add_parser("assets")
    ass.add_argument("--dir", default="prod/gradienterp_cloud/assets",
                     help="repo-relative source dir (the push --dirs convention)")
    # the gradienterp_cloud stack's provider assumes OrganizationAccountAccessRole into the
    # OPERATOR account — the bucket lives there, so the operator-org chain is the right creds
    ass.add_argument("--profile", default="operator-org")
    ass.set_defaults(fn=cmd_assets)
    src = sub.add_parser("source")
    # the bucket is in the OPERATOR account, same as the artifact bucket
    src.add_argument("--profile", default="operator-org")
    src.set_defaults(fn=cmd_source)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
