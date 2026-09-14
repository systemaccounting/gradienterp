"""The local stack's sign-in: `GET /dev/login?sub=<sub>` answers a page that stores a token for that
sub and goes to /. The route was once registered with its query string in the path, which no request
matches, so the catch-all served the app page and nobody was signed in."""

import base64
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "modules" / "aws"))
from tests.server.bff import dev  # noqa: E402


def _routes_matching(path):
    from starlette.routing import Match
    scope = {"type": "http", "path": path, "method": "GET"}
    return [r for r in dev.router.routes if r.matches(scope)[0] == Match.FULL]


def test_the_login_url_the_docs_give_reaches_the_login_verb():
    assert [r.endpoint for r in _routes_matching("/dev/login")] == [dev.login]
    assert all("?" not in r.path for r in dev.router.routes), "a query string in a route path matches nothing"


def test_the_page_stores_a_token_for_the_sub_and_goes_home():
    body = dev.login(sub="local-dev").body.decode()
    token = re.search(r'setItem\(\'id_token\', "([^"]+)"\)', body).group(1)
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert payload["sub"] == "local-dev" and payload["email"] == "local-dev@localhost"
    assert "location.replace('/')" in body


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all dev login tests passed")
