"""Configuring a sender: nothing is written until a message actually sent.

The load-bearing claim is that a wrong host, port or credential fails while the owner is with
you rather than at 3am when a dunning notice does not go — which only holds if a failed test
writes NOTHING. The other is that the first sender becomes the default, because a firm that
configures their address and still sees mail from somewhere else does not file a bug, they just
quietly look unprofessional.
"""

import sys
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import FakeSMTP, FakeTable, invoke, load_lambda  # noqa: E402

SET = {"address": "billing@shop.com", "host": "smtp.shop.com", "secret_name": "smtp_billing"}


def run(payload, rows=(), server=None, secret="app-password"):
    mod = load_lambda("configure_smtp")
    smtp = server or FakeSMTP()
    table = FakeTable(rows)

    with patch.object(mod, "resource") as res, patch.object(mod, "client") as ssm, \
         patch("smtplib.SMTP", smtp.connect), patch("smtplib.SMTP_SSL", smtp.connect):
        res.return_value.Table.return_value = table
        if secret is None:
            ssm.return_value.get_parameter.side_effect = ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "not found"}}, "GetParameter")
        else:
            ssm.return_value.get_parameter.return_value = {"Parameter": {"Value": secret}}
        status, body = invoke(mod, payload)
    return status, body, table, smtp


def test_configuring_sends_a_test_message_and_writes_the_row():
    status, body, table, smtp = run(SET)
    assert status == 200, body
    assert body["status"] == "configured"
    assert len(smtp.sent) == 1, "it must prove the server works before saving"
    assert smtp.sent[0]["to"] == "billing@shop.com"
    assert table.rows[0]["sk"] == "SENDER#billing@shop.com"
    assert table.rows[0]["host"] == "smtp.shop.com"


def test_a_failed_test_send_writes_nothing():
    """The whole point of testing at configure time: a firm never ends up with a stored sender
    that cannot send."""
    smtp = FakeSMTP(fail_login=True)
    status, body, table, smtp = run(SET, server=smtp)
    assert status == 502
    assert "nothing was saved" in body["error"]
    assert table.rows == [], "a broken sender must not be stored"


def test_the_error_names_what_the_owner_has_to_check():
    smtp = FakeSMTP(fail_login=True)
    status, body, _, _ = run({**SET, "username": "someone@shop.com"}, server=smtp)
    assert "someone@shop.com" in body["error"] and "billing@shop.com" in body["error"], \
        "a rejected login is the owner's to fix, so it has to say which login and which address"


def test_a_missing_secret_is_caught_before_any_connection():
    status, body, table, smtp = run(SET, secret=None)
    assert status == 404
    assert "smtp_billing" in body["error"]
    assert smtp.sent == [] and table.rows == []


def test_the_password_is_never_accepted_as_a_value():
    """Only a NAME. A tool that took the password would put it in model context."""
    import json

    schema = json.loads((Path(__file__).resolve().parents[3]
                         / "modules/agent/lambdas/configure_smtp/schema.json").read_text())
    props = set(schema["properties"])
    assert "secret_name" in props
    assert not {"password", "secret", "pass"} & props, f"credential-valued field in {props}"


def test_the_first_sender_becomes_the_default():
    _, body, table, _ = run(SET)
    assert body["default"] is True
    assert table.rows[0]["default"] is True


def test_a_later_sender_does_not_steal_the_default():
    existing = {"sk": "SENDER#hello@shop.com", "host": "h", "secret_name": "s",
                "default": True, "updated_at": 1}
    _, body, table, _ = run(SET, rows=(existing,))
    assert body["default"] is False
    flagged = [r["sk"] for r in table.rows if r.get("default")]
    assert flagged == ["SENDER#hello@shop.com"], "moving the default is a deliberate act"


def test_make_default_moves_the_flag_off_the_others():
    a = {"sk": "SENDER#a@shop.com", "host": "h", "secret_name": "s", "default": True, "updated_at": 1}
    b = {"sk": "SENDER#b@shop.com", "host": "h", "secret_name": "s", "updated_at": 2}
    status, _, table, smtp = run({"op": "make_default", "address": "b@shop.com"}, rows=(a, b))
    assert status == 200
    flagged = [r["sk"] for r in table.rows if r.get("default")]
    assert flagged == ["SENDER#b@shop.com"], f"exactly one default, got {flagged}"
    assert smtp.sent == [], "moving a flag should not send mail"


def test_make_default_on_an_unconfigured_address_is_refused():
    a = {"sk": "SENDER#a@shop.com", "host": "h", "secret_name": "s", "updated_at": 1}
    status, body, _, _ = run({"op": "make_default", "address": "ghost@shop.com"}, rows=(a,))
    assert status == 404
    assert "not a configured sender" in body["error"]


def test_removing_a_sender_drops_the_row():
    a = {"sk": "SENDER#a@shop.com", "host": "h", "secret_name": "s", "updated_at": 1}
    status, body, table, _ = run({"op": "remove", "address": "a@shop.com"}, rows=(a,))
    assert status == 200 and body["status"] == "removed"
    assert table.rows == []


def test_ssl_defaults_to_465_and_starttls_to_587():
    _, _, table, _ = run({**SET, "tls": "ssl"})
    assert table.rows[0]["port"] == 465
    _, _, table, _ = run(SET)
    assert table.rows[0]["port"] == 587


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all configure_smtp tests passed")
