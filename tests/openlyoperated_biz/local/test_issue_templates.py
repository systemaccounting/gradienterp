"""The repo's issue forms and the one page that opens one. Every form is public, so each points a
private report at the support form; blank issues are off; and the dashboard's *propose a change*
opens the metric form with fields the form actually has — GitHub fills a form field from the query
parameter named by its id, and silently drops a parameter no field carries."""

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[3]
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"
DASHBOARD = REPO / "prod" / "openlyoperated_biz" / "web" / "index.html"
SUPPORT = "https://gradienterp.cloud/support"
REPO_URL = "https://github.com/systemaccounting/gradienterp"


def _ids(form):
    return re.findall(r"^\s+id:\s*(\S+)\s*$", form, re.M)


def _labels(form):
    m = re.search(r"^labels:\s*\[(.*)\]\s*$", form, re.M)
    return [x.strip().strip('"') for x in m.group(1).split(",")] if m else []


def test_blank_issues_are_off_and_questions_and_private_reports_go_elsewhere():
    config = (FORMS / "config.yml").read_text()
    assert re.search(r"^blank_issues_enabled:\s*false\s*$", config, re.M)
    assert "url: https://discord.gg/87FHhUhQmK" in config
    assert f"url: {SUPPORT}" in config


def test_each_form_carries_its_label_and_unique_field_ids():
    for name, label in (("bug", "bug"), ("feature", "feature"), ("metric", "metric")):
        form = (FORMS / f"{name}.yml").read_text()
        assert re.search(r"^name:\s*\S", form, re.M) and re.search(r"^description:\s*\S", form, re.M), name
        assert _labels(form) == [label], name
        ids = _ids(form)
        assert ids and len(ids) == len(set(ids)), f"{name} field ids are unique"


def test_the_public_forms_send_private_details_to_the_support_form():
    for name in ("bug", "feature"):
        assert SUPPORT in (FORMS / f"{name}.yml").read_text(), name


def test_the_dashboard_opens_the_metric_form_with_fields_it_has():
    html = DASHBOARD.read_text()
    assert f"const ISSUES = '{REPO_URL}/issues/new';" in html
    card = html[html.index("const metricCard"):]
    card = card[:card.index("return ")]
    # the query string the card builds, with each encodeURIComponent(...) standing in as a value
    built = "".join(re.findall(r"'([^']*)'", card[card.index("const propose"):]))
    params = parse_qs(urlparse("https://x/" + built).query, keep_blank_values=True)
    assert params.pop("template") == ["metric.yml"]
    params.pop("title")
    fields = set(_ids((FORMS / "metric.yml").read_text()))
    assert set(params) == {"key", "definition"}
    assert set(params) <= fields, f"every filled parameter is a field of metric.yml: {set(params) - fields}"
    assert {"key", "definition", "proposed"} <= fields


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all issue template tests passed")
