"""authed_by / created_by — the fact and the attribution.

`authed_by` is who was signed in; `created_by` is whose sale it is. Normally identical, and they
diverge exactly when someone acts for someone else — a manager ringing a ticket for a server. Git
makes the same split (author vs committer) for the same reason: the attribution is a claim, and it
is only trustworthy because the fact sits beside it.

So the tests that matter are the ones about where each value is allowed to come from.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "invoicing" / "lambdas"))
from _helpers import AUTHOR_AGENT, authed_by, build_invoice  # noqa: E402

LINE = {"description": "latte", "account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 4.5}


def _inv(**kw):
    inv, err = build_invoice(customer="walk-in", lines=[dict(LINE)], **kw)
    assert err is None, err
    return inv


def test_authed_by_comes_from_the_verified_claims():
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "tok-sub"}}}}}
    assert authed_by(ev) == "tok-sub"


def test_authed_by_is_never_read_from_the_body():
    """The whole point. A body-supplied subject is a claim; this field must only ever be a fact."""
    assert authed_by({"body": '{"authed_by": "forged"}'}) == ""
    assert authed_by({"authed_by": "forged"}) == ""
    assert authed_by({}) == ""


def test_the_agent_path_propagates_a_verified_subject():
    """No JWT claims reach a gateway-invoked lambda, so the runtime passes the subject it verified.
    That is propagation of a checked fact, not a caller's assertion — the gateway is IAM-only."""
    assert authed_by({"_authed_by": "runtime-verified-sub"}) == "runtime-verified-sub"


def test_created_by_defaults_to_authed_by():
    """The common case — one person rings their own ticket — costs nothing to record."""
    inv = _inv(authed_by="ava-sub")
    assert inv["authed_by"] == "ava-sub"
    assert inv["created_by"] == "ava-sub"


def test_a_manager_ringing_for_a_server_keeps_both():
    inv = _inv(authed_by="mgr-sub", created_by="ava-sub")
    assert inv["created_by"] == "ava-sub", "the sale is Ava's"
    assert inv["authed_by"] == "mgr-sub", "and the record shows who attributed it"


def test_both_grains_carry_it():
    """A ticket is not authored once: a barista opens it, a manager comps a line at the end."""
    inv = _inv(authed_by="mgr-sub", created_by="ava-sub")
    assert inv["lines"][0]["authed_by"] == "mgr-sub"
    assert inv["lines"][0]["created_by"] == "ava-sub"


def test_a_line_may_name_its_own_author():
    inv, err = build_invoice(
        customer="walk-in", authed_by="mgr-sub", created_by="ava-sub",
        lines=[dict(LINE), {**LINE, "created_by": "ben-sub"}])
    assert err is None
    assert [ln["created_by"] for ln in inv["lines"]] == ["ava-sub", "ben-sub"]


def test_automation_is_a_value_not_an_absence():
    """`agent` says the gerp's own automation wrote it. Blank says nobody knows — a worse claim."""
    inv = _inv(authed_by=AUTHOR_AGENT)
    assert inv["authed_by"] == AUTHOR_AGENT and inv["created_by"] == AUTHOR_AGENT


def test_an_unattributed_invoice_carries_neither():
    """A poker or scheduled invoke has no caller. Absent means unknown, and stays absent."""
    inv = _inv()
    assert "authed_by" not in inv and "created_by" not in inv


def test_a_rewrite_does_not_re_author():
    """`put_invoice` rewrites the whole row, so a path that rebuilds an invoice from a request would
    silently re-author it to whoever touched it last — turning an audit field into a most-recent-
    editor field, which is the one thing it must never be."""
    from _helpers import carry_authorship
    prior = {"authed_by": "ava-sub", "created_by": "ava-sub"}
    rebuilt = {"authed_by": "mgr-sub", "created_by": "mgr-sub"}
    assert carry_authorship(rebuilt, prior) == prior


def test_a_rewrite_of_an_unattributed_row_stays_unattributed():
    """A row written before authorship existed must not acquire an author by being edited."""
    from _helpers import carry_authorship
    assert carry_authorship({"authed_by": "mgr", "created_by": "mgr"}, {"customer": "c"}) == {}


def test_an_inbound_cross_firm_sale_is_authored_by_the_counterparty():
    """Nobody signed in here — the buyer's agent authored it under THEIR account, on their side of
    the rail, so their subject is meaningless in our books. The counterparty gerp is the honest
    author; blank would claim nobody knows."""
    inv = _inv(authed_by=AUTHOR_AGENT, created_by="blue-ridge-roasters")
    assert inv["authed_by"] == AUTHOR_AGENT
    assert inv["created_by"] == "blue-ridge-roasters"


def test_an_improvised_draft_builds_and_is_flagged():
    """A draft is inert — no journal entry, and it cannot be paid (`record_invoice_paid` requires
    `issued`). So a line arriving without a price or an account is not an error at build time; it is
    a draft with a hole, flagged for the agent. The completeness gate lives at issue, where money
    moves, and it has to exist there regardless."""
    inv, err = build_invoice(customer="walk-in", lines=[{"description": "whatever the chef made"}])
    assert err is None, err
    assert inv["incomplete"] is True
    assert inv["status"] == "draft"


def test_a_complete_draft_carries_no_flag():
    inv, err = build_invoice(customer="walk-in", lines=[dict(LINE)])
    assert err is None and "incomplete" not in inv


def test_a_line_still_cannot_be_negative():
    """Dropping the completeness check is not dropping validation."""
    _, err = build_invoice(customer="walk-in", lines=[{**LINE, "amount": -1}])
    assert err and "negative" in err


def test_absent_is_a_hole_but_wrong_is_an_error():
    """The distinction the draft-may-be-incomplete rule turns on. A missing account is a POS that
    doesn't know where revenue lands — resolvable. A stated `ASSET` is a caller mistake, and
    accepting it would let a draft claim something that can never post."""
    inv, err = build_invoice(customer="w", lines=[{"description": "custom", "unit_price": 8.5}])
    assert err is None and inv["incomplete"] is True, "absent → a hole"
    _, err2 = build_invoice(customer="w", lines=[{**LINE, "accountType": "ASSET"}])
    assert err2 and "accountType" in err2, "wrong → an error"


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all authorship tests passed")
