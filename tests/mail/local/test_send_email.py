"""Sending as the firm: which address it goes out as, and what a partial batch reports.

The load-bearing claims are that a gerp with no configured sender refuses instead of falling
back to an address the firm does not own, that `to` cannot smuggle a list past the caller, and
that a batch where some recipients fail returns SUCCESS with detail — because a caller that
reads partial failure as failure retries and sends the successes twice.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeSMTP, FakeTable, invoke, load_lambda  # noqa: E402

BILLING = {"sk": "SENDER#billing@shop.com", "host": "smtp.shop.com", "port": 587,
           "username": "billing@shop.com", "tls": "starttls", "secret_name": "smtp_billing",
           "default": True, "updated_at": 100}
HELLO = {"sk": "SENDER#hello@shop.com", "host": "smtp.shop.com", "port": 587,
         "username": "hello@shop.com", "tls": "starttls", "secret_name": "smtp_hello",
         "updated_at": 90}


def run(payload, rows=(BILLING,), server=None):
    """Invoke send_email against a fake settings table and a fake mail server."""
    mod = load_lambda("send_email")
    smtp = server or FakeSMTP()
    table = FakeTable(rows)

    with patch.object(mod._senders, "resource") as senders_res, \
         patch.object(mod, "client") as ssm, \
         patch.object(mod._smtp.smtplib, "SMTP", smtp.connect), \
         patch.object(mod._smtp.smtplib, "SMTP_SSL", smtp.connect):
        senders_res.return_value.Table.return_value = table
        ssm.return_value.get_parameter.return_value = {"Parameter": {"Value": "app-password"}}
        status, body = invoke(mod, payload)
    return status, body, smtp


def test_one_message_goes_out_as_the_default_sender():
    status, body, smtp = run({"to": "a@x.com", "subject": "hi", "body": "there"})
    assert status == 200, body
    assert body["from"] == "billing@shop.com"
    assert body["sent_count"] == 1 and body["failed_count"] == 0
    assert smtp.sent == [{"to": "a@x.com", "subject": "hi", "body": "there"}]


def test_a_named_sender_is_used_over_the_default():
    status, body, _ = run({"from": "hello@shop.com", "to": "a@x.com", "body": "hi"},
                          rows=(BILLING, HELLO))
    assert status == 200
    assert body["from"] == "hello@shop.com"


def test_an_address_the_firm_has_not_configured_is_refused():
    status, body, smtp = run({"from": "nope@elsewhere.com", "to": "a@x.com", "body": "hi"})
    assert status == 409
    assert "cannot send as nope@elsewhere.com" in body["error"]
    assert "billing@shop.com" in body["error"], "the refusal should name what IS configured"
    assert smtp.sent == []


def test_a_firm_with_no_mail_server_refuses_rather_than_falling_back():
    """The whole point of having no platform sender: nothing goes out claiming to be from an
    address the firm does not own."""
    status, body, smtp = run({"to": "a@x.com", "body": "hi"}, rows=())
    assert status == 409
    assert "no mail server is configured" in body["error"]
    assert "configure_smtp" in body["error"], "the refusal should say how to fix it"
    assert smtp.sent == []


def test_no_default_and_no_from_refuses_rather_than_picking_one():
    """A firm with several senders and none flagged must not have mail go out as whichever
    row happened to sort first."""
    a = {**HELLO, "sk": "SENDER#a@shop.com"}
    b = {**HELLO, "sk": "SENDER#b@shop.com"}
    status, body, smtp = run({"to": "x@y.com", "body": "hi"}, rows=(a, b))
    assert status == 409
    assert "no default sender" in body["error"]
    assert smtp.sent == []


def test_many_messages_share_one_connection():
    msgs = [{"to": f"c{i}@x.com", "subject": "invoice", "body": f"you owe {i}"} for i in range(5)]
    status, body, smtp = run({"messages": msgs})
    assert status == 200
    assert body["sent_count"] == 5
    assert smtp.connections == 1, "a batch must not reconnect per message"


def test_a_partial_batch_returns_success_with_detail():
    """Non-2xx here would be worse rather than stricter: a caller that retries on failure sends
    the successes a second time, and email has no idempotency key."""
    smtp = FakeSMTP(refuse={"dead@x.com": (550, "no such user")})
    msgs = [{"to": "ok@x.com", "body": "1"}, {"to": "dead@x.com", "body": "2"},
            {"to": "fine@x.com", "body": "3"}]
    status, body, smtp = run({"messages": msgs}, server=smtp)

    assert status == 200, "partial success is success — otherwise a retry double-sends"
    assert body["sent_count"] == 2 and body["failed_count"] == 1
    assert [f["to"] for f in body["failed"]] == ["dead@x.com"]
    assert body["failed"][0]["code"] == 550
    assert {s["to"] for s in body["sent"]} == {"ok@x.com", "fine@x.com"}


def test_a_dropped_connection_does_not_fail_the_rest_of_the_batch():
    """Some providers close after N messages. Everything after the drop must still go."""
    smtp = FakeSMTP(drop_after=2)
    msgs = [{"to": f"c{i}@x.com", "body": "hi"} for i in range(5)]
    status, body, smtp = run({"messages": msgs}, server=smtp)

    assert status == 200
    assert body["sent_count"] == 5, f"lost messages after the drop: {body['failed']}"
    assert smtp.connections == 2, "it should have reconnected exactly once"


def test_a_rejected_credential_fails_the_whole_call():
    """Nothing was sent, so this reads as a failed call — retrying it is safe."""
    smtp = FakeSMTP(fail_login=True)
    status, body, smtp = run({"to": "a@x.com", "body": "hi"}, server=smtp)
    assert status == 502
    assert "billing@shop.com" in body["error"], "the owner has to know WHICH sender is broken"
    assert smtp.sent == []


def test_a_list_in_to_is_refused_rather_than_leaking_the_recipients():
    """`to` taking a list is how a caller who thinks it means bulk shows every customer each
    other's address — a privacy incident that returns success."""
    status, body, smtp = run({"to": "a@x.com, b@x.com", "body": "hi"})
    assert status == 400
    assert "one address" in body["error"]
    assert "messages" in body["error"], "the error should point at the shape that does work"
    assert smtp.sent == []


def test_cc_is_how_a_group_deliberately_sees_each_other():
    status, body, smtp = run({"to": "a@x.com", "body": "hi", "cc": ["b@x.com", "c@x.com"]})
    assert status == 200
    assert smtp.sent[0]["to"] == "a@x.com"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all send_email tests passed")
