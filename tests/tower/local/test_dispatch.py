"""dispatch.sh starts a workflow run and waits on it. `gh workflow run` names no run, and a new run
takes a few seconds to be listed: a watcher that looked once found nothing and gave up (the
terraform timing run, 2026-09-14). The run is the new id among this workflow's runs on this branch,
looked up again until it appears, and the script's exit status is the run's. `gh` here is a stand-in."""

import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# `run list` answers the old run until the second poll after the dispatch, then the new one too
GH = """#!/usr/bin/env bash
state="$STATE"
case "$1 $2" in
  "run list")
    polls=$(cat "$state/polls" 2>/dev/null || echo 0)
    if [ -f "$state/dispatched" ]; then echo $((polls + 1)) > "$state/polls"; fi
    if [ -f "$state/dispatched" ] && [ "$polls" -ge 1 ]; then echo 101; fi
    echo 100 ;;
  "workflow run") echo "$@" > "$state/dispatched" ;;
  "run view")
    case "$*" in
      *--log-failed*) echo "deploy  the step  Error: the failing line" ;;
      *) echo "https://github.com/o/r/actions/runs/$3" ;;
    esac ;;
  "run watch") echo "$3" > "$state/watched"; exit "$WATCH_EXIT" ;;
esac
"""


def _dispatch(watch_exit):
    root = Path(tempfile.mkdtemp())
    (root / "bin").mkdir()
    (root / "bin" / "gh").write_text(GH)
    (root / "bin" / "gh").chmod(0o755)
    (root / "state").mkdir()
    env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "STATE": str(root / "state"),
           "WATCH_EXIT": str(watch_exit), "DISPATCH_REF": "deploy-workflows", "HOME": str(root)}
    out = subprocess.run([shutil.which("bash"), str(REPO / "scripts" / "dispatch.sh"), "deploy.yaml", "-f", "gerp=westwood-c40fd8"],
                         capture_output=True, text=True, env=env, timeout=60)
    state = {p.name: p.read_text().strip() for p in (root / "state").iterdir()}
    shutil.rmtree(root)
    return out, state


def test_the_run_watched_is_the_new_one_and_it_decides_the_exit():
    out, state = _dispatch(0)
    assert out.returncode == 0, out.stderr
    assert state["dispatched"] == "workflow run deploy.yaml --ref deploy-workflows -f gerp=westwood-c40fd8"
    assert state["watched"] == "101", "the run listed before the dispatch is not the one watched"
    assert out.stdout.splitlines()[0] == "https://github.com/o/r/actions/runs/101"


def test_a_failed_run_fails_the_script_with_the_failing_steps_log():
    out, state = _dispatch(1)
    assert out.returncode == 1 and state["watched"] == "101"
    assert "Error: the failing line" in out.stderr


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all dispatch tests passed")
