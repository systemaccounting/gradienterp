"""The persona prompt carries the linked-report workflow (issue #40): the offer after a data
answer, the portal key, the standing preference, the periodic form and the decline memory."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PROMPT = (REPO / "modules" / "agent" / "prompts" / "bookkeeper.md").read_text()


def test_the_prompt_offers_a_report_once_and_names_where_it_goes():
    para = PROMPT.split("## product questions are answered from the product record", 1)[1].split("\n## ", 1)[0]
    assert "want this as a report you can open any time?" in para
    assert "`pages/reports/`" in para and "`manage_storage op: put`" in para
    assert "offer once" in para, "one offer, never a pitch on every answer"


def test_the_prompt_names_the_standing_preference_the_periodic_form_and_the_decline():
    para = PROMPT.split("## product questions are answered from the product record", 1)[1].split("\n## ", 1)[0]
    assert "`data-questions-as-reports`" in para and "`remember`" in para
    assert "the link and one line" in para
    assert "`latest.html`" in para and "schedule it" in para
    assert "`declined-reports`" in para and "never raised again" in para


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all prompt report tests passed")
