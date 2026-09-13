"""The platform admits a list of AWS Organizations, not one: every trust condition on
`aws:PrincipalOrgID` / `aws:ResourceOrgID` in prod/ and modules/ reads `local.org_ids`, which each
stack builds as the organization it runs in plus `ORG_IDS` from config.json. A second organization
is an id added to that list; no policy is edited."""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SKIP_DIRS = {".terraform", "tmp", "out", ".venv"}
ORG_IDS_LOCAL = re.compile(r"org_ids\s*=\s*concat\(\[data\.aws_organizations_organization\.this\.id\],\s*(?:try\()?local\.config\.ORG_IDS")


def tf_files():
    for root in ("prod", "modules"):
        for path in sorted((REPO / root).rglob("*.tf")):
            if SKIP_DIRS.isdisjoint(path.relative_to(REPO).parts):
                yield path


def code_lines(path):
    """(line number, text) for every line that is not a comment."""
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


CONDITION = re.compile(r'"aws:(?:Principal|Resource)OrgID"\s*=\s*(.+?)[\s}]*$')
PERMISSION = re.compile(r"^\s*principal_org_id\s*=\s*(\S+)\s*$")


DOCUMENT_CONDITION = re.compile(r'variable\s*=\s*"aws:(?:Principal|Resource)OrgID"')
DOCUMENT_VALUES = re.compile(r"values\s*=\s*(\[?[^\]\n]+\]?)")


def condition_sites():
    """Every (path, line number, value) where a policy condition, a policy document's condition
    block or an aws_lambda_permission reads an org id."""
    for path in tf_files():
        lines = list(code_lines(path))
        for i, (n, line) in enumerate(lines):
            m = CONDITION.search(line) or PERMISSION.match(line)
            if m:
                yield path, n, m.group(1)
                continue
            if DOCUMENT_CONDITION.search(line):
                # an aws_iam_policy_document condition: the value is the `values` line beside it
                for n2, line2 in lines[i + 1:i + 3]:
                    v = DOCUMENT_VALUES.search(line2)
                    if v:
                        yield path, n2, v.group(1).strip("[]").strip()
                        break


def test_config_carries_the_admitted_organizations_list():
    config = json.loads((REPO / "config.json").read_text())
    assert isinstance(config.get("ORG_IDS"), list), "config.json ORG_IDS is the list of admitted organizations"
    for org_id in config["ORG_IDS"]:
        assert re.fullmatch(r"o-[a-z0-9]{10,32}", org_id), f"{org_id!r} is not an organization id"


def test_every_condition_reads_local_org_ids():
    sites = list(condition_sites())
    assert len(sites) >= 15, f"the walk found {len(sites)} sites; the trust conditions live in prod/tower, prod/platform/operator and prod/optimizer/infra"
    for path, n, value in sites:
        where = f"{path.relative_to(REPO)}:{n}"
        if value == "each.value":
            # aws_lambda_permission holds one org id per statement; the resource iterates the list
            block = path.read_text().splitlines()[:n]
            start = max(i for i, line in enumerate(block) if line.startswith("resource "))
            assert "for_each = toset(local.org_ids)" in "\n".join(block[start:]), f"{where} iterates something other than local.org_ids"
        elif value == "var.org_ids":
            # a module takes the list from its caller — one under modules/, or a stack's own local
            # module (prod/tower/region) — and the caller passes local.org_ids
            local_module = path.parent.parent.parent.name == "prod"
            assert path.parent.parent.parent.name == "modules" or local_module, f"{where}: var.org_ids is the module form"
            if local_module:
                callers = [c for c in path.parent.parent.glob("*.tf")
                           if re.search(rf'source\s*=\s*"\./{path.parent.name}"', c.read_text())]
            else:
                callers = [c for c in (REPO / "prod").rglob("*.tf") if ".terraform" not in c.parts
                           and re.search(rf'source\s*=\s*"[./]*modules/{path.parent.parent.name}/infra"', c.read_text())]
            passing = re.compile(r"\borg_ids\s*=\s*local\.org_ids\b")
            assert callers and all(passing.search(c.read_text()) for c in callers), f"{where}: a caller ({[str(c.relative_to(REPO)) for c in callers if not passing.search(c.read_text())]}) passes something other than local.org_ids"
        else:
            assert value == "local.org_ids", f"{where} reads {value}"


def test_the_self_match_is_gone():
    """`aws:ResourceOrgID = ${aws:PrincipalOrgID}` admits only the caller's own organization."""
    for path in tf_files():
        for n, line in code_lines(path):
            assert "${aws:PrincipalOrgID}" not in line, f"{path.relative_to(REPO)}:{n} matches the caller's own organization only"


def test_each_stack_with_a_condition_defines_org_ids_from_config():
    stacks = {path.parent for path, _, value in condition_sites() if value != "var.org_ids"}
    assert stacks, "no stack carries a trust condition"
    for stack in sorted(stacks):
        text = "\n".join(p.read_text() for p in stack.glob("*.tf"))
        assert ORG_IDS_LOCAL.search(text), f"{stack.relative_to(REPO)} lacks org_ids = concat([data.aws_organizations_organization.this.id], local.config.ORG_IDS)"
        assert 'data "aws_organizations_organization" "this" {}' in text, f"{stack.relative_to(REPO)} lacks the organization data source"
        assert re.search(r'config\s*=\s*jsondecode\(file\("\$\{path\.module\}/(?:\.\./)+config\.json"\)\)', text), f"{stack.relative_to(REPO)} does not read config.json"
    assert not re.search(r"\bo-[a-z0-9]{10,32}\b", "\n".join(p.read_text() for s in stacks for p in s.glob("*.tf"))), "an organization id is written into a stack"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all org_ids tests passed")
