"""A stack, served locally — the shared runner behind `tests/server/<stack>/server.py`.

One shape for every stack: read `image.json` (taken from the running stack by `snapshot.py`),
create the state it owns on moto, and bind each route to the REAL handler, run in this process.

    route   →  the deployed function's actual handler, imported from source
    AWS     →  moto (`modules/aws/aws.py` sends every boto3 call to the local endpoint)
    state   →  the stack's tables, with their real names and real key schemas

What that buys over a mock: a request runs the signature check, the transform, the cross-invoke and
the write that ship. A mock can only assert what it was handed.

A stack's `server.py` supplies what is genuinely its own — which tables, what to seed into them,
and whether a `$default` route serves files — and nothing else.
"""

import contextlib
import json
import os
import pathlib
import sys

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

REPO = pathlib.Path("/repo") if pathlib.Path("/repo").exists() else pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "modules" / "aws"))
sys.path.insert(0, str(REPO / "tests"))
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)   # not in Lambda ⇒ boto3 goes to the local endpoint

from helpers.localaws import OPERATOR_ONLY as OPERATOR_TABLES  # noqa: E402  (needs the path above)

# Set by the Lambda RUNTIME, not by a function's Environment config — so `get-function-configuration`
# never sees them and no snapshot can carry them. A handler that reads one directly
# (`os.environ["AWS_REGION"]`) fails locally for a reason that has nothing to do with the code.
RUNTIME_ENV = {
    "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
    "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
}


def local_gerps() -> dict:
    """LOCAL_GERPS, the dogfood login's sub → the gerps it owns: from the environment, else the repo-root
    .env (gitignored). The value is JSON or a path to a JSON file; unset is {}."""
    raw = (os.environ.get("LOCAL_GERPS") or "").strip()
    env = REPO / ".env"
    if not raw and env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip() == "LOCAL_GERPS":
                raw = v.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(pathlib.Path(raw).read_text()) if pathlib.Path(raw).exists() else {}


def base_env(image: dict) -> tuple[dict, dict]:
    """Every function's config at once — the union across the stack.

    Production gives each lambda its own environment; here they share one process. A route is only
    the ENTRY point (a stripe webhook cross-invokes post_journal_entry, which cross-invokes
    write_schema (op: extend)), so the base is the union and the entry function is overlaid per request.

    Where two functions disagree, majority wins and the disagreement is REPORTED — silently picking
    one is how a local run stops matching production.
    """
    import collections
    counts = collections.defaultdict(collections.Counter)
    for cfg in image["functions"].values():
        for k, v in cfg["env"].items():
            counts[k][v] += 1
    env = {k: c.most_common(1)[0][0] for k, c in counts.items()}
    return env, {k: sorted(c) for k, c in counts.items() if len(c) > 1}


@contextlib.contextmanager
def _env(overlay: dict):
    """Apply `overlay` to os.environ for the duration, then put back exactly what was there.

    Process-global, so it is only safe where nothing else runs concurrently under a different
    overlay — which is why `run` skips it whenever the function's config already matches the base
    the stack set at startup.
    """
    prior = {k: os.environ.get(k) for k in overlay}
    os.environ.update(overlay)
    try:
        yield
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_tables(image: dict, which) -> list[str]:
    """`which` is a predicate over the logical names in `tests/testdata/table-schemas.json` — a
    snapshot of the deployed tables, so a GSI a handler queries exists here too and a query naming
    the wrong index fails locally exactly as it would in production."""
    from helpers.localaws import SCHEMAS, make_table, real_name
    gerp = image["gerp"]
    made = []
    for logical in sorted(SCHEMAS):
        if not which(logical):
            continue
        make_table(logical, gerp)
        made.append(real_name(logical, gerp))
    return made


def make_bus(env: dict) -> str | None:
    """The shared gerp-events bus, named by the arn the functions already carry.

    It belongs to `platform`, but every per_customer lambda emits onto it and all the local
    processes share one moto — so whoever comes up first creates it. Without it a `put_events` is a
    ResourceNotFoundException in the middle of an otherwise-good write path, which reads like a
    failure of the thing under test.
    """
    arn = env.get("OP_EVENT_BUS_ARN")
    if not arn:
        return None
    name = arn.rsplit("/", 1)[-1]
    from aws import client
    ev = client("events")
    try:
        ev.create_event_bus(Name=name)
    except ev.exceptions.ResourceAlreadyExistsException:
        pass
    return name


