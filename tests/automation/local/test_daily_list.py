"""gradienterp's `daily_list.py`, approved: the lines read by name, the page at the dated key and
latest.html, and the same lines as mail when a `to` is given."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "prod" / "gradienterp" / "automations" / "daily_list.py"
PREFIX = "pages/reports/daily-list/"
WINDOW = {"start": "2026-10-15T07:00:00.000Z", "end": "2026-10-16T07:00:00.000Z", "label": "today"}


class FakeCtx:
    """`ctx.call` the way automate's does; every tool the script reaches, answered."""

    def __init__(self):
        self.calls, self.puts, self.mails = [], {}, []

    def call(self, tool, args=None):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "manage_metrics":
            return {"name": args["name"], "window": dict(WINDOW, label=args["window"]),
                    "rows": [{"period": "2026-10-01", "n": 2, "subjects": 4, "at_start": 3.0, "at_end": 4.0, "ratio": 0.25}]}
        if tool == "manage_invoice":
            return {"invoices": [{"invoice_id": "I1", "status": "unpaid", "lines": [{"amount": "12.50"}, {"amount": "7.5"}]}]}
        if tool == "manage_storage":
            self.puts[args["key"]] = args["content"]
            return {"key": args["key"], "url": "https://portal/" + args["key"]}
        if tool == "send_email":
            self.mails.append(args)
            return {"sent_count": 1, "failed_count": 0}
        raise AssertionError((tool, args))


def _load():
    spec = importlib.util.spec_from_file_location("daily_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LABELS = ("new accounts", "new gerps", "gerps torn down", "checkouts started", "owners active", "accounts",
          "gerps active", "saas.conversion", "saas.churn", "saas.engagement", "saas.unit_cost", "unpaid invoices")


def test_no_params_writes_the_page_and_sends_nothing():
    ctx = FakeCtx()
    out = _load().run(ctx)
    assert sorted(ctx.puts) == [f"{PREFIX}2026-10-16T07-00.html", f"{PREFIX}latest.html"], "the dated key and latest, under the one prefix"
    page = ctx.puts[f"{PREFIX}latest.html"]
    assert page == ctx.puts[f"{PREFIX}2026-10-16T07-00.html"], "the same page at both keys"
    for label in LABELS:
        assert f"<td>{label}</td>" in page, label
    assert "<td>1 open, 20.00</td>" in page, "the invoice read, its lines summed"
    assert "<td>4.00</td>" in page, "the stock's at_end"
    assert ctx.mails == [], "no address, no mail"
    assert out == {"key": f"{PREFIX}latest.html", "url": f"https://portal/{PREFIX}latest.html"}, "the put's answer is the script's"


def test_to_sends_the_same_lines_and_the_page_url():
    ctx = FakeCtx()
    out = _load().run(ctx, to="owner@example.com")
    assert len(ctx.mails) == 1, "one send"
    mail = ctx.mails[0]
    assert mail["to"] == "owner@example.com"
    assert mail["subject"] == "the operator's list, week of 2026-10-12", "the monday of the window's day (a thursday)"
    for label in LABELS:
        assert f"\n{label}  " in "\n" + mail["body"], label
    assert "accounts  4.00  (cumulative · account.signed_up · this_month)" in mail["body"], "value and source on the line"
    assert mail["body"].rstrip().endswith(f"run 2026-10-16T07-00\nhttps://portal/{PREFIX}latest.html"), "the run and the page's url close the mail"
    assert [t for t, _ in ctx.calls][-3:] == ["manage_storage", "manage_storage", "send_email"], "the page is written before the mail goes"
    assert out["mailed"] == "owner@example.com" and out["url"].endswith("latest.html")


if __name__ == "__main__":
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
