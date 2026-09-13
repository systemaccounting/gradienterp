"""Reading back what was sent — the answer to "did the invoices go out?" after the fact."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import invoke, load_lambda  # noqa: E402


def line(**kw):
    return {"message": json.dumps({"event": "send_email", "at": int(time.time()), **kw})}


def run(payload, events, next_token=None, pages=None):
    """`pages` gives the exact sequence FilterLogEvents returns, for the empty-page case."""
    mod = load_lambda("get_send_history")
    seq = pages or [{"events": events, **({"nextToken": next_token} if next_token else {})}]
    with patch.object(mod, "client") as logs:
        logs.return_value.filter_log_events.side_effect = list(seq)
        status, body = invoke(mod, payload)
        call = logs.return_value.filter_log_events.call_args
    return status, body, call


def test_an_empty_first_page_does_not_read_as_nothing_sent():
    """FilterLogEvents scans STREAMS, so a page can carry zero events and a nextToken — the
    matches are further in. Stopping at page one reports "nothing sent" for a send that did."""
    hit = line(**{"from": "billing@shop.com", "sent_count": 1, "failed_count": 0})
    _, body, _ = run({}, [], pages=[
        {"events": [], "nextToken": "p2"},
        {"events": [hit], "nextToken": "p3"},
        {"events": []},
    ])
    assert body["count"] == 1, "the match was on page two"
    assert body["truncated"] is False, "the listing ran to the end, so it is complete"


def test_recent_sends_come_back_with_their_failures():
    events = [
        line(**{"from": "billing@shop.com", "sent_count": 158, "failed_count": 42,
                "failed": [{"to": "dead@x.com", "code": 550, "error": "no such user"}]}),
    ]
    status, body, _ = run({}, events)
    assert status == 200
    assert body["count"] == 1
    assert body["sends"][0]["sent_count"] == 158
    assert body["sends"][0]["failed"][0]["to"] == "dead@x.com"


def test_failures_only_filters_at_the_log_rather_than_in_python():
    """A firm sending daily has far more clean runs than failures; filtering after fetching
    would spend the 200-event cap on successes."""
    _, _, call = run({"failures_only": True}, [])
    assert "$.failed_count > 0" in call.kwargs["filterPattern"]


def test_the_window_is_hours_back_from_now():
    _, _, call = run({"hours": 72}, [])
    start = call.kwargs["startTime"] / 1000
    assert 71.9 * 3600 < time.time() - start < 72.1 * 3600


def test_filtering_by_sender_keeps_only_that_address():
    events = [line(**{"from": "billing@shop.com", "sent_count": 1, "failed_count": 0}),
              line(**{"from": "hello@shop.com", "sent_count": 1, "failed_count": 0})]
    _, body, _ = run({"from": "hello@shop.com"}, events)
    assert body["count"] == 1 and body["sends"][0]["from"] == "hello@shop.com"


def test_truncation_is_reported_rather_than_looking_complete():
    """Hitting the cap with pages still to come has to say so — a capped listing that reads as
    the whole answer is how "nothing else went out" becomes a wrong answer."""
    full = [line(**{"from": "a@b.com", "sent_count": 1, "failed_count": 0}) for _ in range(200)]
    _, body, _ = run({}, [], pages=[{"events": full, "nextToken": "more"}])
    assert body["count"] == 200
    assert body["truncated"] is True


def test_a_malformed_log_line_is_skipped_not_fatal():
    events = [{"message": "not json at all"},
              line(**{"from": "a@b.com", "sent_count": 1, "failed_count": 0})]
    status, body, _ = run({}, events)
    assert status == 200 and body["count"] == 1


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all send_history tests passed")