def _event(request: Request, body: bytes, route: dict) -> dict:
    """The API Gateway v2 payload-format-2.0 event the deployed function receives.

    Claims come from `x-debug-sub`, or from an UNVERIFIED decode of a real bearer token — the local
    runner is not an authorizer, and a route the gateway protects still has to be reachable in a dev
    loop. In production these arrive from the APIGW JWT authorizer and no handler ever builds them.
    """
    import base64
    headers = {k.lower(): v for k, v in request.headers.items()}
    claims = {}
    if auth := headers.get("authorization", ""):
        if auth.lower().startswith("bearer "):
            try:
                payload = auth.split(" ", 1)[1].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            except Exception:  # noqa: BLE001 — a malformed token is simply no claims
                claims = {}
    if sub := headers.get("x-debug-sub"):
        claims = {"sub": sub, "email": headers.get("x-debug-email", ""), **claims}

    rc = {"http": {"method": request.method, "path": request.url.path}}
    if claims:
        rc["authorizer"] = {"jwt": {"claims": claims}}
    return {
        "rawPath": request.url.path,
        "headers": headers,
        "body": body.decode(errors="replace") if body else None,
        "pathParameters": dict(request.path_params) or None,
        "queryStringParameters": dict(request.query_params) or None,
        "requestContext": rc,
    }


def load_image(image_path: pathlib.Path) -> dict:
    """The manifest, plus two things it cannot know by itself.

    `image.overlay.json` (beside it, gitignored) is how you design a route BEFORE it exists in AWS
    — the snapshot describes what is deployed, and waiting for an apply to try an idea is exactly
    the loop this was built to delete. Same shape as `image.json`; its routes and functions win.

        {"routes":    [{"route_key": "POST /refunds", "method": "POST", "path": "/refunds",
                        "function": "gerp-invoicing-gradienterp-refund"}],
         "functions": {"gerp-invoicing-gradienterp-refund":
                       {"src_dir": "modules/invoicing/lambdas/refund", "env": {}}}}

    Dependency stacks' FUNCTIONS are merged too. A stack calls across boundaries — the bff invokes
    tower's provisioner — and while the handler resolves either way, its config only exists in the
    other stack's manifest. `scripts/tags.json` already declares who depends on whom.
    """
    image = json.loads(image_path.read_text())
    here = image_path.parent

    tags = json.loads((REPO / "scripts" / "tags.json").read_text())
    for dep in tags["gerp:stack"]["values"].get(image["stack"], {}).get("depends_on", []):
        dep_image = here.parent / dep / "image.json"
        if dep_image.exists():
            for fn, cfg in json.loads(dep_image.read_text())["functions"].items():
                image["functions"].setdefault(fn, cfg)

    overlay_path = here / "image.overlay.json"
    if overlay_path.exists():
        overlay = json.loads(overlay_path.read_text())
        image["functions"].update(overlay.get("functions") or {})
        planned = {r["route_key"] for r in overlay.get("routes") or []}
        image["routes"] = [r for r in image["routes"] if r["route_key"] not in planned] \
            + (overlay.get("routes") or [])
        print(f"[{image['stack']}] overlay: {len(planned)} planned route(s) not yet in AWS")
    return image


