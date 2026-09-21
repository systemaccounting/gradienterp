"""The periodic report script the metrics kb shows (issue #40), run once against a fake ctx: the
query, then the page at the dated key and at latest.html under the report's prefix."""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


class FakeCtx:
    def __init__(self):
        self.calls, self.puts = [], {}

    def call(self, tool, args=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "manage_metrics":
            return {"name": args["name"], "engine": "athena",
                    "window": {"start": "2026-09-14T07:00:00.000Z", "end": "2026-09-21T07:00:00.000Z", "label": args["window"]},
                    "columns": ["period", "n"], "rows": [{"period": "2026-09-15", "n": "12"}, {"period": "2026-09-16", "n": "9"}],
                    "row_count": 2, "query_id": "q", "bytes_scanned": 208}
        if tool == "manage_storage":
            self.puts[args["key"]] = args["content"]
            return {"key": args["key"], "url": "https://portal/" + args["key"]}
        raise AssertionError(tool)


def _script():
    kb = (REPO / "modules" / "metrics" / "kb.md").read_text()
    section = kb.split("## a periodic report", 1)[1]
    return re.search(r"```python\n(.*?)```", section, flags=re.S).group(1)


def test_the_kbs_script_runs_the_query_and_writes_the_dated_key_and_latest():
    ns = {}
    exec(_script(), ns)
    ctx = FakeCtx()
    out = ns["run"](ctx, name="count", params={"event": "session.started", "grain": "day"}, window="last_week",
                    slug="weekly-sessions", title="sessions last week")
    assert [t for t, _ in ctx.calls] == ["manage_metrics", "manage_storage", "manage_storage"]
    assert ctx.calls[0][1] == {"op": "query", "name": "count", "params": {"event": "session.started", "grain": "day"}, "window": "last_week"}
    assert set(ctx.puts) == {"pages/reports/weekly-sessions/2026-09-21T07-00.html", "pages/reports/weekly-sessions/latest.html"}
    html = ctx.puts["pages/reports/weekly-sessions/latest.html"]
    assert "<h1>sessions last week</h1>" in html and "<td>2026-09-15</td><td>12</td>" in html
    assert "count" in html and "last_week" in html and "run 2026-09-21T07-00" in html, "the footer names the call"
    assert out["url"].endswith("/latest.html")


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all report script tests passed")
