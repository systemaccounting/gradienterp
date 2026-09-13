"""How a handler exits: a refusal is the caller's 4xx, a failure is one `[ERROR]` line with the
ids and then the exit its invoke shape wants. Two shapes the code cannot get wrong by accident:

- an `except` whose next statement is a `print` — the line has no level, so no filter sees it.
  `aws.log.error` / `.warning` / `.info` is the line.
- a handler on a DynamoDB stream that raises poisons its shard, and one that returns after a
  failed record has consumed it. Every function a `modules/terraform/stream` mapping names goes
  through `aws.stream_batch` (python) or returns `batchItemFailures` itself (node).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SKIP_DIRS = {"vendor", "node_modules", "__pycache__"}


def handler_files(suffix=".py"):
    for root in (REPO / "modules", REPO / "prod"):
        for f in root.glob(f"*/lambdas/**/*{suffix}"):
            if not SKIP_DIRS & set(f.parts):
                yield f
        for f in root.glob(f"*/*/lambdas/**/*{suffix}"):
            if not SKIP_DIRS & set(f.parts):
                yield f


def test_no_except_exits_through_a_bare_print():
    bad = []
    for f in handler_files():
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            if not re.match(r"\s*except\b", line):
                continue
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines) and lines[j].strip().startswith("print("):
                bad.append(f"{f.relative_to(REPO)}:{j + 1}")
    assert not bad, "an except that prints has no level — use aws.log:\n  " + "\n  ".join(bad)


def test_no_bare_print_in_a_handler():
    """A `print` of a string has no level and no fields; a line is `aws.log.<level>(msg, **ids)`.
    A `print(json.dumps({...}))` is a data line a filter or a reader consumes and stays; a
    `print(<name>, ...)` is a tee of someone else's output (cmd streams the owner's script)."""
    bad = []
    for f in handler_files():
        for i, line in enumerate(f.read_text().splitlines()):
            s = line.strip()
            if re.match(r"""print\(\s*(f|r|b)?["']""", s):
                bad.append(f"{f.relative_to(REPO)}:{i + 1}")
    assert not bad, "a printed string has no level — use aws.log:\n  " + "\n  ".join(bad)


def stream_functions():
    """(module dir, function label) for every `module "<x>" { source = ".../terraform/stream" ... function_arn = module.<label>... }`."""
    out = []
    for tf in (REPO / "modules").glob("*/infra/*.tf"):
        text = tf.read_text()
        for m in re.finditer(r'\nmodule "(\w+)" \{(.*?)\n\}', text, re.S):
            body = m.group(2)
            if "terraform/stream" not in body:
                continue
            fn = re.search(r'function_arn\s*=\s*module\.(\w+)(?:\["(\w+)"\])?\.', body)
            assert fn, f"{tf}: stream module {m.group(1)} names no function"
            label = fn.group(2) or fn.group(1)
            out.append((tf.parent.parent, label))
    return out


def test_every_stream_handler_reports_batch_item_failures():
    found = stream_functions()
    assert len(found) >= 7, f"expected the seven stream mappings, found {len(found)}"
    bad = []
    for module_dir, label in found:
        # the function's src dir carries the label (the `gerp:src-dir` tag convention), with
        # `-` for `_` where the deployed name does (labor's close-handler)
        candidates = [module_dir / "lambdas" / label, module_dir / "lambdas" / label.replace("_", "-")]
        src = next((c for c in candidates if c.exists()), None)
        assert src, f"{module_dir.name}: no src dir for stream function {label}"
        py = src / "main.py"
        node = next(iter(src.glob("index.mjs")), None)
        text = py.read_text() if py.exists() else node.read_text()
        if "stream_batch(" not in text and "batchItemFailures" not in text:
            bad.append(f"{src.relative_to(REPO)}")
    assert not bad, "stream handlers that neither call aws.stream_batch nor return batchItemFailures:\n  " + "\n  ".join(bad)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all handler-exit tests passed")
