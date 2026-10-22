"""A Cognito stand-in for local dev: the identity-provider actions the SPA calls, in memory.

    :4243   uvicorn tests.server.cognito.server:app

The SPA's `cognito(action, body)` posts to `CFG.COGNITO_IDP` with an `x-amz-target` header; on
localhost that is this. It models signup, confirmation, the login-email change, and a token
refresh, and nothing else — an action it lacks is a 400 that names itself.

Tokens are RS256 JWTs this process signs with a key it makes at start, issuer `LOCAL_COGNITO_URL`
(its own address), audience `local`. `/.well-known/jwks.json` lists the key, and that is what the
bff image (`_image.py`) and the chat lambda verify every bearer against. `GET /token?sub=&email=`
issues a token for any sub, creating the user on sight: the bff's `/dev/login` and the e2e login
helper take theirs there. A session that never signed up here — a token typed into a console — is
admitted on its first attribute call the same way: the user is created from the token's own `sub`
and `email`. Every verification code is `000000`.
"""

import base64
import json
import os
import secrets as _secrets
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _jwt  # noqa: E402

app = FastAPI(title="cognito · local stand-in")
# the SPA on :3000 calls this cross-origin, the way it calls the real regional endpoint, which
# answers preflights itself
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
USERS: dict = {}          # sub → {email, pending_email, confirmed}
BY_EMAIL: dict = {}       # email → sub
REFRESH: dict = {}        # refresh token → sub
CODE = "000000"
ISSUER = os.environ.get("LOCAL_COGNITO_URL", "http://127.0.0.1:4243").rstrip("/")
CLIENT_ID = "local"
KEY = _jwt.new_key()
KID = "local-" + _secrets.token_hex(4)


def _decode(tok: str) -> dict:
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def _tokens(sub: str) -> dict:
    u = USERS[sub]
    now = int(time.time())
    base = {"sub": sub, "email": u["email"], "iat": now, "exp": now + 3600, "iss": ISSUER}
    return {"IdToken": _jwt.sign({**base, "token_use": "id", "aud": CLIENT_ID}, KEY, KID),
            "AccessToken": _jwt.sign({**base, "token_use": "access", "client_id": CLIENT_ID}, KEY, KID),
            "TokenType": "Bearer", "ExpiresIn": 3600}


def _err(kind: str, message: str, status: int = 400):
    return JSONResponse({"__type": kind, "message": message}, status)


def _admit(sub: str, email: str) -> str:
    """A user this stand-in has not seen, created on sight."""
    if sub not in USERS:
        USERS[sub] = {"email": email, "pending_email": "", "confirmed": True}
        if email:
            BY_EMAIL[email] = sub
    return sub


def _user_from_access(token: str):
    """The caller. A token this stand-in never issued still identifies one — created on sight."""
    c = _decode(token or "")
    sub = c.get("sub")
    return _admit(sub, c.get("email", "")) if sub else None


@app.post("/")
async def idp(request: Request):
    action = request.headers.get("x-amz-target", "").split(".")[-1]
    body = json.loads((await request.body()) or b"{}")

    if action == "SignUp":
        email = (body.get("Username") or "").lower()
        if email in BY_EMAIL:
            return _err("UsernameExistsException", "An account with the given email already exists.")
        sub = str(_secrets.token_hex(16))
        USERS[sub] = {"email": email, "pending_email": "", "confirmed": False}
        BY_EMAIL[email] = sub
        return {"UserSub": sub, "UserConfirmed": False,
                "CodeDeliveryDetails": {"Destination": email, "DeliveryMedium": "EMAIL", "AttributeName": "email"}}

    if action == "ConfirmSignUp":
        sub = BY_EMAIL.get((body.get("Username") or "").lower())
        if not sub:
            return _err("UserNotFoundException", "User does not exist.")
        if body.get("ConfirmationCode") != CODE:
            return _err("CodeMismatchException", "Invalid verification code provided, please try again.")
        USERS[sub]["confirmed"] = True
        return {}

    if action == "InitiateAuth":
        if body.get("AuthFlow") == "REFRESH_TOKEN_AUTH":
            sub = REFRESH.get((body.get("AuthParameters") or {}).get("REFRESH_TOKEN", ""))
            if not sub:
                # a session that never signed in here holds no refresh token of ours; the e2e
                # helper's `rt_<sub>` shape names the user directly
                rt = (body.get("AuthParameters") or {}).get("REFRESH_TOKEN", "")
                sub = rt[3:] if rt.startswith("rt_") else None
                if sub and sub not in USERS:
                    return _err("NotAuthorizedException", "Refresh Token has expired")
            if not sub:
                return _err("NotAuthorizedException", "Invalid Refresh Token")
            return {"AuthenticationResult": _tokens(sub)}
        return _err("InvalidParameterException", f"AuthFlow {body.get('AuthFlow')} is not modelled here")

    if action == "UpdateUserAttributes":
        sub = _user_from_access(body.get("AccessToken"))
        if not sub:
            return _err("NotAuthorizedException", "Invalid Access Token")
        attrs = {a["Name"]: a["Value"] for a in body.get("UserAttributes") or []}
        new = (attrs.get("email") or "").lower()
        if not new or "@" not in new:
            return _err("InvalidParameterException", "Invalid email address format.")
        if BY_EMAIL.get(new) not in (None, sub):
            return _err("AliasExistsException", "An account with the email already exists.")
        USERS[sub]["pending_email"] = new      # the login stays the OLD address until verified
        return {"CodeDeliveryDetailsList": [{"Destination": new, "DeliveryMedium": "EMAIL", "AttributeName": "email"}]}

    if action == "VerifyUserAttribute":
        sub = _user_from_access(body.get("AccessToken"))
        if not sub:
            return _err("NotAuthorizedException", "Invalid Access Token")
        if body.get("Code") != CODE:
            return _err("CodeMismatchException", "Invalid verification code provided, please try again.")
        u = USERS[sub]
        if not u["pending_email"]:
            return _err("InvalidParameterException", "No pending attribute change.")
        BY_EMAIL.pop(u["email"], None)
        u["email"], u["pending_email"] = u["pending_email"], ""
        BY_EMAIL[u["email"]] = sub
        return {}

    if action == "GetUser":
        sub = _user_from_access(body.get("AccessToken"))
        if not sub:
            return _err("NotAuthorizedException", "Invalid Access Token")
        return {"Username": sub, "UserAttributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": USERS[sub]["email"]}]}

    return _err("UnknownOperationException", f"{action} is a Cognito action this stand-in does not model "
                                             "(tests/server/cognito/server.py)")


@app.get("/token")
def token(sub: str, email: str = ""):
    """Signed tokens for `sub`, the user created on sight. The bff's `/dev/login` and the e2e login
    helper sign in with these."""
    _admit(sub, email or f"{sub}@localhost")
    return {**_tokens(sub), "RefreshToken": f"rt_{sub}"}


@app.get("/.well-known/jwks.json")
def jwks():
    return {"keys": [_jwt.jwk(KEY, KID)]}


@app.get("/")
def root():
    return {"stand_in": "cognito", "issuer": ISSUER, "client_id": CLIENT_ID, "kid": KID, "users": len(USERS), "code": CODE}
