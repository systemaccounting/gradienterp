"""Taking money from a card the payer saved — the module's first outbound call that moves it.

Two claims carry the weight. It is IDEMPOTENT on the invoice, because a retry or a re-fired sequence
step must not charge a payer twice and Stripe's key is the only place that can be guaranteed. And it
books nothing: the charge fires the same webhook a human clicking a link would, so money moving and
money being recorded stay separate.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "modules/payments/lambdas/charge_saved_method"))
sys.path.insert(0, str(REPO / "modules/aws"))
from _helpers import load_lambda as _load  # noqa: E402

INV = {"invoice_id": "INV-9", "total": "840.00", "status": "issued", "customer": "CT-3"}
SAVED = {"contact_id": "CT-3", "stripe_customer_id": "cus_1", "stripe_payment_method_id": "pm_1"}
PI = {"id": "pi_1", "status": "succeeded", "latest_charge": "ch_1"}


def load_lambda(**env):
    import os
    os.environ.update({"CUSTOMER_ID": "testfirm", "GET_INVOICES_FN": "manage_invoice",
                       "CONTACTS_GET_FN": "manage_contacts", **env})
    return _load("charge_saved_method")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _calc(invoice, tax_cents=0):
    cents = int(round(float(invoice["total"]) * 100))
    return {"id": "taxcalc_1", "object": "tax.calculation", "amount_total": cents + tax_cents,
            "tax_amount_exclusive": tax_cents}


def run(payload, invoice=INV, contact=SAVED, secret="sk_live", raiser=None, tax_cents=0, calc_error=None,
        calc_status=400, card_country="US", setup_intents=(), pi=None, reads=None, watched=None):
    """`tax_cents` is what Stripe Tax answers for the invoice; `calc_error` makes the calculation
    call fail with that error code (`calc_status`, 400 by default) instead. `card_country` is the
    saved card's issuer and `setup_intents` what Stripe lists for it (the India mandate read);
    `reads` collects those GETs."""
    mod = load_lambda()
    posts = []

    def urlopen(req, timeout=None):
        if req.get_method() == "GET":
            if reads is not None:
                reads.append(req.full_url)
            if "/v1/payment_methods/" in req.full_url:
                return _Resp(json.dumps({"id": "pm_1", "card": {"country": card_country}}).encode())
            if "/v1/setup_intents?" in req.full_url:
                return _Resp(json.dumps({"data": list(setup_intents)}).encode())
        is_calc = req.full_url.endswith("/v1/tax/calculations")
        # the calculation is recorded only for the tests about it: every older test reads
        # posts[0] as the intent
        if not is_calc or tax_cents or calc_error:
            posts.append({"url": req.full_url, "body": (req.data or b"").decode(),
                          "headers": {k.lower(): v for k, v in req.header_items()}})
        if is_calc:
            if calc_error:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, calc_status, "Bad Request", {},
                                             io.BytesIO(json.dumps({"error": {"code": calc_error}}).encode()))
            return _Resp(json.dumps(_calc(invoice, tax_cents)).encode())
        if raiser:
            raise raiser(req)
        return _Resp(json.dumps(pi or PI).encode())

    calls = []

    def fake_invoke(FunctionName, Payload):  # noqa: N803
        calls.append((FunctionName, json.loads(Payload)))
        body = ({"invoices": [invoice] if invoice else []} if "manage_invoice" in FunctionName
                else {"contact": contact} if contact else {})
        return {"Payload": io.BytesIO(json.dumps(
            {"statusCode": 200, "body": json.dumps(body)}).encode())}

    if watched is not None:
        mod.h.watch_collection = watched.append
    with patch.object(mod._providers, "resolve", lambda p="": p or "stripe"), \
         patch.object(mod, "_read_secret", lambda n: secret), \
         patch.object(mod, "MARK_UNPAID_FN", "gerp-invoicing-test-mark_unpaid"), \
         patch.object(mod, "_aws") as aws, patch("urllib.request.urlopen", urlopen):
        aws.return_value.invoke.side_effect = fake_invoke
        resp = mod.handler(payload, None)
    return resp["statusCode"], json.loads(resp["body"]), posts, calls


def test_a_second_card_is_a_different_idempotency_key():
    """The one that makes a fallback possible at all. With the method out of the key, Stripe replays
    the FIRST card's decline for every card after it — the loop looks like it ran and charges
    nothing."""
    _, _, first, _calls = run({"invoice_id": "INV-9"})
    _, _, second, _calls = run({"invoice_id": "INV-9", "payment_method_id": "pm_2"})
    k1 = first[0]["headers"]["idempotency-key"]
    k2 = second[0]["headers"]["idempotency-key"]
    assert k1 != k2, "the same key would replay the first card's answer"


def test_the_same_card_twice_keeps_the_same_key():
    """And the property the key already had: a re-run of the same sequence step must not charge
    twice."""
    _, _, a, _calls = run({"invoice_id": "INV-9"})
    _, _, b, _calls = run({"invoice_id": "INV-9"})
    assert a[0]["headers"]["idempotency-key"] == b[0]["headers"]["idempotency-key"]


def test_an_aimed_method_is_charged_against_the_contacts_own_customer():
    """A caller says WHICH method; it never says whose customer. A method is chargeable only
    against the customer it hangs off, so accepting both halves would let a caller pair one payer's
    method with another payer's customer."""
    status, body, posts, _calls = run({"invoice_id": "INV-9", "payment_method_id": "pm_2"})
    assert status == 200, body
    sent = posts[0]["body"]
    assert "payment_method=pm_2" in sent, sent
    assert "customer=cus_1" in sent, "the customer still comes off the contact"


