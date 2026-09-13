"""Local-mode tests for the treasury distribution handler.

An instrument is a rule INSTANCE attached to `DISTRIBUTION#<id>` — its rate, its cap (or no cap),
its holder. That row is the whole configuration; there is no rule set to consult and no params
table. **The attachment is the dispatch**: an instrument pays because someone attached an instance
to it, and a perpetuity differs from a capped dividend only by a `cap` key on that row.

Seeds instruments (and, for the cap cases, prior DIVIDENDS_PAYABLE credits in the ledger), fires the
handler with a period + net income, and asserts the posted entry, the cap clipping, and the emitted
distribution.paid event (validated against modules/events/treasury/distribution.paid.v1.json).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import (scratch_env, load_lambda, read_jsonl, validate_event,  # noqa: F401
                      issue_instrument, seed_prior_distribution, REPO_ROOT)

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import drain, ledger_rows  # noqa: E402

SCHEMA = REPO_ROOT / "modules" / "events" / "treasury" / "distribution.paid.v1.json"

PERPETUITY = "net_income_percent_perpetuity"
DIVIDEND = "net_income_percent_dividend"


def _invoke(lam, event):
    resp = lam.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def _posted():
    """The rows that actually LANDED on the ledger.

    These used to read treasury's own jsonl — the payload it HANDED to accounting. The invoke
    dispatches in-process to the real post_journal_entry now, so a two-leg payload arrives as one
    balanced pair row and the assertions are on what a reader would see."""
    # only what the HANDLER posted — `seed_prior_distribution` writes real ledger rows too, and
    # they credit the same account. Its entry_ids start `prior-`, the handler's `dist-`.
    return [r for r in ledger_rows()
            if r.get("credit_account") == "DIVIDENDS_PAYABLE"
            and str(r.get("entry_id", "")).startswith("dist-")]


def _events():
    """This handler's announcements only. accounting's `journal_entry.posted` lands on the SAME
    bus — one gerp, one bus — so an unfiltered drain hands the distribution schema an entry event."""
    return [m["detail"] for m in drain(os.environ["_QUEUE_URL"], expected=99, tries=2)
            if m["detail_type"] == "distribution.paid"]


def test_perpetuity_pays_percentage_of_net_income():
    with scratch_env() as out:
        issue_instrument("deal-A", PERPETUITY, holder="holderA", factor="0.05")   # no cap
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000",
                                   "instrument": "deal-A"})
        assert code == 200, body

        journal = _posted()
        assert len(journal) == 1, journal
        row = journal[0]
        assert row["debit_account"] == "RETAINED_EARNINGS" and row["debit_account_type"] == "EQUITY"
        assert row["credit_account"] == "DIVIDENDS_PAYABLE" and row["credit_account_type"] == "LIABILITY"
        assert float(row["amount"]) == 5000  # 5% of 100000
        assert row["dims"]["holder"] == "holderA"

        events = _events()
        assert len(events) == 1, events
        ev = events[0]
        validate_event(ev, SCHEMA)
        assert ev["instrument_id"] == "deal-A"
        assert ev["holder"] == "holderA"
        assert ev["rule"] == PERPETUITY            # the product, named by the instance
        assert ev["amount"] == 5000
        assert ev["cumulative_paid"] == 5000
        assert ev["entry_id"] == "dist-deal-A-2026-03-31"
        assert "cap_remaining" not in ev           # a perpetuity attaches no cap
        assert ev["openly_operated"] is False


def test_a_failed_publish_raises_so_the_async_retry_reruns():
    """The holder's inbox books INVESTMENT income off `distribution.paid`. A publish that fails
    after the post is a lost receivable on their books, so the handler raises: Lambda's async
    retry re-runs the close, and the post is idempotent on its entry id."""
    with scratch_env() as out:
        issue_instrument("deal-R", PERPETUITY, holder="holderR", factor="0.05")
        lam = load_lambda("distribution")

        def boom(**kw):
            raise RuntimeError("bus down")
        events = lam._aws("events")          # the shared client is cached: patch it, then put it back
        real = events.put_events
        events.put_events = boom
        try:
            _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000", "instrument": "deal-R"})
        except RuntimeError as e:
            assert "bus down" in str(e)
        else:
            raise AssertionError("a lost distribution.paid was swallowed")
        finally:
            events.put_events = real
        assert len(_posted()) == 1, "the post stood; only the announcement failed"


def test_dividend_caps_at_remaining():
    with scratch_env() as out:
        issue_instrument("deal-B", DIVIDEND, holder="holderB", factor="0.10", cap="50000")
        # 48,000 already paid in a prior period → only 2,000 of the would-be 10,000 fits under the cap
        seed_prior_distribution("deal-B", DIVIDEND, "2026-02", "2026-02-28", 48000)
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000",
                                   "instrument": "deal-B"})
        assert code == 200, body

        assert float(_posted()[0]["amount"]) == 2000   # clipped from 10000 to cap remaining

        ev = _events()[0]
        validate_event(ev, SCHEMA)
        assert ev["amount"] == 2000
        assert ev["cumulative_paid"] == 50000     # 48000 + 2000
        assert ev["cap_remaining"] == 0           # cap fulfilled this cycle


def test_no_distribution_after_cap_reached():
    with scratch_env() as out:
        issue_instrument("deal-C", DIVIDEND, holder="holderC", factor="0.10", cap="50000")
        seed_prior_distribution("deal-C", DIVIDEND, "2026-02", "2026-02-28", 50000)  # cap met
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000",
                                   "instrument": "deal-C"})
        assert code == 200, body
        assert _posted() == []   # nothing posted, not a zero leg
        assert _events() == []   # nothing announced


def test_no_distribution_on_loss():
    with scratch_env() as out:
        issue_instrument("deal-D", PERPETUITY, holder="holderD", factor="0.05")
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "-20000",
                                   "instrument": "deal-D"})
        assert code == 200, body
        assert _posted() == []   # no payout on a loss
        assert _events() == []


def test_a_capped_instrument_also_pays_nothing_on_a_loss():
    # the cap clamps to lo=0, so a loss under a cap is still no legs — not a zero leg, not a negative
    with scratch_env() as out:
        issue_instrument("deal-E", DIVIDEND, holder="holderE", factor="0.10", cap="50000")
        lam = load_lambda("distribution")
        code, _ = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "-20000",
                                "instrument": "deal-E"})
        assert code == 200
        assert _posted() == []
        assert _events() == []


def test_nothing_attached_means_no_instrument():
    # not a cap of zero, not an end_time flag — simply no row. THIS is how "no instruments" is said.
    with scratch_env() as out:
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000"})
        assert code == 200 and body["instruments"] == 0
        assert _posted() == []


def test_enumerates_every_attached_instrument():
    with scratch_env(openly_operated=True) as out:
        issue_instrument("deal-F", PERPETUITY, holder="holderF", factor="0.05")
        issue_instrument("deal-G", PERPETUITY, holder="holderG", factor="0.05")
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000"})  # no id → all
        assert code == 200, body
        assert body["instruments"] == 2

        events = _events()
        assert len(events) == 2
        for ev in events:
            validate_event(ev, SCHEMA)
            assert ev["amount"] == 5000               # each pays its own 5% — independent instruments
            assert ev["openly_operated"] is True      # flag flowed from env into the detail
        assert {ev["holder"] for ev in events} == {"holderF", "holderG"}
        assert {ev["instrument_id"] for ev in events} == {"deal-F", "deal-G"}


def test_two_instruments_can_hold_the_same_product():
    # the old (holder, rule) key could not express this; the instrument id is its own subject now
    with scratch_env() as out:
        issue_instrument("deal-H1", DIVIDEND, holder="holderH", factor="0.10", cap="50000")
        issue_instrument("deal-H2", DIVIDEND, holder="holderH", factor="0.02", cap="10000")
        lam = load_lambda("distribution")
        code, body = _invoke(lam, {"periodEnd": "2026-03-31", "netIncome": "100000"})
        assert code == 200 and body["instruments"] == 2
        paid = {p["instrument_id"]: p["amount"] for p in body["paid"]}
        assert paid == {"deal-H1": 10000, "deal-H2": 2000}   # 10% and 2% of 100000, capped by neither


def test_deterministic_entry_id_for_idempotency():
    """A re-fire posts nothing new. The entryId AND the timestamp both derive from periodEnd, so
    the second run lands on the same (pk, sk) and post_journal_entry no-ops — which this now
    asserts directly, where it used to only check the precondition against a local stub that
    appended twice."""
    with scratch_env() as out:
        issue_instrument("deal-I", PERPETUITY, holder="holderI", factor="0.05")
        lam = load_lambda("distribution")
        ev = {"periodEnd": "2026-03-31", "netIncome": "100000", "instrument": "deal-I"}
        _invoke(lam, ev)
        _invoke(lam, ev)
        assert len(_posted()) == 1


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all distribution tests passed")
