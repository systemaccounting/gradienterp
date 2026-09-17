"""What CodeBuild runs for a gerp: the buildspec's order and guards, and the shapes of the
per_customer template a fresh vend's first plan depends on.

What the source zip holds — every module the templates instantiate, the buildspec, the repo-root
files a template reads — is `test_zip.py`'s, against the zip `zip.sh source` builds.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILDSPEC = REPO / ".codebuild" / "per-customer.yml"
MODULES = REPO / "modules"


def test_the_guides_are_synced_before_the_row_is_marked_active():
    """A gerp is not ready with an empty shelf: the agent's search_guides reads the gerp's own KB,
    terraform only creates it, and westwood's was empty after its vend (2026-09-04). The sync runs
    in post_build, on an apply, before the stamp that marks the row active — so a sync that fails
    fails the build and the row stays provisioning, which the inbox hears."""
    text = BUILDSPEC.read_text()
    post = text[text.index("post_build:"):]
    sync, active = post.index("sync_playbooks.sh"), post.index('echo "marked ${CUSTOMER_ID} active')
    assert sync < active, "the guides sync must run before the row is marked active"
    assert "assume-role" in post[:sync] and 'role/OperatorOrchestration' in post[:sync], "the sync runs in the customer account"


def test_the_buildspec_applies_init_customer_first_and_never_destroys_it():
    """A vended gerp needs the export bucket, its CMK, the reader role and export_gerp before
    per_customer plans (it finds them by name), and needs them to SURVIVE closure (the owner
    downloads for fifteen days after the instance is gone). So init_customer applies before
    per_customer on an apply, and no destroy touches it."""
    text = BUILDSPEC.read_text()
    init_block = text.index('cd prod/init_customer')
    per_customer = text.index('cd prod/per_customer')
    assert init_block < per_customer, "init_customer must apply before per_customer"
    assert 'key=${CUSTOMER_ID}/init.tfstate' in text, "init_customer has its own state key"
    # the init_customer block is guarded on apply
    guard = text.rfind('if [ "${TF_ACTION}" = "apply" ]', 0, init_block)
    assert guard != -1 and 'fi' in text[init_block:per_customer], "the init_customer apply is inside the apply guard"
    # and the block itself only ever applies: no destroy, no TF_ACTION, reaches it
    block = text[init_block:text.index(")", init_block)]
    assert "terraform apply" in block and "destroy" not in block and "TF_ACTION" not in block


def test_the_apply_is_one_pass_and_the_log_is_one_line_per_change():
    """The plan goes to a file whose summary is logged, not its attribute dump; and there is one
    pass — no `-target` first pass standing in for a dependency the graph should carry."""
    text = BUILDSPEC.read_text()
    apply_block = text[text.index("tf_plan_apply() {"):text.index("fi\n", text.index('tf_apply "per_customer"'))]
    assert "-target=" not in apply_block, "a targeted first pass means a module reads another's output outside the graph"
    assert "-out=/tmp/tf.plan" in apply_block and "grep -E '^  # |^Plan:|^No changes'" in apply_block
    assert "terraform apply -input=false -no-color /tmp/tf.plan" in apply_block


def _build_phase():
    text = BUILDSPEC.read_text()
    return text[text.index("  build:"):text.index("  post_build:")]


def test_the_four_actions_and_no_fifth():
    """apply, destroy (the closure), stop (the operator's teardown) and plan (apply.sh --plan).
    Anything else fails in pre_build, before the export or a plan."""
    text = BUILDSPEC.read_text()
    pre = text[text.index("pre_build:"):text.index("  build:")]
    assert 'case "${TF_ACTION}" in apply|destroy|stop|plan) ;; *)' in pre and "exit 1" in pre


def test_a_plan_changes_nothing():
    """A plan build exports nothing, applies and destroys nothing, and writes no row: its branches
    in the build phase run `terraform plan` alone, and post_build's writes are all guarded on another
    action."""
    import yaml
    phases = yaml.safe_load(BUILDSPEC.read_text())["phases"]
    for c in phases["build"]["commands"]:
        for branch in re.findall(r'if \[ "\$\{TF_ACTION\}" = "plan" \]; then(.*?)(?:\n\s*(?:elif|else|fi))', c, re.S):
            assert "terraform plan" in branch
            for word in ("terraform apply", "destroy", "update-item", "invoke"):
                assert word not in branch, f"a plan branch runs {word}"
    export = next(c for c in phases["build"]["commands"] if "EXPORT_FN=" in c)
    assert export.lstrip().startswith('if [ "${TF_ACTION}" = "destroy" ] || [ "${TF_ACTION}" = "stop" ]'), \
        "only the two destroys export; a plan must not"
    for c in phases["post_build"]["commands"]:
        if "update-item" in c:
            assert re.search(r'TF_ACTION\}" = "(apply|destroy)"', c), "every row write in post_build names its action"
    hub = yaml.safe_load((REPO / ".codebuild" / "hub.yml").read_text())["phases"]
    assert any('case "${TF_ACTION}" in apply|destroy|plan) ;; *)' in c for c in hub["pre_build"]["commands"])


def test_both_destroys_export_first_and_only_the_closure_stamps_the_row_closed():
    """The export loop runs for the two destroys, ahead of `terraform destroy`; after it the
    closure writes `closing` with the download window and stop writes `stopped` with the stashes
    removed. post_build stamps `closed` for the closure only; stop has nothing left to say."""
    build = _build_phase()
    closing, stopped = '\\"S\\":\\"closing\\"', '\\"S\\":\\"stopped\\"'   # as the yaml carries them
    export = build.index('if [ "${TF_ACTION}" = "destroy" ] || [ "${TF_ACTION}" = "stop" ]')
    assert export < build.index("terraform destroy"), "the export runs before the destroy"
    assert 'if [ "${TF_ACTION}" = "destroy" ]' in build[export:] and closing in build[export:]
    stop = build.index(stopped)
    assert "REMOVE gateway_url, chat_url, runtime_endpoint_arn" in build[stop - 400:stop + 200], \
        "stop drops the stashes the apply wrote; the next apply writes the new ones"
    assert build.count(closing) == 1, "one write of closing, the closure's"
    text = BUILDSPEC.read_text()
    post = text[text.index("post_build:"):]
    closed, stop_branch = post.index('\\"S\\":\\"closed\\"'), post.index('[ "${TF_ACTION}" = "stop" ]')
    assert post.rfind('if [ "${TF_ACTION}" = "destroy" ]', 0, closed) != -1, "closed is the closure's stamp"
    assert "update-item" not in post[stop_branch:post.index("fi", stop_branch)], "stop writes no row in post_build"


def test_post_build_never_exits_early_and_the_applys_ending_is_one_guarded_command():
    """In CodeBuild an `exit` ends the command it is in, not the phase: the closure's `exit 0`
    after the closed stamp let the apply's stashes run against a destroyed stack and the build
    read FAILED (westwood's closure, 2026-09-06). So nothing in post_build exits, and every
    apply-only step — outputs, the guides sync, the active stamp, the two stashes — sits in ONE
    command under an apply guard."""
    import yaml
    cmds = yaml.safe_load(BUILDSPEC.read_text())["phases"]["post_build"]["commands"]
    assert not any(re.search(r"^\s*exit 0\s*$", c, re.M) for c in cmds), "no early exit in post_build"
    [apply_cmd] = [c for c in cmds if "terraform output" in c]
    assert apply_cmd.lstrip().startswith('if [ "${TF_ACTION}" = "apply" ] && [ "${CODEBUILD_BUILD_SUCCEEDING}" = "1" ]'), \
        "the apply's ending is guarded on the action AND on the build phase having passed"
    # post_build runs after a failed build phase too: no row write without the phase having passed
    for c in cmds:
        if "update-item" in c:
            assert c.lstrip().startswith('if [ "${CODEBUILD_BUILD_SUCCEEDING}" != "1" ]') or "CODEBUILD_BUILD_SUCCEEDING" in c[:200], \
                "a row write in post_build must sit behind CODEBUILD_BUILD_SUCCEEDING"
    for step in ("sync_playbooks.sh", 'echo "marked ${CUSTOMER_ID} active', "SET chat_url", "SET runtime_endpoint_arn"):
        assert step in apply_cmd, f"{step} is part of the apply's one command"
    assert "set -e" in apply_cmd[:apply_cmd.index("terraform output")], "a failing step fails the build"



def test_every_directory_has_a_lock_file_with_a_mac_and_a_linux_hash():
    """The runner's plugin cache hands init an unpacked package, which init checks against the lock
    file's h1: for its platform. A lock file written on a mac alone carries the mac's, and 32 of 46
    directories then failed (2026-09-16); a directory with no lock file picks a version on the fly
    and failed the same way. `scripts/tf-version.sh` writes every directory's, both platforms."""
    import re as _re
    skip = (".terraform/", ".venv", "node_modules", "terraform.tfstate.d")
    dirs = sorted({tf.parent for tf in REPO.glob("**/*.tf") if not any(s in str(tf) for s in skip)})
    short = []
    for d in dirs:
        lock = d / ".terraform.lock.hcl"
        if not lock.is_file():
            short.append(f"{d.relative_to(REPO)}: no lock file")
            continue
        for block in _re.split(r'^provider "', lock.read_text(), flags=_re.M)[1:]:
            name = block.split('"', 1)[0]
            if block.count("h1:") < 2:
                short.append(f"{lock.relative_to(REPO)}: {name} has one platform's h1 only")
    assert not short, "\n  ".join(["every directory, both platforms:"] + sorted(short))


def test_no_module_reads_the_agent_through_ssm_at_plan():
    """A module learns the gateway from module.agent's outputs, threaded by per_customer — the
    graph terraform builds for free. An SSM data source on `/agent/*` reads a parameter the agent
    module has not written yet on a fresh account, and the plan fails (the first vend,
    2026-09-04). The parameters stay for the container at runtime; nothing plans on them."""
    offenders = []
    for tf in MODULES.glob("*/*/*.tf"):
        if "/agent/infra/" in str(tf):
            continue   # the agent writes them
        for m in re.finditer(r'data "aws_ssm_parameter" "\w+" \{[^}]*?/agent/', tf.read_text(), re.S):
            offenders.append(str(tf.relative_to(REPO)))
    assert not offenders, f"plan-time SSM reads of the agent's parameters: {sorted(set(offenders))}"


def test_no_count_or_for_each_gates_on_a_module_output_string():
    """A count or a for_each key must be known at plan. `var.x != ""` on a string that is another
    module's output — an api id, a lambda arn, an endpoint arn — is unknown on a fresh account and
    terraform refuses the plan (the first vend, 2026-09-04). Optional integrations gate on a bool
    the root sets: serve_web, poke_agent, agreements_tools."""
    outputs = ("server_api_id", "agent_runtime_endpoint_arn", "agreements_request_fn_arn", "agreements_accept_fn_arn")
    offenders = []
    for tf in MODULES.glob("*/infra/*.tf"):
        text = tf.read_text()
        for m in re.finditer(r'(count|for_each)\s*=\s*[^\n]*var\.(%s)\s*[!=]=\s*""' % "|".join(outputs), text):
            offenders.append(f"{tf.relative_to(REPO)}: {m.group(0).strip()}")
        for m in re.finditer(r'var\.(%s)\s*[!=]=\s*""\s*\?\s*\{' % "|".join(outputs), text):
            offenders.append(f"{tf.relative_to(REPO)}: a map keyed on {m.group(1)}")
    assert not offenders, "gates on a module output's emptiness:\n  " + "\n  ".join(offenders)


def test_every_function_goes_through_the_lambda_module():
    """`modules/terraform/lambda` owns what every function owns — its log group with a retention,
    its Errors alarm, the artifact pin, the fleet tag. A bare `aws_lambda_function` anywhere else
    is a function with a log group Lambda creates unowned (no retention, survives a destroy) and
    no alarm; the module's own resource is the one allowed."""
    bare = []
    for tf in list(MODULES.glob("*/infra/*.tf")) + list((REPO / "prod").glob("*/*.tf")) + list((REPO / "prod").glob("*/*/*.tf")):
        if "modules/terraform/" in str(tf):
            continue
        for m in re.finditer(r'^resource "aws_lambda_function" "(\w+)"', tf.read_text(), re.M):
            bare.append(f"{tf.relative_to(REPO)}: {m.group(1)}")
    assert not bare, "functions outside modules/terraform/lambda:\n  " + "\n  ".join(bare)


def test_per_customer_never_wraps_a_module_output_in_try():
    """A declared module output is always present, so `try(module.x.y, null)` guards nothing —
    and it costs the plan: try() over an object whose fields are unknown at plan (SES tokens, an
    arn) returns an unknown, and a count gated on that unknown's null-ness fails a fresh apply
    (westwood's re-apply, 2026-09-06: five route53 records, "Invalid count argument"). A module
    output that is null-or-object gates a count on its own."""
    offenders = []
    for tf in (REPO / "prod" / "per_customer").glob("*.tf"):
        for m in re.finditer(r"try\(\s*module\.", tf.read_text()):
            line = tf.read_text()[:m.start()].count("\n") + 1
            offenders.append(f"{tf.relative_to(REPO)}:{line}")
    assert not offenders, f"try() over a module output: {offenders}"


def test_every_module_that_takes_a_gateway_is_handed_one_by_per_customer():
    """A module that declares `gateway_id` registers tools on it; per_customer hands it
    module.agent.gateway_id, whatever the module's register_with_agent default (inbox and
    settings default to true and were the two left empty on the first vend — CreateGatewayTarget
    "gatewayIdentifier must not be empty")."""
    text = (REPO / "prod" / "per_customer" / "main.tf").read_text()
    blocks = {m.group(1): m.group(2) for m in re.finditer(r'^module "(\w+)" \{\n(.*?)^\}', text, re.M | re.S)}
    declares = {tf.parts[-3] for tf in MODULES.glob("*/infra/*.tf") if re.search(r'^variable "gateway_id"', tf.read_text(), re.M)}
    for name in sorted(declares):
        body = blocks.get(name)
        assert body is not None, f"module {name} declares gateway_id but per_customer has no module \"{name}\" block"
        if re.search(r"^\s*register_with_agent\s*=\s*false", body, re.M):
            continue
        assert "module.agent.gateway_id" in body and "module.agent.gateway_role_arn" in body, \
            f'module "{name}" takes a gateway but per_customer does not hand it module.agent.gateway_id / gateway_role_arn'


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all codebuild tests passed")