def test_it_charges_the_saved_card_off_session():
    status, body, posts, _calls = run({"invoice_id": "INV-9"})
    assert status == 200, body
    assert body["payment_intent"] == "pi_1" and body["charge"] == "ch_1"
    sent = posts[0]["body"]
    assert "off_session=true" in sent and "confirm=true" in sent, \
        "one call, because there is no browser to hand back to"
    assert "customer=cus_1" in sent and "payment_method=pm_1" in sent
    assert "amount=84000" in sent


def test_it_is_idempotent_on_the_invoice_and_amount():
    """The header is what stops a double charge — a re-fired sequence step replays the first
    response rather than taking the money again."""
    _, _, a, _calls = run({"invoice_id": "INV-9"})
    _, _, b, _calls = run({"invoice_id": "INV-9"})
    assert a[0]["headers"]["idempotency-key"] == b[0]["headers"]["idempotency-key"]

    # a different amount IS a different charge and must be allowed to be one
    _, _, c, _calls = run({"invoice_id": "INV-9"}, invoice={**INV, "total": "420.00"})
    assert c[0]["headers"]["idempotency-key"] != a[0]["headers"]["idempotency-key"]


def test_the_invoice_id_rides_the_intent():
    """The webhook books it. Nothing else remembers this call happened."""
    _, _, posts, _calls = run({"invoice_id": "INV-9"})
    assert "metadata%5Binvoice_id%5D=INV-9" in posts[0]["body"]


def test_it_books_nothing_itself():
    src = (REPO / "modules/payments/lambdas/charge_saved_method/main.py").read_text()
    assert "post_journal_entry" not in src, \
        "the webhook books it; a charge that also booked could double-count a real payment"


def test_a_payer_with_no_saved_card_is_told_to_send_a_link():
    status, body, posts, _calls = run({"invoice_id": "INV-9"}, contact={"contact_id": "CT-3"})
    assert status == 409
    assert "no saved card" in body["error"] and "payment link" in body["error"]
    assert posts == [], "nothing should reach the processor"


