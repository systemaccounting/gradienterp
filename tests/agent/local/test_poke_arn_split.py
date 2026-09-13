"""Every lambda that wakes the agent must SPLIT the endpoint arn the same way.

`invoke_agent_runtime` wants the RUNTIME arn plus the endpoint NAME as `qualifier`. Handed the full
`.../runtime-endpoint/DEFAULT` arn it appends another `/runtime-endpoint/DEFAULT` to an
already-qualified arn, and the invoke is denied against an arn that does not exist.

That is not hypothetical: `canonical_pull_invoke` failed this way every week from at least
2026-07-23, logging to a place nobody read, while its test passed — because the test asserted the
whole arn was passed. It encoded the bug.

The split is copy-pasted into EIGHT lambdas and only that one had a test. This pins all of them, at
the module constants each computes at import. No AgentCore is needed and none would help: the fault
is in the ARGUMENTS, so capturing them is strictly more informative than reaching a live runtime
(which moto cannot be — it answers AgentCore's control plane but 404s the data plane).
"""

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# (module, lambda) — every caller of invoke_agent_runtime that derives the split at import
POKERS = [
    ("tasks",      "tasks_poke"),
    ("inbox",      "poke_agent"),
    ("calendar",   "agent_dispatcher"),
    ("invoicing",  "on_incomplete_draft"),
    ("schemas",    "canonical_pull_invoke"),
    ("agent",      "email"),
    ("agent",      "continue_poke"),
]

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:867637277314:runtime/agentcore_gradienterp-AbCdEf"
FULL = f"{RUNTIME}/runtime-endpoint/DEFAULT"

# Module roots each lambda's zip bundles alongside its own dir.
_EXTRA = {
    "tasks": [], "inbox": [], "agent": [],
    "calendar":  ["modules/calendar"],
    "invoicing": ["modules/invoicing", "modules/rules", "modules/inventory"],
    "schemas":   ["modules/schemas"],
}
_PURGE = {"_helpers", "rules", "instances", "params", "template", "movements", "capacity",
          "transition_rules", "general_rules", "catalog_rules"}
_added = []


# Every `os.environ[...]` the eight read at import. None of it affects the split under test —
# collected with: rg -o 'os\.environ\["[A-Z_]+"\]' over the eight mains.
_ENV = {
    "CUSTOMER_ID": "gradienterp", "GERP_ID": "gradienterp",
    "SETTINGS_TABLE": "t", "DEDUP_TABLE": "t",
    "EMAIL_BUCKET": "b", "UPLOADS_BUCKET": "b",
    "AGENT_ADDRESS": "agent@gradienterp.agents.gradienterp.cloud",
}


def _load(module, name):
    for d in list(_added):
        sys.path.remove(d)
        _added.remove(d)
    lambdas_dir = REPO_ROOT / "modules" / module / "lambdas"
    for d in [str(lambdas_dir)] + [str(REPO_ROOT / p) for p in _EXTRA[module]]:
        sys.path.insert(0, d)
        _added.append(d)
    for m in list(sys.modules):
        if m in _PURGE or m.startswith("poke_"):
            del sys.modules[m]
    path = lambdas_dir / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"poke_{module}_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_poker_splits_the_endpoint_arn():
    prior = {k: os.environ.get(k) for k in [*_ENV, "AGENT_RUNTIME_ENDPOINT_ARN"]}
    os.environ.update(_ENV)
    os.environ["AGENT_RUNTIME_ENDPOINT_ARN"] = FULL
    try:
        for module, name in POKERS:
            mod = _load(module, name)
            who = f"{module}/{name}"
            assert mod.RUNTIME_ARN == RUNTIME, f"{who}: passed {mod.RUNTIME_ARN!r}"
            assert "/runtime-endpoint/" not in mod.RUNTIME_ARN, who
            assert mod.QUALIFIER == "DEFAULT", f"{who}: qualifier {mod.QUALIFIER!r}"
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_an_already_split_arn_is_left_alone():
    """Terraform hands the full endpoint arn today, but a bare runtime arn must still work — the
    split is a normalizer, not a parser that assumes one shape."""
    prior = {k: os.environ.get(k) for k in [*_ENV, "AGENT_RUNTIME_ENDPOINT_ARN"]}
    os.environ.update(_ENV)
    os.environ["AGENT_RUNTIME_ENDPOINT_ARN"] = RUNTIME
    try:
        for module, name in POKERS:
            mod = _load(module, name)
            assert mod.RUNTIME_ARN == RUNTIME, f"{module}/{name}"
            assert mod.QUALIFIER == "DEFAULT", f"{module}/{name}"
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all poke-arn tests passed")
