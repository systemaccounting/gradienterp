"""The workflows' wiring between jobs and steps, read from the yaml.

A job reads another's result as `needs.<job>.outputs.<name>` and a step's as `steps.<id>.outputs.<name>`.
A name that isn't there evaluates to an empty string, not an error: a matrix over it fails at dispatch,
and a `--tag` from it is silently absent. So every such read names a job this one needs, an output that
job declares, or a step of this job that runs before it; and a composite action's declared outputs
name steps it has."""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yaml"))
ACTIONS = sorted((REPO / ".github" / "actions").glob("*/action.yaml"))

NEEDS_READ = re.compile(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)")
STEP_READ = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)")


def _load(path):
    doc = yaml.safe_load(path.read_text())
    doc.setdefault("on", doc.pop(True, None))   # yaml reads a bare `on:` key as True
    return doc


def test_every_needs_read_names_a_needed_job_and_its_declared_output():
    problems = []
    for wf in WORKFLOWS:
        jobs = _load(wf).get("jobs", {})
        for name, job in jobs.items():
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            for other, output in NEEDS_READ.findall(yaml.safe_dump(job)):
                if other not in needs:
                    problems.append(f"{wf.name} {name}: reads needs.{other} but doesn't need it")
                elif output not in (jobs.get(other, {}).get("outputs") or {}):
                    problems.append(f"{wf.name} {name}: reads needs.{other}.outputs.{output}, which {other} doesn't declare")
    assert not problems, "\n".join(problems)


def test_every_step_read_names_an_earlier_step_of_the_same_job():
    problems = []
    for wf in WORKFLOWS:
        for name, job in _load(wf).get("jobs", {}).items():
            seen = set()
            for step in job.get("steps", []):
                for sid, _ in STEP_READ.findall(yaml.safe_dump(step)):
                    if sid not in seen:
                        problems.append(f"{wf.name} {name}: a step reads steps.{sid} before any step has that id")
                if "id" in step:
                    seen.add(step["id"])
            ids = {s["id"] for s in job.get("steps", []) if "id" in s}
            for sid, _ in STEP_READ.findall(yaml.safe_dump(job.get("outputs") or {})):
                if sid not in ids:
                    problems.append(f"{wf.name} {name}: an output reads steps.{sid}, which no step is")
    for action in ACTIONS:
        doc = yaml.safe_load(action.read_text())
        ids = {s["id"] for s in doc["runs"]["steps"] if "id" in s}
        for sid, _ in STEP_READ.findall(yaml.safe_dump(doc.get("outputs") or {})):
            if sid not in ids:
                problems.append(f"{action.parent.name}: an output reads steps.{sid}, which no step is")
    assert not problems, "\n".join(problems)


def test_the_lambdas_job_is_the_fleet_pipe_the_runtimes_fan_out_and_the_image_builds_on_arm():
    """deploy.yaml (issue #59): `lambdas` is one job over the fleet, the pipe `deploy.sh fleet` runs on
    a laptop with GNU parallel in the xargs slot; `runtimes` still fans out per gerp, since a runtime
    waits minutes for READY and the image × runtime loop is not yet a piece."""
    jobs = _load(REPO / ".github" / "workflows" / "deploy.yaml")["jobs"]
    assert "strategy" not in jobs["lambdas"], "one job over the fleet"
    fleet_step = next(st for st in jobs["lambdas"]["steps"] if st.get("name") == "fleet")["run"]
    for needle in ("fleet.py listzipversions", "parallel -k -j 10 --halt never", "fleet.py listzipfnsconf --gerp {}", "fleet.py status", "fleet.py update", "::group::fleet", "GITHUB_STEP_SUMMARY"):
        assert needle in fleet_step, needle
    push_step = next(st for st in jobs["lambdas"]["steps"] if st.get("name") == "push")["run"]
    assert "fleet.py push" in push_step and "npm ci" in push_step, "the artifacts once, before the pipe"
    assert jobs["lambdas"]["timeout-minutes"] == "${{ fromJSON(needs.gerps.outputs.lambdas_timeout) }}", "a minute a gerp"
    assert jobs["runtimes"]["strategy"]["matrix"]["gerp"] == "${{ fromJSON(needs.gerps.outputs.list) }}"
    assert jobs["runtimes"]["strategy"]["fail-fast"] is False, "one gerp's failure stops no other"
    assert jobs["build"]["runs-on"] == "ubuntu-24.04-arm"
    assert "needs" not in jobs["build"] or jobs["build"]["needs"] == "gerps", "the build waits on no deploy"
    assert "lambdas" not in (jobs["runtimes"]["needs"] or []), "the image and the lambdas run at once"