def test_a_card_that_needs_the_payer_is_not_retryable():
    """Stripe declines an off-session charge that wants a challenge. Retrying fails identically —
    the payer has to come back — so the answer names that rather than looking transient."""
    import urllib.error

    def raiser(req):
        return urllib.error.HTTPError(
            req.full_url, 402, "Payment Required", {},
            io.BytesIO(json.dumps({"error": {"code": "authentication_required",
                                             "message": "The payment requires authentication"}}).encode()))

    status, body, _, _calls = run({"invoice_id": "INV-9"}, raiser=raiser)
    assert status == 409, "not a 502 — nothing is broken, the payer is just absent"
    assert body["retryable"] is False
    assert "payment link" in body["error"]


def test_a_cancelled_or_missing_mandate_needs_the_payer_back_too():
    """An India-issued card's e-mandate cancelled, paused, or never registered declines the
    off-session charge with one of three codes; each is the same answer as a challenge nobody
    can answer — the card has to be saved again, by a link."""
    import urllib.error
    for code in ("transaction_not_approved", "india_recurring_payment_mandate_canceled", "payment_intent_mandate_invalid"):
        def raiser(req, code=code):
            return urllib.error.HTTPError(
                req.full_url, 402, "Payment Required", {},
                io.BytesIO(json.dumps({"error": {"code": code, "message": "declined"}}).encode()))
        status, body, _, _calls = run({"invoice_id": "INV-9"}, raiser=raiser)
        assert status == 409 and body["retryable"] is False and "payment link" in body["error"], code


def test_a_paid_invoice_is_not_charged_again():
    status, body, posts, _calls = run({"invoice_id": "INV-9"}, invoice={**INV, "status": "paid"})
    assert status == 409 and posts == []


def test_a_decline_says_what_the_processor_said():
    import urllib.error

    def raiser(req):
        return urllib.error.HTTPError(
            req.full_url, 402, "Payment Required", {},
            io.BytesIO(json.dumps({"error": {"message": "Your card was declined."}}).encode()))

    status, body, _, _calls = run({"invoice_id": "INV-9"}, raiser=raiser)
    assert status == 409 and body["retryable"] is False, "a decline is the processor's no, not a fault"
    assert "declined" in body["error"]
    assert "sk_live" not in json.dumps(body)


# ─── the announced path: a firm's invoice-transition rule asked for this charge ───


def announced(detail):
    """What EventBridge delivers, minus the fields nothing here reads."""
    return {"version": "0", "detail-type": "collection.requested", "source": "invoicing",
            "detail": detail}


def test_an_announced_charge_takes_the_same_card():
    """The rule carries an invoice id and nothing else. Same charge, same key as a direct call."""
    status, body, posts, _calls = run(announced({"invoice_id": "INV-9"}))
    assert status == 200, body
    assert "customer=cus_1" in posts[0]["body"] and "amount=84000" in posts[0]["body"]

    direct = run({"invoice_id": "INV-9"})[2]
    assert posts[0]["headers"]["idempotency-key"] == direct[0]["headers"]["idempotency-key"], \
        "a redelivery must replay the first response rather than take the money twice"


def test_the_amount_is_read_fresh_not_carried_by_the_event():
    """A partial payment can land between the transition and the delivery. What gets charged is
    what the invoice says NOW, which is why the event carries an id and no amount."""
    _, _, posts, _calls = run(announced({"invoice_id": "INV-9"}), invoice={**INV, "total": "300.00"})
    assert "amount=30000" in posts[0]["body"]


def test_a_processor_failure_raises_so_lambda_retries_and_dead_letters():
    """Nothing reads the return value on this path — EventBridge invoked it asynchronously. A
    returned 502 looks like success and the invoice sits issued, uncollected, with no trace."""
    import urllib.error

    def raiser(req):
        return urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                      io.BytesIO(b'{"error": {"message": "try later"}}'))

    try:
        run(announced({"invoice_id": "INV-9"}), raiser=raiser)
    except Exception as e:
        assert type(e).__name__ == "Undelivered", e
        assert "sk_live" not in str(e)
    else:
        assert False, "a retryable processor failure must raise on the announced path"

    status, body, _, _calls = run({"invoice_id": "INV-9"}, raiser=raiser)
    assert status == 502 and "try later" in body["error"], "a direct caller still gets a value"


