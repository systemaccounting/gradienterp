"""Enumerate the test files a run will execute, and how many tests each ACTUALLY runs.

A test file is a script: it defines `test_*` functions and a `if __name__ == "__main__":` block that
calls them. The count used to come from `grep -c '^def test_'`, which counts what is DEFINED — so a
function nobody calls was reported as a passing test. Two shapes of that bug have already shipped:
five files with no runner block at all, and one whose block runs a hardcoded list that a later test
was never added to.

Both are the same question — which of the defined tests does the block reach? — and it is
answerable without running anything:

  * a block containing `globals()` iterates every test in the module, so all of them run
  * otherwise a test runs only if its name appears in the block

Output is one tab-separated job per line, for the shell to read:

    kind \t path \t label \t count \t orphans

`orphans` is a comma-separated list of defined-but-unreached test names, empty when there are none.
The caller fails those files rather than silently counting them.

Usage: collect_tests.py <tests_dir> <env> [module] [name-substring]
"""

import ast
import pathlib
import re
import sys


def _runner_and_tests(src: str) -> tuple[list[str], list[str]]:
    """(tests that will run, tests defined but unreachable) for one python test file."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], []

    defined = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    if not defined:
        return [], []

    # the `if __name__ == "__main__":` block, as source
    block = None
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            block = "\n".join(ast.unparse(s) for s in node.body)
            break
    if block is None:
        return [], defined                      # nothing runs; every test is orphaned

    if "globals()" in block:
        return defined, []                      # the loop idiom reaches all of them

    reached = [t for t in defined if re.search(rf"\b{re.escape(t)}\b", block)]
    return reached, [t for t in defined if t not in reached]


def main() -> int:
    tests_dir = pathlib.Path(sys.argv[1])
    env = sys.argv[2]
    module = sys.argv[3] if len(sys.argv) > 3 else ""
    name = sys.argv[4] if len(sys.argv) > 4 else ""

    module_dirs = [tests_dir / module] if module else sorted(
        d for d in tests_dir.iterdir() if d.is_dir() and (d / "local").is_dir() or (d / "integ").is_dir())

    for mod in module_dirs:
        env_dir = mod / env
        if not env_dir.is_dir():
            continue
        for f in sorted(env_dir.glob("test_*.py")):
            if name and name not in f.stem:
                continue
            runs, orphans = _runner_and_tests(f.read_text())
            label = f"{mod.name}/{env}/{f.stem} ({len(runs)} tests)"
            print(f"py\t{f}\t{label}\t{len(runs)}\t{','.join(orphans)}")
        # node's runner discovers and runs every `test(`/`it(` in the file — nothing to orphan
        for f in sorted(env_dir.glob("*.test.mjs")):
            stem = f.name[: -len(".test.mjs")]
            if name and name not in stem:
                continue
            n = len(re.findall(r"^[ \t]*(?:test|it)\(", f.read_text(), re.M))
            print(f"node\t{f}\t{mod.name}/{env}/{stem} ({n} tests, node)\t{n}\t")
    return 0


if __name__ == "__main__":
    sys.exit(main())
