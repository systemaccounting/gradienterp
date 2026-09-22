"""tf-validate-all.sh runs the directories at once, so a failure has to survive the fan-out: a worker
that fails is named at the end and fails the run, and the log reads in directory order whichever
worker finishes first. Terraform here is a stand-in on PATH."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / ".github/workflows/tf-validate-all.sh"

# init fails in a dir named broken_init, validate in broken_validate, a warning comes from warned; dir
# `slow` finishes last
TERRAFORM = """#!/usr/bin/env bash
d=$(basename "$PWD")
[[ $d == slow ]] && sleep 1
case "$1" in
  init) [[ $d == broken_init ]] && { echo "Error: no provider"; exit 1; }; echo initialized;;
  validate)
    # terraform validate -json: the document on stdout, anything else on stderr
    [[ -f zz_validate_providers.tf ]] && cat zz_validate_providers.tf >&2
    if [[ $d == broken_validate ]]; then
      echo '{"valid":false,"diagnostics":[{"severity":"error","summary":"bad reference","detail":"no such thing","range":{"filename":"main.tf","start":{"line":1}}}]}'; exit 1
    fi
    if [[ $d == warned ]]; then
      echo '{"valid":true,"diagnostics":[{"severity":"warning","summary":"Deprecated attribute","detail":"The attribute \\"id\\" is deprecated.","range":{"filename":"main.tf","start":{"line":3}}}]}'; exit 0
    fi
    echo '{"valid":true,"diagnostics":[]}';;
esac
"""


def _run(dirs, extra_tf=None):
    root = Path(tempfile.mkdtemp())
    try:
        (root / ".github/workflows").mkdir(parents=True)
        shutil.copy(SCRIPT, root / ".github/workflows/tf-validate-all.sh")
        for d in dirs:
            (root / d).mkdir()
            (root / d / "main.tf").write_text((extra_tf or {}).get(d, "locals {}\n"))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "terraform").write_text(TERRAFORM)
        (bin_dir / "terraform").chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TF_VALIDATE_JOBS": "4"}
        out = subprocess.run([shutil.which("bash"), str(root / ".github/workflows/tf-validate-all.sh")],
                             capture_output=True, text=True, env=env, timeout=60)
        left = sorted(str(p.relative_to(root)) for p in root.rglob("zz_validate_providers.tf"))
        return out.returncode, out.stdout, left
    finally:
        shutil.rmtree(root)


def test_a_failed_directory_fails_the_run_and_is_named():
    rc, out, _ = _run(["broken_init", "broken_validate", "ok"])
    assert rc == 1, out
    assert "2 of 3 dirs failed:" in out, out
    assert "  - broken_init (init)" in out and "  - broken_validate (validate)" in out, out
    assert "Error: no provider" in out and "Error: bad reference" in out, out


def test_a_warning_fails_the_directory_and_prints_where():
    """validate -json: a deprecation warning is a failure, named with its file and line, so validate
    says nothing when nothing is wrong."""
    rc, out, _ = _run(["ok", "warned"])
    assert rc == 1, out
    assert "  - warned (warning)" in out and "1 of 2 dirs failed:" in out, out
    assert "Warning: Deprecated attribute (main.tf:3)" in out and 'The attribute "id" is deprecated.' in out, out


def test_every_directory_valid_passes():
    rc, out, _ = _run(["a", "b", "c"])
    assert rc == 0, out
    assert "all 3 dirs valid" in out, out


def test_the_log_reads_in_directory_order_whichever_finishes_first():
    rc, out, _ = _run(["a_first", "slow", "z_last"])
    assert rc == 0, out
    heads = [line for line in out.splitlines() if line.startswith("=== ")]
    assert heads == ["=== a_first ===", "=== slow ===", "=== z_last ==="], heads


def test_an_aliased_provider_is_declared_for_its_validate_and_removed_after():
    tf = {"mod": 'terraform {\n  required_providers {\n    aws = { configuration_aliases = [aws.hub] }\n  }\n}\n'}
    rc, out, left = _run(["mod"], tf)
    assert rc == 0, out
    assert 'provider "aws" {\n  alias = "hub"\n}' in out, out
    assert left == [], left


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all tf-validate-all tests passed")