def test_a_decline_does_not_raise_even_when_announced():
    """Retrying a declined card declines it again. Done; a person is the next step."""
    import urllib.error

    def raiser(req):
        return urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, io.BytesIO(
            json.dumps({"error": {"code": "authentication_required",
                                  "message": "needs confirmation"}}).encode()))

    status, body, _, _calls = run(announced({"invoice_id": "INV-9"}), raiser=raiser)
    assert status == 409 and body["retryable"] is False


def test_a_payer_with_no_saved_card_does_not_raise():
    """Most payers never arm one. Not a delivery failure, and must not fill the queue."""
    status, _, posts, _calls = run(announced({"invoice_id": "INV-9"}), contact={"contact_id": "CT-3"})
    assert status == 409 and posts == []


def test_an_unreadable_invoice_raises_rather_than_reporting_it_missing():
    """`_invoke` returns None for a failed read and a missing row alike. The rule only ran because
    the invoice reached a status, so here None means the read failed — worth another attempt."""
    try:
        run(announced({"invoice_id": "INV-9"}), invoice=None)
    except Exception as e:
        assert type(e).__name__ == "Undelivered", e
    else:
        assert False, "a failed invoice read must reach the undelivered queue"

    status, _, _, _calls = run({"invoice_id": "INV-9"}, invoice=None)
    assert status == 404, "a direct caller asking about an unknown invoice still gets an answer"


def _lines(fn):
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]


def test_a_failed_charge_files_an_incident():
    """A charge that arrived and FAILED leaves nothing behind unless it says so in the shape
    `create_inc_from_log` reads — the undelivered queue only catches what never ran."""
    import urllib.error

    def raiser(req):
        return urllib.error.HTTPError(req.full_url, 402, "Payment Required", {},
                                      io.BytesIO(b'{"error": {"message": "card declined"}}'))

    hit = next(l for l in _lines(lambda: run({"invoice_id": "INV-9"}, raiser=raiser))
               if l.get("incident"))
    assert hit["incident"] == "fail"
    assert hit["subject"] == "collection:INV-9"
    assert hit["category"] == "collection"
    assert "declined" in hit["error"]


def test_a_successful_charge_closes_it():
    """Without this an incident opens on a transient decline and never clears."""
    hit = next(l for l in _lines(lambda: run({"invoice_id": "INV-9"})) if l.get("incident"))
    assert hit["incident"] == "ok" and hit["subject"] == "collection:INV-9"


def test_no_saved_card_files_nothing():
    """Most payers never arm one. Filing an incident for every invoice a firm bills on terms
    would bury the real ones."""
    assert [l for l in _lines(lambda: run({"invoice_id": "INV-9"}, contact={"contact_id": "CT-3"}))
            if l.get("incident")] == []



def _declined(message="Your card was declined."):
    import urllib.error

    def raiser(req):
        return urllib.error.HTTPError(
            req.full_url, 402, "Payment Required", {},
            io.BytesIO(json.dumps({"error": {"message": message}}).encode()))
    return raiser


def test_a_failed_charge_marks_the_invoice_unpaid():
    """`issued` says the money is owed; `unpaid` says someone tried to take it and could not, which
    is the fact a chase attaches to."""
    status, _, _, calls = run({"invoice_id": "INV-9"}, raiser=_declined())
    assert status == 409
    marked = [c for c in calls if "mark_unpaid" in c[0]]
    assert len(marked) == 1
    assert marked[0][1]["invoice_id"] == "INV-9"
    assert "declined" in marked[0][1]["reason"]


def test_a_successful_charge_marks_nothing():
    _, _, _, calls = run({"invoice_id": "INV-9"})
    assert not [c for c in calls if "mark_unpaid" in c[0]]


