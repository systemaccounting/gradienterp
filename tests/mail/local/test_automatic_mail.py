"""The agent's email door answers people, never machines: an out-of-office answering the agent's reply
would be answered, and answer back, forever. And the agent's own reply says it is automatic, so a
compliant responder stays quiet."""

import email
import sys
from email.policy import default as default_policy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mailbox_sorting import load  # noqa: E402

AGENT = "agent@testfirm.agents.gradienterp.cloud"
OWNER = "owner@shop.com"


def _raw(*extra):
    auth = (f"Authentication-Results: amazonses.com; spf=pass smtp.mailfrom={OWNER}; "
            f"dkim=pass header.i=@shop.com; dmarc=pass header.from=shop.com\r\n")
    head = "".join(h + "\r\n" for h in extra)
    return (f"From: {OWNER}\r\nTo: {AGENT}\r\nSubject: Re: your books\r\n{auth}{head}"
            f"Message-ID: <m1@shop.com>\r\n\r\nI'm out of the office until Monday.\r\n").encode()


def _deliver(raw):
    mod = load()
    invokes, sends = [], []
    with patch.object(mod, "s3") as s3, patch.object(mod, "ddb") as ddb, \
         patch.object(mod, "agentcore") as ac, patch.object(mod, "ses") as ses:
        s3.get_object.return_value = {"Body": type("B", (), {"read": lambda self: raw})()}
        ddb.get_item.return_value = {"Item": {"value": {"L": [{"S": "agent"}]}}}
        ac.invoke_agent_runtime.side_effect = lambda **kw: invokes.append(kw) or {
            "response": type("R", (), {"read": lambda self: b'{"response":"hi"}'})()}
        ses.send_raw_email.side_effect = lambda **kw: sends.append(kw)
        mod.handler({"Records": [{"s3": {"object": {"key": "inbound/m1"}}}]}, None)
    return invokes, sends


def test_an_automatic_message_wakes_nothing_and_gets_no_reply():
    for header in ("Auto-Submitted: auto-replied", "Auto-Submitted: auto-generated", "Precedence: auto_reply",
                   "Precedence: bulk", "X-Autoreply: yes", "X-Autorespond: 1", "List-Id: <owners.shop.com>",
                   "Return-Path: <>"):
        invokes, sends = _deliver(_raw(header))
        assert (invokes, sends) == ([], []), header


def test_a_persons_message_is_answered_and_the_reply_says_it_is_automatic():
    invokes, sends = _deliver(_raw("Auto-Submitted: no"))
    assert len(invokes) == 1 and len(sends) == 1
    reply = email.message_from_bytes(sends[0]["RawMessage"]["Data"], policy=default_policy)
    assert reply["Auto-Submitted"] == "auto-replied"


def test_the_agents_own_address_is_a_machine():
    mod = load()
    msg = email.message_from_bytes(_raw(), policy=default_policy)
    assert mod.automatic(msg, AGENT) == "the agent's own address"
    assert mod.automatic(msg, OWNER) == ""


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all automatic mail tests passed")
