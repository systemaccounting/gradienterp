"""How a tool name becomes a gateway address, and the convention that makes it a function.

`automate` reaches tools through the gerp's own gateway, which addresses a tool as
`<TargetName>___<ToolName>`. The target hyphenates because AgentCore rejects underscores in a target
name; the tool inside keeps snake_case because that is what should read like a function to an agent.

Every module composes its target the same way, so the address is derivable and there is no map to
build, thread or keep fresh. That convention is held by hand in the modules that hardcode their
target name — which is what the second test is for. Break it and one tool becomes silently
uncallable from a script, at 3am, in an unattended run.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules/automation/lambdas/automate"))
import _gateway  # noqa: E402


def test_the_address_is_composed_from_the_tool_name():
    assert _gateway.address("manage_tasks") == "manage-tasks___manage_tasks"
    assert _gateway.address("send_email") == "send-email___send_email"
    assert _gateway.address("payment_links") == "payment-links___payment_links"


def test_an_already_qualified_address_is_passed_through():
    """A managed connector's target and tool names come from AWS and do not line up, so its address
    cannot be derived. Composing on top of one that is already whole is what produced
    `web-search---WebSearch___web-search___WebSearch`, a 500 that names a tool nobody registered."""
    assert _gateway.address("web-search___WebSearch") == "web-search___WebSearch"


def test_every_registered_target_still_follows_the_convention():
    """A text comparison over the terraform, the same shape as test_codebuild_source: the invariant
    is two names agreeing, and nothing made them agree except habit."""
    offenders = []
    for tf in REPO.glob("modules/*/infra/*.tf"):
        src = tf.read_text()
        for block in re.findall(
            r'resource "aws_bedrockagentcore_gateway_target".*?\n\}\n', src, re.S
        ):
            target = re.search(r'^\s*name\s*=\s*(.+)$', block, re.M)
            if not target:
                continue
            t = target.group(1).split("#")[0].strip()
            if "replace(" in t:
                continue                      # computed from the tool name — correct by construction
            tool = re.search(r'inline_payload\s*\{\s*\n\s*name\s*=\s*"([^"]+)"', block)
            if not tool:
                continue
            literal = t.strip('"')
            if literal != tool.group(1).replace("_", "-"):
                offenders.append(f"{tf.relative_to(REPO)}: target {literal!r} vs tool {tool.group(1)!r}")

    assert not offenders, (
        "a hardcoded target name no longer matches its tool, so automate cannot address it:\n  "
        + "\n  ".join(offenders)
    )


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all gateway name tests passed")
