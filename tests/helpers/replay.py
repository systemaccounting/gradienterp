"""
Replay provider webhook fixtures through their transform functions.

Reads tests/scenarios.jsonc, loads each listed fixture from
tests/testdata/<provider>/<fixture>, applies optional `overrides` (dot-paths),
and calls the matching transform_<provider>_<event>() function in
modules/accounting/lambdas/ingest/transform.py. Writes the resulting
post_journal_entry input shapes as JSON Lines.

Output is production-realistic — no accountType. If a scenario references a
transform function that doesn't exist yet, it's skipped with a warning.

Usage:
  python replay.py
  python replay.py --out entries.jsonl
"""

import argparse
import copy
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "modules" / "accounting" / "lambdas" / "ingest"))

import transform  # noqa: E402

SCENARIOS_JSONC = REPO_ROOT / "tests" / "scenarios.jsonc"
TESTDATA_ROOT = REPO_ROOT / "tests" / "testdata"


_JSONC_PATTERN = re.compile(
    r'"(?:\\.|[^"\\])*"'    # string literal (preserves // and /* inside strings)
    r'|//[^\n]*'            # // line comment
    r'|/\*.*?\*/',          # /* block */ comment
    re.DOTALL,
)


def load_jsonc(path):
    with open(path) as f:
        text = f.read()
    cleaned = _JSONC_PATTERN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)  # strip trailing commas
    return json.loads(cleaned)


def load_scenarios(path):
    return load_jsonc(path).get("scenarios") or []


def fixture_to_suffix(fixture):
    """charge.succeeded.json -> charge_succeeded
       PAYMENT.CAPTURE.COMPLETED.json -> payment_capture_completed"""
    stem = re.sub(r"\.json$", "", fixture)
    return re.sub(r"[.\-]", "_", stem).lower()


def find_transform(provider, fixture):
    name = f"transform_{provider}_{fixture_to_suffix(fixture)}"
    return getattr(transform, name, None), name


def apply_overrides(payload, overrides):
    out = copy.deepcopy(payload)
    for dot_path, value in overrides.items():
        keys = dot_path.split(".")
        node = out
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="-", help="output file path or '-' for stdout")
    args = parser.parse_args()

    scenarios = load_scenarios(SCENARIOS_JSONC)

    entries = []
    for s in scenarios:
        provider = s["provider"]
        fixture = s["fixture"]
        fn, fn_name = find_transform(provider, fixture)
        if fn is None:
            sys.stderr.write(f"skip {provider}/{fixture}: transform.{fn_name} not defined\n")
            continue
        with open(TESTDATA_ROOT / provider / fixture) as f:
            payload = json.load(f)
        if s.get("overrides"):
            payload = apply_overrides(payload, s["overrides"])
        entries.append(fn(payload))

    fh = sys.stdout if args.out == "-" else open(args.out, "w")
    try:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    finally:
        if fh is not sys.stdout:
            fh.close()


if __name__ == "__main__":
    main()