def test_the_two_fleet_compositions_are_the_same_pieces_in_the_same_order():
    """modules/terraform AGENTS: the laptop's pipe (deploy.sh fleet) and the runner's (deploy.yaml
    lambdas) are identical in logic; a runner thing is written in the yaml, never in a script."""
    sh = (REPO / "scripts" / "deploy.sh").read_text()
    laptop = re.findall(r"fleet\.py (\w+)", sh[sh.index("fleet.py listzipversions"):sh.index("fleet.py update") + len("fleet.py update")])
    jobs = _load(REPO / ".github" / "workflows" / "deploy.yaml")["jobs"]
    fleet_step = next(st for st in jobs["lambdas"]["steps"] if st.get("name") == "fleet")["run"]
    runner = re.findall(r"fleet\.py (\w+)", fleet_step)
    assert laptop == runner == ["listzipversions", "listgerps", "listzipfnsconf", "status", "update"], (laptop, runner)
    assert "-P 10" in sh and "-j 10" in fleet_step, "ten at a time on both"
    for f in sorted((REPO / "scripts").glob("*.py")) + sorted((REPO / "scripts").glob("*.sh")):
        assert "GITHUB_ACTIONS" not in f.read_text(), f"{f.name} branches on the runner"


def test_playbooks_runs_on_a_kb_change_by_dispatch_or_by_call_and_finds_the_knowledge_base_by_name():
    """playbooks.yaml (issue #42): GitHub's paths filter is the change detection; the dispatch and the
    call take a gerp; every job deploys from main through prod; the sync step finds the knowledge
    base and data source by name in the gerp's account and hands the script its five arguments."""
    wf = _load(REPO / ".github" / "workflows" / "playbooks.yaml")
    on = wf["on"]
    assert on["push"] == {"branches": ["main"], "paths": ["modules/**/kb.md"]}
    assert "gerp" in on["workflow_dispatch"]["inputs"] and "gerp" in on["workflow_call"]["inputs"]
    assert all(job["environment"] == "prod" for job in wf["jobs"].values())
    # the per-gerp step is a script a laptop runs the same; the yaml runs the list through GNU parallel
    script = (REPO / "scripts" / "sync_gerp_playbooks.sh").read_text()
    for needle in ("list-knowledge-bases", 'playbooks-${GERP//_/-}', "list-data-sources", '"repo-playbooks"'):
        assert needle in script, needle
    assert 'bash scripts/sync_playbooks.sh "$GERP" "$kb" "$ds" "gerp-$GERP" "$region"' in script
    assert "strategy" not in wf["jobs"]["sync"], "one job over the fleet"
    sync = next(st for st in wf["jobs"]["sync"]["steps"] if st.get("name") == "sync")["run"]
    for needle in ("parallel -k -j 10 --halt never --joblog", "bash scripts/sync_gerp_playbooks.sh {}", "::group::{}", "GITHUB_STEP_SUMMARY", "exit 1"):
        assert needle in sync, needle
    assert wf["jobs"]["sync"]["timeout-minutes"] == "${{ fromJSON(needs.gerps.outputs.timeout) }}", "a minute a gerp"


def test_deploy_composes_playbooks_only_when_asked():
    wf = _load(REPO / ".github" / "workflows" / "deploy.yaml")
    assert wf["on"]["workflow_dispatch"]["inputs"]["playbooks"]["default"] is False
    job = wf["jobs"]["playbooks"]
    assert job["uses"] == "./.github/workflows/playbooks.yaml"
    assert job["with"]["gerp"] == "${{ inputs.gerp }}" and job["secrets"] == "inherit"
    assert "inputs.playbooks" in job["if"] and "lambdas" in job["needs"]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all workflow tests passed")
