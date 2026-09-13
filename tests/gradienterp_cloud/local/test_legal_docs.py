"""The terms, the privacy notice and the DPA: each exists under docs/, each is linked from the
landing footer (both the static shell and the rendered page) and from the create screen, and the
two new ones carry what GDPR requires of them and the one stance they share — erasure removes who
a person is, and the accounting entries stay, since balances are computed from that history."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
WEB = REPO / "prod" / "gradienterp_cloud" / "web"
BASE = "https://github.com/systemaccounting/gradienterp/blob/main/docs/"


def test_the_three_documents_exist_and_link_each_other():
    for name in ("TERMS.md", "PRIVACY.md", "DPA.md"):
        assert (DOCS / name).is_file(), name
    terms = (DOCS / "TERMS.md").read_text()
    assert "(PRIVACY.md)" in terms and "(DPA.md)" in terms
    assert "(DPA.md)" in (DOCS / "PRIVACY.md").read_text()
    assert "(PRIVACY.md)" in (DOCS / "DPA.md").read_text() and "(TERMS.md)" in (DOCS / "DPA.md").read_text()


def test_the_landing_footer_and_the_create_screen_link_all_three():
    shell = (WEB / "index.html").read_text()
    footer = shell[shell.index('<footer class="login-foot">'):shell.index("</footer>", shell.index('<footer class="login-foot">'))]
    for name in ("TERMS.md", "PRIVACY.md", "DPA.md"):
        assert BASE + name in footer, f"the static shell's footer links {name}"
    app = (WEB / "app.js").read_text()
    for const, name in (("TERMS_URL", "TERMS.md"), ("PRIVACY_URL", "PRIVACY.md"), ("DPA_URL", "DPA.md")):
        assert f'const {const} = "{BASE}{name}"' in app
    login = app[app.index("const loginTpl"):app.index("const signupTpl")]
    assert "${TERMS_URL}" in login and "${PRIVACY_URL}" in login and "${DPA_URL}" in login
    create = app[app.index("const creategerpTpl"):]
    create = create[:create.index("helpLink}`;")]
    assert "${TERMS_URL}" in create and "${PRIVACY_URL}" in create and "${DPA_URL}" in create


def test_the_privacy_notice_carries_what_article_13_asks():
    text = re.sub(r"\s+", " ", (DOCS / "PRIVACY.md").read_text().lower())
    for part in ("controller", "legal basis", "legitimate interest", "legal obligation", "united states",
                 "standard contractual clauses", "how long", "supervisory authority", "automated", "stripe",
                 "amazon web services", "article 27", "required to hold an account", "a copy is available",
                 "do not sell or share"):
        assert part in text, part


def test_the_dpa_carries_what_article_28_asks():
    text = re.sub(r"\s+", " ", (DOCS / "DPA.md").read_text().lower())
    for part in ("subject matter", "duration", "nature and purpose", "data subjects", "documented instructions",
                 "confidentiality", "article 32", "subprocessor", "30 days", "articles 32 to 36",
                 "breach", "deleted", "audits", "standard contractual clauses", "2021/914", "module two",
                 "clause 17", "addendum", "fadp", "service provider", "does not sell or share"):
        assert part in text, part


def test_both_say_erasure_keeps_the_entries():
    for name in ("PRIVACY.md", "DPA.md"):
        text = re.sub(r"\s+", " ", (DOCS / name).read_text().lower())
        assert "computed from that history" in text, f"{name} says why the entries stay"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all legal docs tests passed")
