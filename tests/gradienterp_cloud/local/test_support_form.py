"""The /support page: a message to the operator with the address in no page.

The landing's Private support glyph opens `/support` in a new tab; the page posts to
`POST /api/support`, the one `/api` path answered without a subject; the BFF sends through SES
from the operator's sender to SUPPORT_EMAIL, Reply-To the email typed. The address lives in the
BFF's env and nowhere a scraper reads.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import event, load_handler, scratch_env  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "prod" / "gradienterp_cloud" / "web"
GERPS = {}


class _SES:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_email(self, **kw):
        if self.fail:
            raise RuntimeError("MessageRejected")
        self.sent.append(kw)
        return {"MessageId": "m-1"}


def _mod(ses):
    mod = load_handler()
    mod.SENDER_EMAIL, mod.SUPPORT_EMAIL = "ops+sender@example.test", "hello@example.test"
    real = mod._aws
    mod._aws = lambda svc, **kw: ses if svc == "ses" else real(svc, **kw)
    return mod


def _post(mod, body):
    return mod.handler(event("POST", "/api/support", body=body), None)


def test_a_message_is_sent_to_the_support_address_reply_to_the_sender_with_no_login():
    with scratch_env(GERPS):
        ses = _SES()
        mod = _mod(ses)
        resp = _post(mod, {"email": "ken@cafe.example", "subject": "invoice question", "message": "where is my August invoice?"})
        assert resp["statusCode"] == 200 and json.loads(resp["body"]) == {"sent": True}
        [m] = ses.sent
        assert m["Source"] == "ops+sender@example.test"
        assert m["Destination"] == {"ToAddresses": ["hello@example.test"]}
        assert m["ReplyToAddresses"] == ["ken@cafe.example"]
        assert m["Message"]["Subject"]["Data"] == "[support] invoice question"
        body = m["Message"]["Body"]["Text"]["Data"]
        assert body.startswith("from: ken@cafe.example\nat: 20") and body.rstrip().endswith("where is my August invoice?")
        assert "hello@" not in resp["body"], "the address is in no response"


def test_each_refusal_names_its_field_and_sends_nothing():
    with scratch_env(GERPS):
        ses = _SES()
        mod = _mod(ses)
        good = {"email": "ken@cafe.example", "subject": "s", "message": "m"}
        cases = [
            ({**good, "email": "ken"}, "email"),
            ({**good, "subject": ""}, "subject"),
            ({**good, "subject": "x" * 201}, "subject"),
            ({**good, "message": "  "}, "message"),
            ({**good, "message": "x" * 5001}, "message"),
            ({**good, "website": "http://spam.example"}, "website"),
        ]
        for body, field in cases:
            resp = _post(mod, body)
            assert resp["statusCode"] == 400 and json.loads(resp["body"])["field"] == field, (field, resp)
        assert ses.sent == []
        assert mod.handler(event("POST", "/api/support", auth="x"), None)["statusCode"] == 400, "no body is a 400, not a 500"


def test_a_send_ses_refuses_is_a_502_and_nothing_else_changes():
    with scratch_env(GERPS):
        mod = _mod(_SES(fail=True))
        resp = _post(mod, {"email": "ken@cafe.example", "subject": "s", "message": "m"})
        assert resp["statusCode"] == 502 and "try again" in json.loads(resp["body"])["error"]


def test_the_support_path_is_the_one_api_path_without_a_subject():
    with scratch_env(GERPS):
        mod = _mod(_SES())
        assert mod.handler(event("GET", "/api/support"), None)["statusCode"] == 401, "only the POST"
        assert mod.handler(event("GET", "/api/gerps"), None)["statusCode"] == 401


def test_the_page_is_served_at_support_and_no_page_carries_the_address():
    with scratch_env(GERPS):
        mod = load_handler()
        resp = mod.handler(event("GET", "/support"), None)
        assert resp["statusCode"] == 200 and resp["headers"]["content-type"].startswith("text/html")
        assert 'id="f"' in resp["body"] and '<script src="/support.js">' in resp["body"]
        assert "/api/support" in mod.handler(event("GET", "/support.js"), None)["body"]
    address = re.compile(r"mailto:|[A-Za-z0-9._+-]+@gradienterp\.cloud")
    for f in list(WEB.glob("*.html")) + list(WEB.glob("*.js")):
        hits = [m.group(0) for m in address.finditer(f.read_text())
                if not m.group(0).startswith(("ops+", "<gerp_id>"))]   # the sender and the agent mailbox pattern in copy are not the support address
        assert not [h for h in hits if h.startswith("mailto:") or h.startswith("hello@")], (f.name, hits)


def test_the_page_ships_in_the_bff_bundle():
    """The lambda serves web/ from its own zip, and `BFF_FILES` (scripts/deploy.py) is that zip's
    list: a page missing there falls back to the SPA shell on the live site while the local
    handler, reading the repo's web/, serves it."""
    src = (REPO / "scripts" / "deploy.py").read_text()
    files = src[src.index("BFF_FILES = ["):src.index("]", src.index("BFF_FILES = ["))]
    assert '"support.html"' in files


def test_the_landing_glyph_opens_the_page_in_a_new_tab():
    for f in ("index.html", "app.js"):
        s = (WEB / f).read_text()
        assert 'href="/support" target="_blank" rel="noopener" title="Private support"' in s, f



def test_the_support_route_is_its_own_and_throttled():
    """Unauthenticated, and each post an SES send from the identity signups mail from: its own route
    so the stage throttles it, and no authorizer (there is no account behind a support message)."""
    tf = (Path(__file__).resolve().parents[3] / "prod" / "gradienterp_cloud" / "main.tf").read_text()
    route = tf[tf.index('resource "aws_apigatewayv2_route" "support"'):]
    route = route[:route.index("\n}\n")]
    assert 'route_key = "POST /api/support"' in route and "authorization_type" not in route
    stage = tf[tf.index('resource "aws_apigatewayv2_stage" "default"'):]
    settings = stage[stage.index("route_settings {"):stage.index("}", stage.index("route_settings {"))]
    assert "aws_apigatewayv2_route.support.route_key" in settings
    assert "throttling_rate_limit" in settings and "throttling_burst_limit" in settings

if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all support form tests passed")