def build(image_path: pathlib.Path, *, tables, seed=None, static: pathlib.Path | None = None, static_html=False):
    """The stack's FastAPI app. `tables` picks which logical tables to create; `seed` runs after
    they exist; `static` is the directory a `$default` route serves from."""
    image = load_image(image_path)
    app = FastAPI(title=f"{image['stack']} · {image['gerp']}", version=image.get("api_id") or "none")
    state: dict = {"tables": []}

    # Handlers, cached on the SOURCE MTIME. Re-executing a module on every request is what makes an
    # edit show up on the next curl, and it is also a race: uvicorn runs these sync endpoints on a
    # threadpool, handlers read their constants at import time, and `run` used to be swapping
    # process-global env underneath. Keyed on mtime, an edit still re-execs and an ordinary request
    # does no import at all, so there is nothing left to race. No lock, so nothing serializes.
    modules: dict = {}

    def handler_for(fn_name):
        """The real handler. `gerp:src-dir` is snapshotted into the manifest, so this is exact and
        needs no AWS call; the glob is the fallback for a function the tag did not cover."""
        import importlib.util
        from aws import _bundle_dirs, _front, _local_handler
        src = (image["functions"].get(fn_name) or {}).get("src_dir")
        if not src:
            return _local_handler(fn_name)
        path = REPO / src / "main.py"
        key = (fn_name, path.stat().st_mtime_ns)
        if key in modules:
            return modules[key]
        _front(_bundle_dirs(path))
        spec = importlib.util.spec_from_file_location(f"_fn_{fn_name.replace('-', '_')}", path)
        mod = importlib.util.module_from_spec(spec)
        # exec under THIS function's own env, not the stack's base union: module-level constants are
        # read once now instead of on every request, so this is the only chance to get them right
        # for a function whose config differs from the majority.
        with _env(image["functions"].get(fn_name, {}).get("env", {})):
            spec.loader.exec_module(mod)
        # drop only THIS function's older mtimes. Clearing the whole cache would make two functions
        # in one stack evict each other on every alternating request and re-exec forever.
        for stale in [k for k in modules if k[0] == fn_name]:
            del modules[stale]
        modules[key] = mod.handler
        return mod.handler

    # Which functions actually need their env overlaid at CALL time. `base_env` already put the
    # union on the process at startup, so a function whose config matches it — every function in a
    # single-function stack, by definition — has nothing to swap and swapping anyway is all cost.
    base = base_env(image)[0]
    deltas = {name: {k: v for k, v in (cfg.get("env") or {}).items() if base.get(k) != v}
              for name, cfg in image["functions"].items()}

    def run(route, request: Request, body: bytes):
        fn = route["function"]
        handler = handler_for(fn)
        event = _event(request, body, route)
        delta = deltas.get(fn) or {}
        if not delta:
            return handler(event, None)
        with _env(delta):
            return handler(event, None)

    def respond(result: dict) -> Response:
        body = result.get("body") or ""
        if result.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body)
        headers = {k.lower(): v for k, v in (result.get("headers") or {}).items()}
        return Response(content=body, status_code=result.get("statusCode", 200),
                        media_type=headers.get("content-type", "application/json"))

    @app.on_event("startup")
    def _startup():
        state["tables"] = make_tables(image, tables)
        env, conflicts = base_env(image)
        os.environ.update({**RUNTIME_ENV, **env})
        bus = make_bus(env)
        if seed:
            seed(image)
        print(f"[{image['stack']}] {len(state['tables'])} tables · {len(image['routes'])} routes · "
              f"{len(image['functions'])} functions{' · bus ' + bus if bus else ''} · "
              f"gerp={image['gerp']}")
        for k, vs in conflicts.items():
            print(f"[{image['stack']}] env disagreement, base uses {env[k]!r}: {k} = {vs}")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "stack": image["stack"], "gerp": image["gerp"],
                "routes": len(image["routes"]), "tables": len(state["tables"])}

    def bind(route):
        # a factory, so `route` is captured lexically — a default argument would be read by
        # FastAPI as a query parameter, and a sync endpoint returning a coroutine is unserializable
        async def endpoint(request: Request):
            return respond(run(route, request, await request.body()))

        # APIGW's greedy segment `{proxy+}` is starlette's `{proxy:path}`; the handler reads it off
        # `pathParameters.proxy` either way
        import re
        path = re.sub(r"\{(\w+)\+\}", r"{\1:path}", route["path"])
        app.add_api_route(path, endpoint, methods=[route["method"]],
                          name=route["route_key"], summary=route["function"])

    default_route = None
    for r in image["routes"]:
        if r["path"] is None:                           # $default — bound last, as the catch-all
            default_route = r
        else:
            bind(r)

    # Function URLs. Not routes and not in any route table — a property of a function, read off it
    # by the snapshot. Mounted under /fn/<name> so a stack whose front door is a Function URL (three
    # of this fleet's four are) is reachable in the same process as everything else.
    for fn_name, cfg in sorted(image["functions"].items()):
        if not cfg.get("function_url"):
            continue
        bind({"route_key": f"FNURL {fn_name}", "method": "GET", "function": fn_name,
              "path": f"/fn/{fn_name}"})

    if default_route is not None or static:
        @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        async def catch_all(request: Request, full_path: str):
            """$default. Static files win when the stack serves any, so the SPA loads from disk and
            edits show on reload; everything else goes to the handler, which is what answers an
            /api path the gateway has no explicit route for (in production that is a 401, and
            reproducing it is the point).

            HTML is the exception and goes to the handler. The shell carries placeholders the
            handler substitutes at serve time (`<!--LLMS-->`, `<!--PURCHASE-TERMS-->`), so serving
            it off disk would hand the browser literal comments where production has content — a
            local page that differs from the deployed one in exactly the part being worked on. The
            handler reads the same directory (WEB_DIR), so edits still show on reload. A stack
            whose pages are plain files (`static_html`) serves them off disk too."""
            if static:
                candidate = (static / (full_path or "index.html")).resolve()
                if (candidate.is_file() and str(candidate).startswith(str(static.resolve()))
                        and (static_html or candidate.suffix != ".html")):
                    return FileResponse(candidate)
            if default_route is None:                       # files only: nothing answers the rest
                return Response(status_code=404)
            return respond(run(default_route, request, await request.body()))

    return app
