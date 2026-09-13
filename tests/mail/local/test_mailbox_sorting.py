"""Where inbound mail lands, and which mailbox wakes the agent.

The load-bearing claims: only `agent@` starts a turn (waking on every forwarded reply and
bounce would be constant and expensive), a local part the firm never declared is parked rather
than dropped, and filing happens OUT of the watched prefix — filing into it would re-trigger
the handler on its own output, forever.
"""

import email
import sys
from email.policy import default as default_policy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[3]
LAMBDA = REPO / "modules" / "agent" / "lambdas" / "email"


def load(mailboxes=("agent", "billing")):
    import importlib.util
    import os

    sys.path.insert(0, str(REPO / "modules" / "aws"))
    os.environ.update({
        "EMAIL_BUCKET": "gerp-agent-testfirm-email", "DEDUP_TABLE": "dedup",
        "AGENT_ADDRESS": "agent@testfirm.agents.gradienterp.cloud",
        "ALLOWLIST": "owner@shop.com", "GERP_ID": "testfirm",
        "SETTINGS_TABLE": "gerp-settings-testfirm",
        "AGENT_RUNTIME_ENDPOINT_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
    })
    spec = importlib.util.spec_from_file_location("lambda_inbound_email", LAMBDA / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._MAILBOXES = set(mailboxes)
    return mod


def message(to, frm="someone@elsewhere.com", subject="hello", authenticated=True):
    """Shaped like what SES actually delivers — including Authentication-Results, which the
    agent-path gate reads. A fixture without it silently fails the gate for the wrong reason."""
    auth = (f"Authentication-Results: amazonses.com; spf=pass smtp.mailfrom={frm}; "
            f"dkim=pass header.i=@{frm.split('@')[1]}; dmarc=pass header.from={frm.split('@')[1]}\r\n"
            if authenticated else "")
    raw = (f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n{auth}"
           f"Message-ID: <abc@x>\r\n\r\nbody text\r\n").encode()
    return raw, email.message_from_bytes(raw, policy=default_policy)


def run(to, mailboxes=("agent", "billing"), frm="someone@elsewhere.com", authenticated=True):
    """Deliver one message and report where it was filed + whether the agent was woken."""
    mod = load(mailboxes)
    raw, _ = message(to, frm, authenticated=authenticated)
    puts, deletes, invokes = [], [], []

    with patch.object(mod, "s3") as s3, patch.object(mod, "ddb") as ddb, \
         patch.object(mod, "agentcore") as ac, patch.object(mod, "ses"):
        s3.get_object.return_value = {"Body": type("B", (), {"read": lambda self: raw})()}
        s3.put_object.side_effect = lambda **kw: puts.append(kw["Key"])
        s3.delete_object.side_effect = lambda **kw: deletes.append(kw["Key"])
        ddb.get_item.return_value = {"Item": {"value": {"L": [{"S": m} for m in mailboxes]}}}
        ac.invoke_agent_runtime.side_effect = lambda **kw: invokes.append(kw) or {
            "response": type("R", (), {"read": lambda self: b'{"response":"hi"}'})()
        }
        mod.handler({"Records": [{"s3": {"object": {"key": "inbound/msg123"}}}]}, None)

    return {"filed": puts, "cleared": deletes, "woke": len(invokes)}


def test_mail_to_a_quiet_mailbox_is_filed_and_wakes_nothing():
    out = run("billing@testfirm.agents.gradienterp.cloud")
    assert out["filed"] == ["in/billing/msg123"]
    assert out["cleared"] == ["inbound/msg123"], "the landing zone must not accumulate"
    assert out["woke"] == 0, "a forwarded reply must not start a model turn"


def test_mail_to_agent_wakes_the_runtime():
    out = run("agent@testfirm.agents.gradienterp.cloud", frm="owner@shop.com")
    assert out["filed"] == ["in/agent/msg123"]
    assert out["woke"] == 1


def test_an_undeclared_local_part_is_parked_as_spam():
    out = run("sales@testfirm.agents.gradienterp.cloud")
    assert out["filed"] == ["in/spam/msg123"], "parked, so a customer who guessed wrong is readable"
    assert out["woke"] == 0


def test_filing_leaves_the_watched_prefix():
    """SES writes to inbound/ and that triggers the handler. Filing into inbound/ would fire it
    again on its own output — forever."""
    for to in ("billing@x", "agent@x", "nope@x"):
        out = run(to, frm="owner@shop.com")
        assert all(k.startswith("in/") for k in out["filed"]), out["filed"]
        assert not any(k.startswith("inbound/") for k in out["filed"]), \
            f"filed back into the watched prefix: {out['filed']}"


def test_the_dmarc_gate_applies_only_to_the_agent_path():
    """Forwarded mail routinely fails DMARC — the forwarding server is not authorised for the
    original sender's domain. It lands in a quiet mailbox, so it must not be gated."""
    out = run("billing@testfirm.agents.gradienterp.cloud", frm="stranger@nowhere.com",
              authenticated=False)
    assert out["filed"] == ["in/billing/msg123"], "a quiet mailbox must accept unauthenticated mail"
    assert out["woke"] == 0


def _headers(*lines, frm="owner@firm.example"):
    raw = ("".join(f"{l}\r\n" for l in lines) + f"From: {frm}\r\nTo: agent@x\r\nSubject: hi\r\n"
           "Message-ID: <m@x>\r\n\r\nbody\r\n").encode()
    return email.message_from_bytes(raw, policy=default_policy)


def test_only_the_header_ses_wrote_authenticates_a_sender():
    """The gate read every Authentication-Results in the message and took a bare `dmarc=pass`.
    A sender writes headers below the one SES prepends, so a forged owner address passed."""
    mod = load(("agent",))
    ok = mod._sender_authenticated
    ses_pass = "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=owner@firm.example; dkim=pass header.i=@firm.example; dmarc=pass header.from=firm.example"
    ses_fail = "Authentication-Results: amazonses.com; spf=fail smtp.mailfrom=owner@firm.example; dkim=none; dmarc=fail header.from=firm.example"
    forged = "Authentication-Results: x; dmarc=pass header.from=firm.example"
    assert ok(_headers(ses_pass), "owner@firm.example") is True
    # SES says fail, the sender's own header below says pass
    msg = email.message_from_bytes((ses_fail + "\r\n").encode() + _headers(forged).as_bytes(), policy=default_policy)
    assert ok(msg, "owner@firm.example") is False
    # the first header isn't SES's
    assert ok(_headers(forged), "owner@firm.example") is False
    assert ok(_headers("Authentication-Results: mx.evil.example; dmarc=pass header.from=firm.example"), "owner@firm.example") is False
    # dmarc=pass for another domain, and dkim=pass signed by another domain
    assert ok(_headers("Authentication-Results: amazonses.com; dmarc=pass header.from=evil.example"), "owner@firm.example") is False
    assert ok(_headers("Authentication-Results: amazonses.com; dkim=pass header.i=@evil.example; dmarc=none"), "owner@firm.example") is False
    # a failing dkim clause naming the right domain doesn't count
    assert ok(_headers("Authentication-Results: amazonses.com; dkim=fail header.i=@firm.example; dmarc=none"), "owner@firm.example") is False
    # aligned dkim with no DMARC policy still counts; a subdomain sender aligns to its signing domain
    assert ok(_headers("Authentication-Results: amazonses.com; dkim=pass header.d=firm.example; dmarc=none"), "owner@firm.example") is True
    assert ok(_headers("Authentication-Results: amazonses.com; dkim=pass header.d=firm.example; dmarc=none"), "owner@mail.firm.example") is True


def test_agent_stays_reachable_when_the_list_omits_it():
    """A firm that empties the mailbox list must not lose the way to reach its own agent."""
    out = run("agent@testfirm.agents.gradienterp.cloud", mailboxes=("billing",), frm="owner@shop.com")
    assert out["filed"] == ["in/agent/msg123"]
    assert out["woke"] == 1


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all mailbox sorting tests passed")
