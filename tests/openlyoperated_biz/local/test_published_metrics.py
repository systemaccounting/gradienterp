"""The business page draws a card per definition the firm published (issue #48), off
GET /gerps/{id}/metrics, with the cards the economy uses and no per-metric code."""
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = (REPO / "prod" / "openlyoperated_biz" / "web" / "main.js").read_text()


def _card_code():
    """The page's card functions as they ship: from `const money` through `const metricsRow`."""
    lines = PAGE.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("const money = "))
    end = next(i for i, l in enumerate(lines) if l.startswith("const metricsRow = "))
    return "\n".join(lines[start:end + 1])


def test_a_card_per_published_definition_with_its_label_headline_and_call():
    payload = {"gerp_id": "cafe", "metrics": [
        {"key": "account.signed_up.count.day", "label": "signups <b>per</b> day", "unit": "count", "grain": "day",
         "headline": {"period": "2026-09-21", "value": 5}, "points": [{"period": "2026-09-20", "value": 3}, {"period": "2026-09-21", "value": 5}],
         "definition": "events per period for one event", "source": {"curl": "curl https://api.openlyoperated.biz/v1/gerps/cafe/metrics/account.signed_up.count.day"}},
        {"key": "session.started.active.day", "label": "daily active", "unit": "count", "grain": "day",
         "headline": {"period": "2026-09-21", "value": 2}, "points": [{"period": "2026-09-21", "value": 2}],
         "definition": "distinct subjects per period", "source": {"curl": "curl https://api.openlyoperated.biz/v1/gerps/cafe/metrics/session.started.active.day"}}]}
    script = _card_code() + f"\nconsole.log(metricsRow({json.dumps(payload)}.metrics));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert out.count("/gerps/cafe/metrics/") == 2, "one card per definition, each with the call that produced it"
    assert "signups &lt;b&gt;per&lt;/b&gt; day" in out and "<b>per</b>" not in out, "a firm's label is text"
    assert "daily active" in out and "2026-09-21" in out


def test_the_page_reads_the_platforms_route_and_draws_it_with_the_shared_cards():
    assert "getJson('/gerps/' + encodeURIComponent(gid) + '/metrics')" in PAGE
    assert "metricsRow(published)" in PAGE
    assert "sec('what this business records'" in PAGE


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all published metrics tests passed")
