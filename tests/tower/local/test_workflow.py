"""workflow.sh: start a run on the working tree, wait on runs, read their logs. `zip.sh`, `upload.sh` and
`gh` here are stand-ins that record what they were given.

`run` pins the dispatch to the version the upload printed, so a later upload can't change the tree it
runs, and refuses a deploy that names nothing before anything uploads. A new run takes seconds to be
listed, for a dispatch and for a push alike: a watcher that looked once found nothing and exited (the CI
watcher for da1131d and the first terraform timing run, 2026-09-14). So `run` looks for the id that
wasn't listed before its dispatch, and `wait --commit` looks until every workflow whose `on:` names the
event is listed, then waits on all of them."""

import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SHA = "6e665710d9b3a1d9a0b1c2d3e4f5a6b7c8d9e0f1"

ZIP = """#!/usr/bin/env bash
echo "$@" > "$STATE/zipped"
"""

UPLOAD = """#!/usr/bin/env bash
echo "$@" > "$STATE/uploaded"
echo "source.zip: put 5,378,117 bytes, commit da1131d5e28d, version V-uploaded-1"
"""

# a dispatch's run is listed from the second poll; a push's runs list `unit` first, the rest a poll
# later (or never, with COMMIT_RUNS=unit); a run in FAIL_IDS fails
GH = """#!/usr/bin/env bash
fails() { case " $FAIL_IDS " in *" $1 "*) return 0 ;; esac; return 1; }
case "$1 $2" in
  "run list")
    case "$*" in
      *--commit*)
        polls=$(cat "$STATE/commit_polls" 2>/dev/null || echo 0); echo $((polls + 1)) > "$STATE/commit_polls"
        printf 'unit\\t201\\n'
        if [ "$polls" -ge 1 ] && [ "${COMMIT_RUNS:-all}" = all ]; then printf 'e2e\\t202\\nterraform\\t203\\n'; fi ;;
      *)
        polls=$(cat "$STATE/polls" 2>/dev/null || echo 0)
        if [ -f "$STATE/dispatched" ]; then echo $((polls + 1)) > "$STATE/polls"; fi
        if [ -f "$STATE/dispatched" ] && [ "$polls" -ge 1 ]; then echo 101; fi
        echo 100 ;;
    esac ;;
  "workflow run") echo "$@" > "$STATE/dispatched" ;;
  "run view")
    case "$*" in
      *--log-failed*) echo "failing steps of $3" ;;
      *--log*) echo "whole log of $3" ;;
      *conclusion*) if fails "$3"; then echo failure; else echo success; fi ;;
      *) echo "https://github.com/o/r/actions/runs/$3" ;;
    esac ;;
  "run watch") echo "$3" >> "$STATE/watched"; if fails "$3"; then exit 1; fi ;;
esac
"""


def _workflow(*args, fail_ids="", **env_extra):
    root = Path(tempfile.mkdtemp())
    for name, text in (("scripts/workflow.sh", (REPO / "scripts" / "workflow.sh").read_text()),
                       ("scripts/zip.sh", ZIP), ("scripts/upload.sh", UPLOAD), ("bin/gh", GH)):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
        (root / name).chmod(0o755)
    shutil.copytree(REPO / ".github" / "workflows", root / ".github" / "workflows")
    (root / "state").mkdir()
    env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "STATE": str(root / "state"),
           "FAIL_IDS": fail_ids, "WORKFLOW_REF": "main", "HOME": str(root), **env_extra}
    out = subprocess.run([shutil.which("bash"), str(root / "scripts" / "workflow.sh"), *args],
                         capture_output=True, text=True, env=env, timeout=60)
    state = {p.name: p.read_text().strip() for p in (root / "state").iterdir()}
    shutil.rmtree(root)
    return out, state


# ── run ──

def test_run_takes_the_uploaded_tree_and_waits_on_the_new_run():
    out, state = _workflow("run", "deploy.yaml", "--dirs", "modules/payments/lambdas/ingest_stripe", "-f", "gerp=westwood-c40fd8")
    assert out.returncode == 0, out.stderr
    assert state["zipped"] == "source --dirs modules/payments/lambdas/ingest_stripe"
    assert state["uploaded"] == "source"
    assert state["dispatched"] == ("workflow run deploy.yaml --ref main -f gerp=westwood-c40fd8 "
                                   "-f source_version=V-uploaded-1"), "the run is pinned to the version put"
    assert state["watched"] == "101", "the run listed before the dispatch is not the one watched"
    assert out.stdout.splitlines()[:2] == ["run 101", "https://github.com/o/r/actions/runs/101"]


