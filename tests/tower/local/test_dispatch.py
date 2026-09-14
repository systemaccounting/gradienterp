"""dispatch.sh uploads the working tree, starts a workflow run on it and waits on it.

The run takes the version the upload printed, so a later upload can't change the tree it runs. A
deploy dispatch that names nothing to deploy is refused before anything uploads. `gh workflow run`
names no run, and a new run takes a few seconds to be listed: a watcher that looked once found
nothing and gave up (the terraform timing run, 2026-09-14), so the run is the new id among this
workflow's runs on this branch, looked up again until it appears, and the script's exit is the run's.
`zip.sh`, `upload.sh` and `gh` here are stand-ins that record what they were given."""

import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

ZIP = """#!/usr/bin/env bash
echo "$@" > "$STATE/zipped"
"""

UPLOAD = """#!/usr/bin/env bash
echo "$@" > "$STATE/uploaded"
echo "source.zip: put 5,378,117 bytes, commit da1131d5e28d, version V-uploaded-1"
"""

# `run list` answers the old run until the second poll after the dispatch, then the new one too
GH = """#!/usr/bin/env bash
case "$1 $2" in
  "run list")
    polls=$(cat "$STATE/polls" 2>/dev/null || echo 0)
    if [ -f "$STATE/dispatched" ]; then echo $((polls + 1)) > "$STATE/polls"; fi
    if [ -f "$STATE/dispatched" ] && [ "$polls" -ge 1 ]; then echo 101; fi
    echo 100 ;;
  "workflow run") echo "$@" > "$STATE/dispatched" ;;
  "run view")
    case "$*" in
      *--log-failed*) echo "deploy  the step  Error: the failing line" ;;
      *) echo "https://github.com/o/r/actions/runs/$3" ;;
    esac ;;
  "run watch") echo "$3" > "$STATE/watched"; exit "$WATCH_EXIT" ;;
esac
"""


def _dispatch(*args, watch_exit=0):
    root = Path(tempfile.mkdtemp())
    for name, text in (("scripts/dispatch.sh", (REPO / "scripts" / "dispatch.sh").read_text()),
                       ("scripts/zip.sh", ZIP), ("scripts/upload.sh", UPLOAD), ("bin/gh", GH)):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
        (root / name).chmod(0o755)
    (root / "state").mkdir()
    env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "STATE": str(root / "state"),
           "WATCH_EXIT": str(watch_exit), "DISPATCH_REF": "main", "HOME": str(root)}
    out = subprocess.run([shutil.which("bash"), str(root / "scripts" / "dispatch.sh"), *args],
                         capture_output=True, text=True, env=env, timeout=60)
    state = {p.name: p.read_text().strip() for p in (root / "state").iterdir()}
    shutil.rmtree(root)
    return out, state


def test_the_run_takes_the_uploaded_tree_and_is_the_new_one_watched():
    out, state = _dispatch("deploy.yaml", "--dirs", "modules/payments/lambdas/ingest_stripe", "-f", "gerp=westwood-c40fd8")
    assert out.returncode == 0, out.stderr
    assert state["zipped"] == "source --dirs modules/payments/lambdas/ingest_stripe"
    assert state["uploaded"] == "source"
    assert state["dispatched"] == ("workflow run deploy.yaml --ref main -f gerp=westwood-c40fd8 "
                                   "-f source_version=V-uploaded-1"), "the run is pinned to the version put"
    assert state["watched"] == "101", "the run listed before the dispatch is not the one watched"
    assert out.stdout.splitlines()[0] == "https://github.com/o/r/actions/runs/101"


def test_a_named_source_version_zips_and_uploads_nothing():
    out, state = _dispatch("apply.yaml", "-f", "stack=dns", "-f", "source_version=V-earlier")
    assert out.returncode == 0, out.stderr
    assert "zipped" not in state and "uploaded" not in state
    assert state["dispatched"].endswith("-f stack=dns -f source_version=V-earlier")


def test_a_deploy_that_names_nothing_is_refused_before_it_uploads():
    out, state = _dispatch("deploy.yaml", "-f", "gerp=all")
    assert out.returncode == 2 and "deploy nothing" in out.stderr
    assert state == {}, "nothing zipped, uploaded or dispatched"
    out, state = _dispatch("deploy.yaml", "-f", "image=none")
    assert out.returncode == 2 and state == {}


def test_an_image_only_deploy_uploads_the_tree_without_dirs():
    out, state = _dispatch("deploy.yaml", "-f", "image=build", "-f", "gerp=westwood-c40fd8")
    assert out.returncode == 0, out.stderr
    assert state["zipped"] == "source"
    assert state["dispatched"].endswith("-f image=build -f gerp=westwood-c40fd8 -f source_version=V-uploaded-1")


def test_a_failed_run_fails_the_script_with_the_failing_steps_log():
    out, state = _dispatch("apply.yaml", "-f", "stack=dns", watch_exit=1)
    assert out.returncode == 1 and state["watched"] == "101"
    assert "Error: the failing line" in out.stderr


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all dispatch tests passed")
