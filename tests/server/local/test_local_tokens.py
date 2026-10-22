"""Every local token is one the Cognito stand-in signed. `_jwt` signs and verifies; the bff image
(`_image._claims_from_bearer`) accepts a bearer only from the stand-in at LOCAL_COGNITO_URL and
gives an unsigned, forged, expired or foreign one no claims at all."""

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import _jwt  # noqa: E402

ISSUER = "http://127.0.0.1:1"      # a port nothing answers on: a refetch of the jwks fails fast


def _claims(sub="alice", **over):
    now = int(time.time())
    return {"sub": sub, "email": f"{sub}@localhost", "iat": now, "exp": now + 600, "iss": ISSUER,
            "token_use": "id", "aud": "local", **over}


def test_a_signed_token_verifies_against_its_jwk_and_nothing_else_does():
    key, other = _jwt.new_key(), _jwt.new_key()
    jwks = {"k1": _jwt.jwk(key, "k1")}
    tok = _jwt.sign(_claims(), key, "k1")
    assert _jwt.verify(tok, jwks, ISSUER)["sub"] == "alice"

    def refused(token, why):
        try:
            _jwt.verify(token, jwks, ISSUER)
        except Exception as e:  # noqa: BLE001
            return type(e).__name__, str(e)
        raise AssertionError(f"accepted a token that should be refused: {why}")

    h, p, s = tok.split(".")
    none = _jwt.b64url(b'{"alg":"none","typ":"JWT"}')
    assert refused(f"{none}.{p}.local", "alg none")[1] == "unexpected alg"
    assert refused(_jwt.sign(_claims(), key, "k9"), "unknown kid")[1] == "unknown signing key"
    assert refused(_jwt.sign(_claims(), other, "k1"), "another key, our kid")[0] == "InvalidSignature"
    assert refused(_jwt.sign(_claims(exp=int(time.time()) - 120), key, "k1"), "expired")[1] == "token expired"
    assert refused(_jwt.sign(_claims(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x"), key, "k1"),
                   "another issuer")[1] == "bad issuer"
    assert refused(f"{h}.{p}", "two parts")[0] == "ValueError"


def test_the_stand_in_issues_tokens_its_own_jwks_verifies():
    os.environ["LOCAL_COGNITO_URL"] = ISSUER
    for m in [m for m in sys.modules if m.endswith("cognito.server") or m == "server"]:
        del sys.modules[m]
    sys.path.insert(0, str(HERE / "cognito"))
    import server  # noqa: E402  — tests/server/cognito/server.py, its key made at import
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    jwks = {k["kid"]: k for k in c.get("/.well-known/jwks.json").json()["keys"]}
    got = c.get("/token", params={"sub": "bob", "email": "bob@localhost"}).json()
    for which, use in (("IdToken", "id"), ("AccessToken", "access")):
        claims = _jwt.verify(got[which], jwks, ISSUER)
        assert claims["sub"] == "bob" and claims["token_use"] == use
    assert _jwt.verify(got["IdToken"], jwks, ISSUER)["aud"] == "local"
    assert got["RefreshToken"] == "rt_bob"
    # the user exists now: the refresh the SPA does on reload answers for it
    r = c.post("/", headers={"x-amz-target": "AWSCognitoIdentityProviderService.InitiateAuth"},
               json={"AuthFlow": "REFRESH_TOKEN_AUTH", "AuthParameters": {"REFRESH_TOKEN": "rt_bob"}})
    assert _jwt.verify(r.json()["AuthenticationResult"]["IdToken"], jwks, ISSUER)["sub"] == "bob"


def test_the_bff_image_takes_claims_from_the_stand_ins_signature_only():
    import _image
    key = _jwt.new_key()
    tok = _jwt.sign(_claims(), key, "k1")
    # no stand-in named: a bearer carries no claims, whoever signed it
    os.environ.pop("LOCAL_COGNITO_URL", None)
    _image._JWKS.clear()
    _image._JWKS["k1"] = _jwt.jwk(key, "k1")
    assert _image._claims_from_bearer(tok) == {}
    # the stand-in named and its key known: the signed token's claims, and nothing from the others
    os.environ["LOCAL_COGNITO_URL"] = ISSUER
    assert _image._claims_from_bearer(tok)["sub"] == "alice"
    none = _jwt.b64url(b'{"alg":"none","typ":"JWT"}')
    assert _image._claims_from_bearer(f"{none}.{tok.split('.')[1]}.local") == {}
    assert _image._claims_from_bearer(_jwt.sign(_claims(), _jwt.new_key(), "k1")) == {}
    assert _image._claims_from_bearer("not-a-token") == {}
    _image._JWKS.clear()
    os.environ.pop("LOCAL_COGNITO_URL", None)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all local token tests passed")
