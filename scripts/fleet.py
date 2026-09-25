"""fleet.py — the fleet deploy in pieces (issue #57).

Every gerp walked, each function compared with the platform's artifact bucket, only what differs
moved. Pure AWS calls, no local build in the path, and no knowledge of where it runs: boto3 takes
the operator's credentials from the environment, a laptop profile or a runner's OIDC role alike,
and the gerp profiles `awsacct.sh --all` writes (the aws action writes the same on a runner) do
the assume. Six pieces, each one call shape with tab-separated lines on stdout and an exit code,
composed as a pipe in `scripts/deploy.sh fleet` (the laptop's) and in the workflow yaml (the
runner's), identical in logic:

  listzipversions                 src_dir  version_id  sha256      the latest version of every zip in the bucket: the snapshot
  listgerps                       gerp_id  account  region         every active row with an account
  listzipfnsconf --gerp X         gerp  function  src_dir  sha     every function carrying gerp:src-dir, with its CodeSha256
  status <snapshot> [--dirs …]    gerp  function  in-sync|behind|left  [key  version_id  sha]     the join on src_dir
  update                          gerp  function  moved|failed  version|why                       one UpdateFunctionCode per behind line
  push <dir>…                     src_dir  version_id  sha256      build and put with provenance; the only piece that zips

A loop is one artifact type paired with one resource type; this is zip × lambda function. Another
deployable is another loop after it (listimageversions, listimagefnsconf, listecstaskconf …), not a
column in these lines.
"""
import argparse
import concurrent.futures as cf
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy  # noqa: E402



def _out(*cols):
    print("\t".join(str(c) for c in cols), flush=True)


def listzipversions(args):
    """The snapshot: `src_dir version_id sha256` for the latest version of every zip in the
    operator's bucket, read once before any account is touched."""
    s3 = deploy._boto(args.operator_profile).client("s3")
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=deploy.BUCKET):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".zip")]
    for key in sorted(keys):
        version, sha = deploy.artifact_head(s3, key)
        if version and sha:
            _out(key[:-4], version, sha)


def listgerps(args):
    for r in deploy._active_rows(deploy._boto(args.operator_profile)):
        _out(r["gerp_id"], r["account"], r["region"])


def listzipfnsconf(args):
    """One gerp's functions through its profile: the tag query for the src_dir, ListFunctions
    for the sha; a tagged function the account no longer holds shows `-`."""
    session = deploy._boto(args.profile or f"gerp-{args.gerp}")
    fns = deploy.fleet(session)
    shas = deploy.deployed_shas(session.client("lambda"))
    for name, src in sorted(fns.items()):
        _out(args.gerp, name, src, shas.get(name, "-"))


def read_snapshot(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            if line.strip():
                src, version, sha = line.rstrip("\n").split("\t")
                out[src] = (version, sha)
    return out


def status(args):
    """The join of `listzipfnsconf` lines on stdin with the snapshot: `in-sync`, `behind` with the
    key and the snapshot's version to move to, or `left` (a function whose src_dir has no artifact,
    or a src_dir another gerp in the run carries and this one does not: the apply creates it, a
    deploy cannot). A key nobody carries is a retired function's zip and says nothing. Touches
    nothing."""
    snapshot = read_snapshot(args.snapshot)
    only = set(args.dirs or [])
    seen = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        gerp, name, src, sha = line.rstrip("\n").split("\t")
        seen.setdefault(gerp, set()).add(src)
        if only and src not in only:
            continue
        if src not in snapshot:
            _out(gerp, name, "left", "no artifact for " + src)
        elif sha == snapshot[src][1]:
            _out(gerp, name, "in-sync")
        else:
            version, target = snapshot[src]
            _out(gerp, name, "behind", src + ".zip", version, target)
    carried = set().union(*seen.values()) if seen else set()
    for gerp, srcs in sorted(seen.items()):
        for src in sorted(carried - srcs):
            if src in snapshot and (not only or src in only):
                _out(gerp, "-", "left", "no function for " + src)


def _move(op_s3, line):
    gerp, name, key, version, sha = line
    try:
        session = deploy._boto(f"gerp-{gerp}")
        region = session.region_name or "us-east-1"
        bucket = deploy.bucket_for(region)
        if bucket != deploy.BUCKET:
            # a gerp elsewhere deploys from its region's replica, under that bucket's version id
            version = deploy.replicated(op_s3, key, sha, bucket)
            if version is None:
                return (gerp, name, "failed", f"not yet replicated to {bucket}")
        lam = session.client("lambda")
        lam.update_function_code(FunctionName=name, S3Bucket=bucket, S3Key=key, S3ObjectVersion=version)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        return (gerp, name, "moved", version)
    except Exception as e:  # noqa: BLE001 — one function's failure is a line, not the fleet's end
        return (gerp, name, "failed", f"{type(e).__name__}: {e}")


def update(args):
    """`behind` lines on stdin → one UpdateFunctionCode each, with the bucket, the key and the
    snapshot's version; never a "latest" read here. Every line runs; the exit names the failures."""
    lines = []
    for line in sys.stdin:
        if line.strip():
            cols = line.rstrip("\n").split("\t")
            if cols[2] == "behind":
                lines.append((cols[0], cols[1], cols[3], cols[4], cols[5]))
    op_s3 = deploy._boto(args.operator_profile).client("s3")
    failed = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for gerp, name, state, why in ex.map(lambda l: _move(op_s3, l), lines):
            _out(gerp, name, state, why)
            if state == "failed":
                failed.append(f"{gerp} {name}")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


def push(args):
    """Build each dir and put it with provenance when the bucket's latest differs; the only piece
    that zips. Prints the snapshot line the put (or the current version) gives."""
    import time
    s3 = deploy._boto(args.operator_profile).client("s3")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for src in args.dirs:
        blob = deploy.build_artifact(src)
        sha = deploy.sha256_b64_bytes(blob)
        version, art = deploy.artifact_head(s3, src + ".zip")
        if art != sha:
            version = deploy.put_artifact(s3, src + ".zip", blob, {"src_dir": src, "src_sha256": deploy.src_tree_sha(src),
                                                                   "built_at": now, "zip_sha256": sha}, args.notes)
        _out(src, version, sha)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="piece", required=True)
    for name, fn in (("listzipversions", listzipversions), ("listgerps", listgerps)):
        p = sub.add_parser(name)
        p.add_argument("--operator-profile", default="operator-org")
        p.set_defaults(fn=fn)
    p = sub.add_parser("listzipfnsconf")
    p.add_argument("--gerp", required=True)
    p.add_argument("--profile", default=None, help="the profile reaching it (default: gerp-<gerp>)")
    p.set_defaults(fn=listzipfnsconf)
    p = sub.add_parser("status")
    p.add_argument("snapshot", help="the file listzipversions wrote")
    p.add_argument("--dirs", nargs="*", help="only these src dirs")
    p.set_defaults(fn=status)
    p = sub.add_parser("update")
    p.add_argument("--operator-profile", default="operator-org")
    p.set_defaults(fn=update)
    p = sub.add_parser("push")
    p.add_argument("dirs", nargs="+")
    p.add_argument("--notes", default="")
    p.add_argument("--operator-profile", default="operator-org")
    p.set_defaults(fn=push)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
