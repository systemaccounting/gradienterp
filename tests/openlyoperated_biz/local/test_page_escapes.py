"""openlyoperated.biz builds its business page from strings, and two things reach those strings from
outside: the url (whatever follows `#b/`) and the api (labels, places, definitions, names a business
types). Each goes through `esc` before it meets markup, so a crafted link or a published label renders
as text. The escaper itself runs here under Node against the markup characters."""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = (REPO / "prod" / "openlyoperated_biz" / "web" / "index.html").read_text()


def test_the_escaper_turns_markup_into_text():
    line = next(l for l in PAGE.splitlines() if l.startswith("const esc = "))
    script = line + "\nconsole.log(JSON.stringify([esc('<img src=x onerror=alert(1)>'), esc(`\"'&`), esc(null), esc(42)]));"
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == ["&lt;img src=x onerror=alert(1)&gt;", "&quot;&#39;&amp;", "", "42"]


def test_the_url_and_the_api_reach_markup_only_through_esc():
    for escaped in ("loading ' + esc(gid)", "esc(gid) + ' does not publish", "esc(g.label)",
                    "esc(m.definition)", "esc(m.label)", "esc(q)", "esc(g.gerp_id)", "esc(it.name)",
                    "esc(legs)", "esc(acct(x.account))", "esc(e.date)", "+ esc(c) +", "esc(note)", "esc(t)"):
        assert escaped in PAGE, escaped
    # no raw interpolation of the url's gerp id or a published field into markup
    for raw in (r"'loading ' \+ gid", r"'>' \+ gid \+ ' does not publish", r"\+ g\.label \+", r"\+ m\.definition \+",
                r"\+ it\.name \+", r"\+ legs \+ '</span>'", r"searching · \"' \+ q \+"):
        assert not re.search(raw, PAGE), raw


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all page escape tests passed")
