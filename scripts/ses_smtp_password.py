"""Derive an SES SMTP password from an IAM secret access key.

SES's SMTP interface does not take AWS keys. It takes a username (the access key id) and a
password derived from the secret access key by the transform below — an HMAC chain over the
region and a fixed set of scope strings, prefixed with a version byte and base64'd. AWS
documents it so the credential can be made without the console.

    python3 scripts/ses_smtp_password.py <secret-access-key> [region]

Print it, hand it to the gerp's secret store, and do not keep it: the secret access key it comes
from is the thing worth protecting, and this is a reversible-free but equivalent credential.
"""

import base64
import hashlib
import hmac
import sys

DATE = "11111111"
SERVICE = "ses"
TERMINAL = "aws4_request"
MESSAGE = "SendRawEmail"
VERSION = 0x04


def sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def smtp_password(secret_access_key: str, region: str) -> str:
    sig = sign(("AWS4" + secret_access_key).encode(), DATE)
    for part in (region, SERVICE, TERMINAL, MESSAGE):
        sig = sign(sig, part)
    return base64.b64encode(bytes([VERSION]) + sig).decode()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(smtp_password(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "us-east-1"))
