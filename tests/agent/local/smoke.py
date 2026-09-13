"""Live smoke test for the agent dev harness.

Runs the scripted conversation in prompts.jsonc against the real Anthropic API.
Hits each tool call against the real local lambda handlers. Costs a few cents
per run. Not a pytest — that's `test_bookkeeper_flow.py` (mocked, free).

Use to validate conversation quality and tool dispatch after prompt changes.
Use the mocked test to gate commits.

Usage (via wrapper that bootstraps .venv + env):
    bash tests/agent/local/smoke.sh
    bash tests/agent/local/smoke.sh --category post,query
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent / "modules" / "agent" / "dev"))

from agent import load_system_prompt, run_turn  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2].parent
DEFAULT_PROMPTS = Path(__file__).resolve().parent / "prompts.jsonc"

# Artifacts we inspect at end-of-run. Any path set in env gets reported on.
ARTIFACT_ENV_VARS = [
    "LOCAL_LEDGER", "LOCAL_PENDING", "LOCAL_BALANCES",
    "LOCAL_SCHEMA", "LOCAL_SECRETS", "LOCAL_COA_REQUESTS", "LOCAL_CONFIG",
    "LOCAL_S3",
]


_JSONC_PATTERN = re.compile(
    r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
    re.DOTALL,
)


def load_jsonc(path):
    text = path.read_text()
    cleaned = _JSONC_PATTERN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return json.loads(cleaned)


def main():
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.stderr.write("requires: pip3 install anthropic (or python3 -m pip install anthropic)\n")
        sys.exit(1)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompts", default=str(DEFAULT_PROMPTS), help="path to prompts jsonc catalog")
    parser.add_argument("--category", help="comma-separated categories to include (default: all)")
    args = parser.parse_args()

    prompts_path = Path(args.prompts)
    prompts = load_jsonc(prompts_path)["prompts"]
    if args.category:
        wanted = set(c.strip() for c in args.category.split(","))
        prompts = [p for p in prompts if p.get("category") in wanted]
        if not prompts:
            sys.stderr.write(f"no prompts matched categories: {wanted}\n")
            sys.exit(1)

    client = Anthropic()
    system = load_system_prompt()
    messages = []

    print(f"=== mode={os.environ.get('AGENT_MODE', 'bookkeeper')} prompts={prompts_path.name} ({len(prompts)} turns) ===", flush=True)

    for p in prompts:
        print(f"\n>>> [{p.get('category', '?')}] {p['input']}", flush=True)
        messages.append({"role": "user", "content": p["input"]})
        reply = run_turn(client, system, messages)
        print(f"<<< {reply}", flush=True)

    # artifact summary — which local stores got writes this run
    print("\n--- artifacts ---", flush=True)
    for var in ARTIFACT_ENV_VARS:
        path = os.environ.get(var)
        if not path:
            continue
        p = Path(path)
        if p.is_dir():
            files = [f for f in p.rglob("*") if f.is_file()]
            print(f"  {var} ({p.name}/): {len(files)} file(s)", flush=True)
        elif p.exists():
            lines = [l for l in p.read_text().splitlines() if l.strip()]
            print(f"  {var} ({p.name}): {len(lines)} row(s)", flush=True)
        else:
            print(f"  {var} ({p.name}): —", flush=True)


if __name__ == "__main__":
    main()
