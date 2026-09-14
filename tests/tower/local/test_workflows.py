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


def test_the_deploy_matrices_fan_out_over_the_resolved_gerps_and_the_image_builds_on_arm():
    jobs = _load(REPO / ".github" / "workflows" / "deploy.yaml")["jobs"]
    for name in ("lambdas", "runtimes"):
        assert jobs[name]["strategy"]["matrix"]["gerp"] == "${{ fromJSON(needs.gerps.outputs.list) }}", name
        assert jobs[name]["strategy"]["fail-fast"] is False, f"{name}: one gerp's failure stops no other"
    assert jobs["build"]["runs-on"] == "ubuntu-24.04-arm"
    assert "needs" not in jobs["build"] or jobs["build"]["needs"] == "gerps", "the build waits on no deploy"
    assert "lambdas" not in (jobs["runtimes"]["needs"] or []), "the image and the lambdas run at once"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all workflow tests passed")
