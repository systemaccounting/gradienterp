"""Take a stack's image manifest from the RUNNING stack.

    python3 tests/server/snapshot.py per_customer
    python3 tests/server/snapshot.py bff

Writes `tests/server/<stack>/image.json`. Nothing in it is authored here — AWS is asked to describe
itself, which is the same reason `deploy.py` keeps no list of 122 lambdas:

  routes    `get-routes` + `get-integrations` + `get-authorizers`. NOT the OpenAPI export: that
            drops `$default` (the BFF's SPA catch-all is a route, and an OAS path it is not), and
            OAS cannot model a websocket API at all. One call shape covers every stack.
  env       `get-function-configuration` for every function the `gerp:src-dir` tag returns — not
            just the routed ones. A route is an entry point; behind it are cross-invokes, and in
            one process they share one environment. Missing a callee's config surfaces two hops in
            as `KeyError: PENDING_TABLE`. The env values in `REDACT` are replaced with a placeholder
            before the file is written, everywhere they appear.

Committed, because the dev loop must not need AWS. Re-run when a route or an env var changes.
"""

import argparse
import json
import re
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
_SRC_DIRS: dict = {}

STACKS = {
    # the gerp's own bus rides along: its consume rules are how an event the hub puts there
    # reaches the gerp's functions
    "per_customer":      {"profile": "gerp-gradienterp", "api": "gerp-server-", "bus": "gerp-internal-gradienterp", "gerp": "gradienterp"},
    "bff":               {"profile": "operator-org",                 "api": "gerp-cloud",   "gerp": "gradienterp"},
    # the read api is a REST api (api.tf) with no local image; the stack's own functions (the
    # counter, the publisher, the published stamp) are what the snapshot carries
    "openlyoperated_biz": {"profile": "operator-org",                "api": None,           "gerp": "gradienterp"},
    # No API of their own. Snapshotted anyway, for their function CONFIG: a stack that calls into
    # one of these (the bff invokes tower's provisioner) merges it via `depends_on` in
    # scripts/tags.json, so the callee finds its env instead of a KeyError two hops in.
    # the operator's own bus: what the hub's forward edge puts here, and the consumers' rules
    "platform":          {"profile": "operator-org",  "api": None, "bus": "gerp-operator", "gerp": "gradienterp"},
    # the hub (prod/hub, its own account): the bus every gerp puts to, and the edges — a rule per
    # spoke whose target is that gerp's bus, and the forward to the operator's
    "hub":               {"profile": "hub-us-east-1", "api": None, "bus": "gerp-events", "gerp": "gradienterp"},
    "tower":             {"profile": "operator-org",                 "api": None,           "gerp": "gradienterp"},
}

# Env values the repo does not publish, by variable name → the placeholder written instead: the
# owner's own address, and the owner portal's slug (the portal's Function URL has no auth, so the
# slug is its credential). A value is replaced in every env var that carries it, so PORTAL_URL,
# which ends in the slug, gets the placeholder too. The local stack runs on the placeholders.
REDACT = {
    "OWNER_EMAIL": "owner@example.com",
    "ALLOWLIST": "owner@example.com",
    "PORTAL_SLUG": "portal-slug",
}


# A value shaped like a credential stops the snapshot: the env is copied whole into a committed,
# published file, and a secret belongs in SSM with its NAME in the env.
CREDENTIAL = re.compile(r"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}|whsec_[0-9A-Za-z]{10,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
                        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-ant-[0-9A-Za-z_-]{10,}|\b(?:ghp|gho|ghs|github_pat)_[0-9A-Za-z_]{20,}"
                        r"|\bxox[abpr]-[0-9A-Za-z-]{10,}|\beyJ[0-9A-Za-z_-]{10,}\.eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}")


def _refuse_credentials(fns):
    """Every env var whose value looks like a credential, as `function: VAR`."""
    return [f"{name}: {key}" for name, cfg in fns.items()
            for key, value in ((cfg or {}).get("env") or {}).items() if CREDENTIAL.search(str(value))]


def aws(*args, profile):
    out = subprocess.run(["aws", *args, "--no-cli-pager", "--profile", profile, "--output", "json"],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"aws {' '.join(args)} failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout or "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stack", choices=sorted(STACKS))
    ap.add_argument("--gerp")
    args = ap.parse_args()
    cfg = STACKS[args.stack]
    profile, gerp = cfg["profile"], args.gerp or cfg["gerp"]

    routes, fns = [], {}
    aid, protocol = None, None
    if cfg["api"]:
        aid, protocol, routes, fns = _api(cfg["api"], profile)

    _sweep(args.stack, profile, fns)
    _configure(fns, profile)
    _redact(fns)
    if found := _refuse_credentials(fns):
        raise SystemExit("not written — these env values look like credentials; move each to SSM and put its "
                         "name in the env:\n  " + "\n  ".join(found))
    bus = _bus(cfg.get("bus"), profile)

    out = HERE / args.stack / "image.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"stack": args.stack, "gerp": gerp, "api_id": aid,
                               "protocol": protocol,
                               **({"bus": bus} if bus else {}),
                               "routes": sorted(routes, key=lambda r: r["route_key"]),
                               "functions": fns}, indent=2, sort_keys=True) + "\n")
    urls = sum(1 for f in fns.values() if f.get("function_url"))
    print(f"{out.relative_to(HERE.parents[1])}: {protocol or 'no api'} · {len(routes)} routes · "
          f"{len(fns)} functions · {urls} function urls")