def test_the_card_needing_the_payer_back_still_marks_it():
    """A 409 the sequence must not retry is still a charge that did not land, so the invoice says so
    and the chase can send the link the payer has to use."""
    status, _, _, calls = run({"invoice_id": "INV-9"},
                              raiser=_declined("authentication_required"))
    assert status == 409
    assert [c for c in calls if "mark_unpaid" in c[0]]


def test_marking_it_unpaid_cannot_swallow_the_charge_failure():
    """The incident is the record that matters. A status update that throws must not turn a decline
    into something the caller reads as a different error."""
    mod = load_lambda()
    import urllib.error

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 402, "Payment Required", {},
            io.BytesIO(json.dumps({"error": {"message": "Your card was declined."}}).encode()))

    def fake_invoke(FunctionName, Payload):  # noqa: N803
        if "mark_unpaid" in FunctionName:
            raise RuntimeError("invoicing is down")
        body = ({"invoices": [INV]} if "manage_invoice" in FunctionName else {"contact": SAVED})
        return {"Payload": io.BytesIO(json.dumps(
            {"statusCode": 200, "body": json.dumps(body)}).encode())}

    with patch.object(mod._providers, "resolve", lambda p="": p or "stripe"), \
         patch.object(mod, "_read_secret", lambda n: "sk_live"), \
         patch.object(mod, "MARK_UNPAID_FN", "gerp-invoicing-test-mark_unpaid"), \
         patch.object(mod, "_aws") as aws, patch("urllib.request.urlopen", urlopen):
        aws.return_value.invoke.side_effect = fake_invoke
        resp = mod.handler({"invoice_id": "INV-9"}, None)

    assert resp["statusCode"] == 409
    assert "declined" in json.loads(resp["body"])["error"]



def test_cross_module_reads_name_their_op():
    """The invoice and the contact live behind op-routed tools now; a read without `op` is a 400
    at the callee, so the op is part of this caller's contract."""
    _, _, _, calls = run({"invoice_id": "INV-9"})
    assert {p.get("op") for fn, p in calls if "manage_invoice" in fn} == {"get"}
    assert {p.get("op") for fn, p in calls if "manage_contacts" in fn} == {"get"}

def _intent(posts):
    from urllib.parse import parse_qs
    return {k: v[0] for k, v in parse_qs(next(p["body"] for p in posts if p["url"].endswith("/v1/payment_intents"))).items()}


def test_tax_owed_charges_the_total_with_the_calculation_linked():
    """Stripe Tax answers before the intent; when it owes anything the intent is for the total with
    tax and carries the calculation, so Stripe records the tax transaction itself. The cents ride
    metadata for the webhook's ledger leg."""
    status, body, posts, _ = run({"invoice_id": "INV-9"}, tax_cents=725)
    assert status == 200, body
    pi = _intent(posts)
    assert int(pi["amount"]) == int(round(float(INV["total"]) * 100)) + 725
    assert pi["hooks[inputs][tax][calculation]"] == "taxcalc_1"
    assert pi["metadata[tax_amount]"] == "725"
    assert body["tax"] == 7.25
    calc = next(p for p in posts if p["url"].endswith("/v1/tax/calculations"))
    assert "customer=" in calc["body"] and "line_items%5B0%5D%5Bamount%5D=" in calc["body"]
    assert "-tax725" in next(p["headers"]["idempotency-key"] for p in posts if p["url"].endswith("/v1/payment_intents"))


def test_zero_tax_charges_the_invoice_total_with_no_hook():
    """No registration where the customer is → zero tax → the intent is what it always was."""
    status, body, posts, _ = run({"invoice_id": "INV-9"})
    assert status == 200, body
    pi = _intent(posts)
    assert int(pi["amount"]) == int(round(float(INV["total"]) * 100))
    assert not any(k.startswith("hooks") for k in pi) and "metadata[tax_amount]" not in pi
    assert body["tax"] == 0


