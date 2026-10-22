"""RS256 tokens for the local stack. The Cognito stand-in (tests/server/cognito) signs with these;
the bff image (`_image.py`) verifies against the stand-in's jwks, and so does the chat lambda, in
Node, with the same jwks. `cryptography` only — it comes with moto."""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    """The public half as a JWK, the shape `/.well-known/jwks.json` lists."""
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": kid,
            "n": b64url(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
            "e": b64url(n.e.to_bytes((n.e.bit_length() + 7) // 8, "big"))}


def sign(claims: dict, key: rsa.RSAPrivateKey, kid: str) -> str:
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    body = b64url(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64url(sig)}"


def verify(token: str, jwks: dict, issuer: str, leeway: int = 60) -> dict:
    """The claims of a token a key in `jwks` (kid → jwk) signed for `issuer`. Raises ValueError on
    another alg, an unknown kid, an expired token or another issuer, and
    `cryptography.exceptions.InvalidSignature` on a signature the key did not make."""
    head, body, sig = token.split(".")
    header = json.loads(b64url_decode(head))
    if header.get("alg") != "RS256":
        raise ValueError("unexpected alg")
    k = jwks.get(header.get("kid"))
    if not k:
        raise ValueError("unknown signing key")
    pub = rsa.RSAPublicNumbers(int.from_bytes(b64url_decode(k["e"]), "big"),
                               int.from_bytes(b64url_decode(k["n"]), "big")).public_key()
    pub.verify(b64url_decode(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    claims = json.loads(b64url_decode(body))
    if claims.get("exp", 0) < time.time() - leeway:
        raise ValueError("token expired")
    if claims.get("iss") != issuer:
        raise ValueError("bad issuer")
    return claims
