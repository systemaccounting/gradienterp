"""A Cognito stand-in for local dev: the identity-provider actions the SPA calls, in memory.

    :4243   uvicorn tests.server.cognito.server:app

The SPA's `cognito(action, body)` posts to `CFG.COGNITO_IDP` with an `x-amz-target` header; on
localhost that is this. It models signup, confirmation, the login-email change, and a token
refresh, and nothing else — an action it lacks is a 400 that names itself.

Tokens are unsigned JWTs (`header.payload.local`), the shape the e2e login helper mints and the
local BFF decodes without verifying. A session that never signed up here — the e2e helper's token,
a `sessionStorage` one typed into a console — is admitted on its first attribute call: the user is
created from the token's own `sub` and `email`. Every verification code is `000000`.
"""

import base64
import json
import secrets as _secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="cognito · local stand-in")
# the SPA on :3000 calls this cross-origin, the way it calls the real regional endpoint, which
# answers preflights itself
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
USERS: dict = {}          # sub → {email, pending_email, confirmed}
BY_EMAIL: dict = {}       # email → sub
REFRESH: dict = {}        # refresh token → sub
CODE = "000000"


def _b64(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")


def _decode(tok: str) -> dict:
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def _tokens(sub: str) -> dict:
    u = USERS[sub]
    now = int(time.time())
    base = {"sub": sub, "email": u["email"], "iat": now, "exp": now + 3600}
    return {"IdToken": f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({**base, 'token_use': 'id'})}.local",
            "AccessToken": f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({**base, 'token_use': 'access'})}.local",
            "TokenType": "Bearer", "ExpiresIn": 3600}


def _err(kind: str, message: str, status: int = 400):
    return JSONResponse({"__type": kind, "message": message}, status)


def _user_from_access(token: str):
    """The caller. A token this stand-in never issued still identifies one — created on sight."""
    c = _decode(token or "")
    sub = c.get("sub")
    if not sub:
        return None
    if sub not in USERS:
        USERS[sub] = {"email": c.get("email", ""), "pending_email": "", "confirmed": True}
        if c.get("email"):
            BY_EMAIL[c["email"]] = sub
    return sub


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


@app.get("/")
def root():
    return {"stand_in": "cognito", "users": len(USERS), "code": CODE}
