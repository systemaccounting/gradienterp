"""Local test for the connect flow tool — connect_bank: op start (Hosted Link) and
op check (finalize + store the access_token).

The Plaid gateway invoke is monkeypatched: Plaid is an external service and the gateway that fronts
it lives in the OPERATOR account, so there is nothing local to stand it up against. Everything else
is real — the link token and the access token land in SSM at the parameter names the deployment
configures, which is where `reconcile` reads the access token back from. These used to be files, so
a test could not tell whether the writer and the reader agreed on a path.

Pins: link_token stored on start, access_token stored + link cleared only on a linked session,
nothing stored while still pending.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import scratch_env, load_lambda, env  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from helpers.localaws import unique  # noqa: E402


def _params():
    """Parameter names unique to one case. SSM is ONE namespace across the whole moto server, so a
    token written by one test is visible to the next unless the path differs — the same reason
    every table name is unique."""
    stem = unique("plaid")
    return f"/gerp/test/{stem}/pending_link", f"/gerp/test/{stem}/access_token"


def _param(name):
    """The SSM value, or None. The same read `reconcile` does."""
    from aws import client
    ssm = client("ssm")
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def _plaid(**resp):
    return lambda _payload: {"ok": True, **resp}


def test_connect_bank_starts_and_stores_link():
    with scratch_env():
        PENDING, TOKEN = _params()
        with env(PLAID_PENDING_LINK_PARAM=PENDING):
            cb = load_lambda("connect_bank")
            cb._gateway = _plaid(link_token="link-sandbox-xyz",
                                 hosted_link_url="https://secure.plaid.com/hl/abc")
            body = json.loads(cb.handler({"op": "start"}, None)["body"])
        assert body["hosted_link_url"].startswith("https://")
        assert _param(PENDING) == "link-sandbox-xyz"   # stored for the finalize step


def test_check_reports_no_pending_link():
    with scratch_env():
        PENDING, TOKEN = _params()
        with env(PLAID_PENDING_LINK_PARAM=PENDING, PLAID_ACCESS_TOKEN_PARAM=TOKEN):
            ck = load_lambda("connect_bank")
            body = json.loads(ck.handler({"op": "check"}, None)["body"])
        assert body["status"] == "no_pending_link"


def test_check_still_linking_stores_nothing():
    with scratch_env():
        PENDING, TOKEN = _params()
        with env(PLAID_PENDING_LINK_PARAM=PENDING, PLAID_ACCESS_TOKEN_PARAM=TOKEN):
            from aws import client
            client("ssm").put_parameter(Name=PENDING, Value="link-sandbox-xyz",
                                        Type="String", Overwrite=True)
            ck = load_lambda("connect_bank")
            ck._gateway = _plaid(status="pending")
            body = json.loads(ck.handler({"op": "check"}, None)["body"])
            assert body["status"] == "pending"
            assert _param(TOKEN) is None            # nothing stored while the session is unfinished
            assert _param(PENDING) is not None      # link kept so a later check can retry


def test_check_links_stores_token_and_clears():
    with scratch_env():
        PENDING, TOKEN = _params()
        with env(PLAID_PENDING_LINK_PARAM=PENDING, PLAID_ACCESS_TOKEN_PARAM=TOKEN):
            from aws import client
            client("ssm").put_parameter(Name=PENDING, Value="link-sandbox-xyz",
                                        Type="String", Overwrite=True)
            ck = load_lambda("connect_bank")
            ck._gateway = _plaid(status="linked", access_token="access-sandbox-abc",
                                 item_id="item-1", institution="Bank of America")
            body = json.loads(ck.handler({"op": "check"}, None)["body"])
            assert body["status"] == "linked" and body["institution"] == "Bank of America"
            assert _param(TOKEN) == "access-sandbox-abc"  # the derived secret reconcile reads
            assert _param(PENDING) is None                # pending link cleared once linked


def test_missing_or_unknown_op_is_refused():
    with scratch_env():
        cb = load_lambda("connect_bank")
        assert cb.handler({}, None)["statusCode"] == 400
        assert cb.handler({"op": "rotate"}, None)["statusCode"] == 400


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all connect_bank tests passed")
