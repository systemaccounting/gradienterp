"""What apply.sh prints of a plan: one line per change and the counts, and on a failure the error.

CodeBuild echoes each buildspec command it runs, comments included, and a comment indented like a
plan's change line (`  # the row is written before the destroy starts…`) printed as a change in the
first live run. The change lines are terraform's `  # <address> will be …` / `must be …` alone."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import apply  # noqa: E402


def test_a_change_line_prints_and_an_echoed_comment_does_not():
    changes = ["  # module.bff.aws_lambda_function.this will be updated in-place",
               '  # aws_s3_object.platform_standards["gerp/scheduling.md"] will be updated in-place',
               "  # module.x.aws_y.z must be replaced",
               "Plan: 0 to add, 1 to change, 0 to destroy.",
               "No changes. Your infrastructure matches the configuration."]
    echoed = ["  # the row is written before the destroy starts, so an interrupted build can be read rather",
              "  # one pass: the agent's gateway reaches every module as an input through the graph",
              '      + environment = { "PROVISION_QUEUE" = "x" }']
    assert all(apply.summary_line(ln) for ln in changes)
    assert not any(apply.summary_line(ln) for ln in echoed)


# a terraform whose plan fails: an attribute value on stdout, the error on stderr
TERRAFORM = """#!/usr/bin/env bash
case "$1" in
  init) echo "Terraform has been successfully initialized!";;
  plan) echo '  # aws_lambda_function.fn will be updated in-place'
        echo '      ~ environment = { "STRIPE_KEY" = "rk_live_not_for_a_public_log" }'
        echo 'Error: creating Lambda Function: AccessDenied' >&2
        exit 1;;
esac
"""


def test_a_failed_plan_prints_the_error_and_not_the_plan():
    """On GitHub the run log is public, and a plan's stdout is where attribute values are."""
    root = Path(tempfile.mkdtemp())
    (root / "stack").mkdir()
    (root / "bin").mkdir()
    (root / "bin" / "terraform").write_text(TERRAFORM)
    (root / "bin" / "terraform").chmod(0o755)
    saved_repo, saved_path = apply.REPO, os.environ["PATH"]
    apply.REPO, os.environ["PATH"] = str(root), f"{root / 'bin'}:{saved_path}"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = apply.apply_stack("stack", plan_only=False)
    finally:
        apply.REPO, os.environ["PATH"] = saved_repo, saved_path
        shutil.rmtree(root)
    text = out.getvalue()
    assert rc == 1
    assert "Error: creating Lambda Function: AccessDenied" in text
    assert "aws_lambda_function.fn will be updated in-place" in text, "the change line still names what failed"
    assert "rk_live_not_for_a_public_log" not in text


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all apply output tests passed")