def _redact(fns):
    """Replace each `REDACT` value in every function's env, in place. Longest value first, so a
    list that contains an address is replaced whole before the address inside it is."""
    swaps = {}
    for cfg in fns.values():
        for key, placeholder in REDACT.items():
            if value := (cfg or {}).get("env", {}).get(key):
                swaps[value] = placeholder
    for cfg in fns.values():
        env = (cfg or {}).get("env", {})
        for key, value in env.items():
            for real in sorted(swaps, key=len, reverse=True):
                value = value.replace(real, swaps[real])
            env[key] = value


def _bus(name, profile):
    """The event bus and its rules, with each rule's pattern and targets.

    An emit is only half a path — the rule is what decides who hears it, and a pattern that does
    not match is a live failure mode. So the patterns are taken from the running bus rather than
    reconstructed, and a local run does the same matching production does.
    """
    if not name:
        return None
    rules = []
    for r in aws("events", "list-rules", "--event-bus-name", name, profile=profile)["Rules"]:
        targets = aws("events", "list-targets-by-rule", "--rule", r["Name"],
                      "--event-bus-name", name, profile=profile)["Targets"]
        rules.append({
            "name": r["Name"],
            "pattern": json.loads(r["EventPattern"]) if r.get("EventPattern") else None,
            # only lambda targets can be delivered locally; a firehose or an sqs arn is recorded so
            # the omission is visible rather than silent
            "targets": [{"arn": t["Arn"],
                         "function": t["Arn"].split(":function:")[-1] if ":function:" in t["Arn"] else None,
                         # a bus target is delivered locally by a put onto the bus of that name
                         "bus": t["Arn"].split(":event-bus/")[-1] if ":event-bus/" in t["Arn"] else None}
                        for t in targets],
        })
    return {"name": name, "rules": sorted(rules, key=lambda r: r["name"])}


def _api(name_prefix, profile):
    apis = aws("apigatewayv2", "get-apis", profile=profile)["Items"]
    api = next(a for a in apis if a["Name"].startswith(name_prefix))
    aid, protocol = api["ApiId"], api["ProtocolType"]

    integrations = {i["IntegrationId"]: i for i in
                    aws("apigatewayv2", "get-integrations", "--api-id", aid, profile=profile)["Items"]}
    authorizers = {a["AuthorizerId"]: a for a in
                   aws("apigatewayv2", "get-authorizers", "--api-id", aid, profile=profile)["Items"]}

    routes, fns = [], {}
    for r in aws("apigatewayv2", "get-routes", "--api-id", aid, profile=profile)["Items"]:
        integ = integrations.get((r.get("Target") or "").split("/")[-1], {})
        fn = integ.get("IntegrationUri", "").split(":function:")[-1].split("/")[0]
        key = r["RouteKey"]
        method, _, path = key.partition(" ")
        auth = authorizers.get(r.get("AuthorizerId", ""), {})
        routes.append({
            "route_key": key,
            "method": method if path else "ANY",
            "path": path or None,                       # None ⇒ $default, the catch-all
            "function": fn,
            "authorization": r.get("AuthorizationType", "NONE"),
            "jwt": auth.get("JwtConfiguration") or None,
        })
        if fn:
            fns[fn] = None
    return aid, protocol, routes, fns


def _sweep(stack, profile, fns):
    """THIS stack's functions, by tag. The operator account holds five stacks' worth, so without
    `gerp:stack` there is no way to ask for one — this is the query the tag exists for."""
    tagged = aws("resourcegroupstaggingapi", "get-resources",
                 "--tag-filters", f"Key=gerp:stack,Values={stack}",
                 "--resource-type-filters", "lambda:function", profile=profile)
    src_dirs = {}
    for m in tagged["ResourceTagMappingList"]:
        name = m["ResourceARN"].split(":function:")[-1]
        fns.setdefault(name, None)
        tags = {t["Key"]: t["Value"] for t in m["Tags"]}
        src_dirs[name] = tags.get("gerp:src-dir")
    _SRC_DIRS.update(src_dirs)


def _configure(fns, profile):
    """Each function's deployed environment, and its Function URL if it has one."""
    for fn in sorted(fns):
        c = aws("lambda", "get-function-configuration", "--function-name", fn, profile=profile)
        # A Function URL is a property of its function, not a taggable resource — so it is read off
        # the function rather than discovered. Three of this fleet's four front doors are one, and
        # none of them appears in any route table.
        try:
            url = aws("lambda", "get-function-url-config", "--function-name", fn, profile=profile)
        except SystemExit:
            url = {}
        # src_dir comes from the tag, so a function whose source is not under a `lambdas/` dir
        # (the BFF lives at prod/gradienterp_cloud/bff) resolves without a naming convention, and
        # a name that collides across modules (settle_agreement, apply_inbound) is unambiguous.
        fns[fn] = {"env": c.get("Environment", {}).get("Variables", {}), "timeout": c.get("Timeout"),
                   "src_dir": _SRC_DIRS.get(fn),
                   **({"function_url": {"auth": url["AuthType"]}} if url.get("FunctionUrl") else {})}


if __name__ == "__main__":
    main()
