"""docs/AGENTS.md is the file a user hands their own agent to learn gradientERP. The landing's docs glyph
and both sites' llms.txt point at it; every GitHub link on the two sites names a file that exists; and it
has an entry for every module a gerp is built with, so a new module comes with its entry."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs" / "AGENTS.md"
BLOB = "https://github.com/systemaccounting/gradienterp/blob/main/"
SITES = [REPO / "prod/gradienterp_cloud/web/index.html", REPO / "prod/gradienterp_cloud/web/app.js",
         REPO / "prod/gradienterp_cloud/web/llms.txt", REPO / "prod/openlyoperated_biz/web/llms.txt"]


def test_the_docs_glyph_and_both_llms_txt_point_at_docs_agents():
    for path in SITES:
        assert BLOB + "docs/AGENTS.md" in path.read_text(), path.name
    for path in SITES[:2]:
        assert 'title="docs for agents"' in path.read_text(), path.name


def test_every_github_file_link_on_the_sites_exists():
    for path in SITES:
        for rel in re.findall(re.escape(BLOB) + r"([^\s\"')<>]+)", path.read_text()):
            assert (REPO / rel).is_file(), f"{path.name} links {rel}, which isn't in the repo"


def test_every_module_a_gerp_is_built_with_has_an_entry():
    tf = (REPO / "prod/per_customer/main.tf").read_text()
    modules = sorted(set(re.findall(r'source\s*=\s*"\.\./\.\./modules/([a-z_]+)/', tf)))
    assert len(modules) > 20, modules
    doc = DOCS.read_text()
    missing = [m for m in modules if f"`modules/{m}`" not in doc]
    assert not missing, f"docs/AGENTS.md has no entry for {missing}"


def test_it_says_what_it_is_for_in_its_first_lines():
    head = "\n".join(DOCS.read_text().splitlines()[:6]).lower()
    assert "agent" in head and "root `agents.md`" in head


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all docs for agents tests passed")
