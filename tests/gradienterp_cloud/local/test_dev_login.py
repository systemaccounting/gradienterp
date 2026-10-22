"""The local stack's sign-in: `GET /dev/login?sub=<sub>` answers a page that stores a token for that
sub and goes to /. The token is one the Cognito stand-in signed — the bff image and the chat lambda
accept no other — so the verb needs the stand-in up, and says so when it is not. The route was once
registered with its query string in the path, which no request matches, so the catch-all served the
app page and nobody was signed in."""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "modules" / "aws"))
sys.path.insert(0, str(REPO / "tests" / "server"))
sys.path.insert(0, str(REPO / "tests" / "server" / "cognito"))
import _jwt  # noqa: E402
import standin  # noqa: E402
from tests.server.bff import dev  # noqa: E402


def _routes_matching(path):
    from starlette.routing import Match
    scope = {"type": "http", "path": path, "method": "GET"}
    return [r for r in dev.router.routes if r.matches(scope)[0] == Match.FULL]


def test_the_login_url_the_docs_give_reaches_the_login_verb():
    assert [r.endpoint for r in _routes_matching("/dev/login")] == [dev.login]
    assert all("?" not in r.path for r in dev.router.routes), "a query string in a route path matches nothing"


def test_the_page_stores_the_stand_ins_token_for_the_sub_and_goes_home():
    proc, url = standin.start()
    try:
        os.environ["LOCAL_COGNITO_URL"] = url
        body = dev.login(sub="local-dev").body.decode()
        token = re.search(r'setItem\(\'id_token\', "([^"]+)"\)', body).group(1)
        with urllib.request.urlopen(f"{url}/.well-known/jwks.json", timeout=3) as r:
            jwks = {k["kid"]: k for k in json.loads(r.read())["keys"]}
        claims = _jwt.verify(token, jwks, url)
        assert claims["sub"] == "local-dev" and claims["email"] == "local-dev@localhost" and claims["token_use"] == "id"
        assert "location.replace('/')" in body
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.environ.pop("LOCAL_COGNITO_URL", None)


def test_without_the_stand_in_the_verb_says_so():
    os.environ["LOCAL_COGNITO_URL"] = "http://127.0.0.1:1"
    try:
        r = dev.login(sub="local-dev")
        assert r.status_code == 503
        assert "127.0.0.1:1" in r.body.decode() and "local-dev.sh --start" in r.body.decode()
    finally:
        os.environ.pop("LOCAL_COGNITO_URL", None)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all dev login tests passed")
