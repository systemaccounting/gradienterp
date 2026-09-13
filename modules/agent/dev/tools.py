"""The agent's tools, locally — DISCOVERED, not listed.

Every tool the agent has in production is a target registered on its AgentCore Gateway, and every
target is declared by a `schema.json` beside its lambda. So the toolset is already written down 88
times in this repo; this reads it rather than restating it. A tool added to a module appears here on
the next run, and one whose schema changed changes here too — which is the whole reason the old
hand-written list (17 of 88, with inline stand-ins for the rest) was worth deleting.

Dispatch goes through `modules/aws/aws.py::_local_handler`, the same in-process resolver
`tests/server` uses: it finds the handler by its source dir and reproduces the layout the deployed
zip has, so a tool call runs the code that ships. Against moto, which means the write actually
lands and the next tool call reads it back.

    bash scripts/local-dev.sh --start        # moto + the stacks (this reads their env)
    export ANTHROPIC_API_KEY=sk-ant-...
    bash modules/agent/dev/run.sh

Exports:
    TOOLS: list of dicts matching Anthropic's tool_use schema
    invoke(name, input) -> dict
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)   # not in Lambda ⇒ boto3 goes to the local endpoint

# The per_customer stack's config, taken from the running stack by tests/server/snapshot.py. The
# tools ARE that stack's lambdas, so they need its environment — the table names, the cross-invoke
# function names, the bus arn. Union across every function, the way the local image does it, since
# one tool call can reach three handlers.
_IMAGE = REPO_ROOT / "tests" / "server" / "per_customer" / "image.json"


def _load_env() -> int:
    if not _IMAGE.exists():
        print(f"[tools] no {_IMAGE.relative_to(REPO_ROOT)} — run: python3 tests/server/snapshot.py "
              "per_customer", file=sys.stderr)
        return 0
    image = json.loads(_IMAGE.read_text())
    env = {}
    for cfg in image["functions"].values():
        env.update(cfg.get("env") or {})
    os.environ.update(env)
    # the emit path needs the shared bus to exist; the local processes share one moto, so this is
    # idempotent whether or not `local-dev.sh` already made it
    if arn := env.get("OP_EVENT_BUS_ARN"):
        from aws import client
        ev = client("events")
        try:
            ev.create_event_bus(Name=arn.rsplit("/", 1)[-1])
        except Exception:  # noqa: BLE001 — already exists, or no emulator up yet
            pass
    return len(env)


def _discover():
    """{tool_name: (schema_path, src_dir)} — one per gateway target declared in the repo.

    The tool's NAME is its lambda's directory, which is how the gateway target is named too; the
    gateway is one shared namespace, so those names are already unique across modules.
    """
    out = {}
    for schema in sorted(REPO_ROOT.glob("modules/*/lambdas/*/schema.json")):
        src = schema.parent
        if not (src / "main.py").exists():
            continue
        out[src.name] = (schema, src)
    return out


_CATALOG = _discover()
_ENV_VARS = _load_env()


def _tool(name, schema_path):
    spec = json.loads(schema_path.read_text())
    description = spec.get("description", "").strip()
    # the schema file IS the input schema; its top-level description documents the tool itself
    input_schema = {k: v for k, v in spec.items() if k != "description"}
    input_schema.setdefault("type", "object")
    return {"name": name, "description": description, "input_schema": input_schema}


TOOLS = [_tool(name, schema) for name, (schema, _) in sorted(_CATALOG.items())]


def invoke(name, tool_input):
    """Run a tool: the real handler, in this process, against the local stack."""
    entry = _CATALOG.get(name)
    if not entry:
        return {"error": f"unknown tool '{name}'", "known": sorted(_CATALOG)[:20]}
    _, src = entry
    from aws import _bundle_dirs
    import importlib.util
    main = src / "main.py"
    for d in _bundle_dirs(main):
        if d not in sys.path:
            sys.path.insert(0, d)
    module_spec = importlib.util.spec_from_file_location(f"_tool_{name}", main)
    mod = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(mod)

    response = mod.handler(tool_input, None)
    if isinstance(response, dict) and "body" in response:
        body = response["body"]
        return json.loads(body) if isinstance(body, str) else body
    return response


if __name__ == "__main__":
    print(f"{len(TOOLS)} tools · {_ENV_VARS} env vars")
    for t in TOOLS:
        print(f"  {t['name']:28} {t['description'][:78]}")