def test_tax_not_set_up_on_the_account_charges_untaxed():
    """An account without Stripe Tax answers the calculation with an account-level error; the
    charge goes out untaxed rather than not at all."""
    status, body, posts, _ = run({"invoice_id": "INV-9"}, calc_error="tax_settings_not_configured")
    assert status == 200, body
    assert int(_intent(posts)["amount"]) == int(round(float(INV["total"]) * 100))


def test_a_tax_service_outage_never_charges_untaxed():
    """Stripe Tax answering 5xx is OUR failure, not an account without tax: the charge fails
    closed (no intent posted, a 502 the caller can retry) instead of going out untaxed."""
    status, body, posts, _ = run({"invoice_id": "INV-9"}, calc_error="api_error", calc_status=503)
    assert status == 502, body
    assert not any(p["url"].endswith("/v1/payment_intents") for p in posts)


def test_an_unresolvable_address_fails_the_charge_closed():
    """`customer_tax_location_invalid` is about the payer's address, not the account: fail like a
    decline, so the notice says what to fix, and post no intent."""
    status, body, posts, _ = run({"invoice_id": "INV-9"}, calc_error="customer_tax_location_invalid")
    assert status != 200
    assert not any(p["url"].endswith("/v1/payment_intents") for p in posts)


def test_an_india_card_is_charged_under_the_mandate_its_setup_registered():
    """RBI declines an off-session charge on an India-issued card without an e-mandate. The card is
    read first; an `IN` card is charged naming the mandate its succeeded SetupIntent registered."""
    reads = []
    code, body, posts, _ = run({"invoice_id": "INV-9"}, card_country="IN", reads=reads,
                               setup_intents=[{"id": "seti_1", "status": "succeeded", "mandate": "mandate_1"}])
    assert code == 200, body
    intent = [p for p in posts if p["url"].endswith("/v1/payment_intents")][0]
    assert "mandate=mandate_1" in intent["body"] and "off_session=true" in intent["body"]
    assert any("/v1/setup_intents?" in r and "payment_method=" in r for r in reads)


def test_an_india_card_with_no_mandate_is_the_payer_back_case_and_no_intent_is_made():
    """Saved through Checkout by a business elsewhere: no mandate, so no charge can clear. Refused
    before any PaymentIntent, not retryable, the invoice marked unpaid so the link goes out."""
    code, body, posts, calls = run({"invoice_id": "INV-9"}, card_country="IN", setup_intents=[])
    assert code == 409 and body["retryable"] is False and "payment link" in body["error"]
    assert not [p for p in posts if p["url"].endswith("/v1/payment_intents")]
    assert any("mark_unpaid" in fn and "india_card_without_mandate" in p["reason"] for fn, p in calls)


def test_a_card_from_elsewhere_reads_no_mandate():
    reads = []
    code, body, posts, _ = run({"invoice_id": "INV-9"}, card_country="GB", reads=reads)
    assert code == 200 and "mandate=" not in [p for p in posts if p["url"].endswith("/v1/payment_intents")][0]["body"]
    assert not [r for r in reads if "/v1/setup_intents" in r]


def test_an_india_charge_held_for_the_pre_debit_notice_hands_the_watch_its_hold():
    """The charge comes back `processing` for 26 hours; the collection watch is told when it
    completes, so a charge still in its notice window is not reported as a missing webhook."""
    queued = []
    pi = {**PI, "status": "processing", "processing": {"type": "card", "card": {
        "customer_notification": {"approval_requested": False, "completes_at": 1789999999}}}}
    code, body, _, _ = run({"invoice_id": "INV-9"}, card_country="IN", pi=pi, watched=queued,
                           setup_intents=[{"status": "succeeded", "mandate": "mandate_1"}])
    assert code == 200 and body["completes_at"] == 1789999999
    assert queued and queued[-1]["hold_until"] == 1789999999


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all charge_saved_method tests passed")