def test_run_splits_a_dirs_or_f_value_that_arrived_as_one_word():
    """zsh does not split `$dirs`, so `--dirs $dirs` arrives as one word holding three, and
    `${V:+-f k=$V}` as one word holding the flag and its value (2026-09-19, twice). The script takes
    the words apart, so a caller from any shell means the same thing."""
    out, state = _workflow("run", "deploy.yaml", "--dirs", "modules/a/lambdas/x modules/b/lambdas/y", "-f gerp=westwood-c40fd8")
    assert state["zipped"] == "source --dirs modules/a/lambdas/x modules/b/lambdas/y"
    assert "-f gerp=westwood-c40fd8" in state["dispatched"]


def test_run_with_a_named_source_version_zips_and_uploads_nothing():
    out, state = _workflow("run", "apply.yaml", "-f", "stack=dns", "-f", "source_version=V-earlier")
    assert out.returncode == 0, out.stderr
    assert "zipped" not in state and "uploaded" not in state
    assert state["dispatched"].endswith("-f stack=dns -f source_version=V-earlier")


def test_run_of_a_deploy_that_names_nothing_is_refused_before_it_uploads():
    for args in (("-f", "gerp=all"), ("-f", "image=none")):
        out, state = _workflow("run", "deploy.yaml", *args)
        assert out.returncode == 2 and "deploy nothing" in out.stderr
        assert state == {}, "nothing zipped, uploaded or dispatched"


def test_run_of_an_image_only_deploy_uploads_the_tree_without_dirs():
    out, state = _workflow("run", "deploy.yaml", "-f", "image=build", "-f", "gerp=westwood-c40fd8")
    assert out.returncode == 0, out.stderr
    assert state["zipped"] == "source"
    assert state["dispatched"].endswith("-f image=build -f gerp=westwood-c40fd8 -f source_version=V-uploaded-1")


def test_run_no_wait_prints_the_run_and_watches_nothing():
    out, state = _workflow("run", "apply.yaml", "-f", "stack=dns", "--no-wait")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["run 101", "https://github.com/o/r/actions/runs/101"]
    assert "watched" not in state


# ── wait ──

def test_wait_on_a_run_exits_with_its_result_and_prints_a_failures_steps():
    out, state = _workflow("wait", "101")
    assert out.returncode == 0 and state["watched"] == "101"
    out, state = _workflow("wait", "101", fail_ids="101")
    assert out.returncode == 1 and "failing steps of 101" in out.stderr


def test_wait_on_a_commit_waits_for_every_push_workflow_to_be_listed():
    out, state = _workflow("wait", "--commit", SHA)
    assert out.returncode == 0, out.stderr
    assert sorted(state["watched"].split()) == ["201", "202", "203"], "a run listed a poll late is still waited on"
    assert int(state["commit_polls"]) >= 2
    assert sorted(out.stdout.splitlines()) == ["e2e success (run 202)", "terraform success (run 203)", "unit success (run 201)"]


def test_wait_on_a_commit_fails_when_one_run_fails_and_names_it():
    out, state = _workflow("wait", "--commit", SHA, fail_ids="203")
    assert out.returncode == 1
    assert "terraform failure (run 203)" in out.stdout and "unit success (run 201)" in out.stdout
    assert "failing steps of 203" in out.stderr and "failing steps of 201" not in out.stderr


def test_wait_on_a_commit_fails_naming_a_workflow_never_listed():
    out, state = _workflow("wait", "--commit", SHA, COMMIT_RUNS="unit", WORKFLOW_APPEAR_SECONDS="2")
    assert out.returncode == 1
    assert "e2e terraform" in out.stderr and "unit" not in out.stderr.split("run of")[1].split("for")[0]
    assert "watched" not in state, "nothing is waited on until every expected run is listed"


# ── log ──

def test_log_prints_the_whole_log_or_the_failing_steps():
    out, _ = _workflow("log", "101")
    assert out.returncode == 0 and out.stdout.strip() == "whole log of 101"
    out, _ = _workflow("log", "101", "--failed")
    assert out.returncode == 0 and out.stdout.strip() == "failing steps of 101"


def test_run_of_a_workflow_that_runs_on_its_checkout_uploads_nothing_and_takes_no_dirs():
    out, state = _workflow("run", "playbooks.yaml", "-f", "gerp=all")
    assert out.returncode == 0, out.stderr
    assert "zipped" not in state and "uploaded" not in state
    assert state["dispatched"].endswith("-f gerp=all")
    out, state = _workflow("run", "playbooks.yaml", "--dirs", "modules/x/lambdas/y")
    assert out.returncode == 2 and "takes no --dirs" in out.stderr and state == {}


def test_wait_on_a_commit_expects_no_workflow_behind_a_paths_filter():
    """playbooks.yaml runs on a push only when a kb.md changed, image-check.yaml on a pull request only
    when the image's inputs did: a wait cannot know which, so neither is expected and neither is
    named missing."""
    out, state = _workflow("wait", "--commit", SHA)
    assert out.returncode == 0, out.stderr
    assert "playbooks" not in out.stdout + out.stderr and "image-check" not in out.stdout + out.stderr


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all workflow.sh tests passed")
