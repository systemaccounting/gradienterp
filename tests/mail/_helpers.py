"""Shared helpers for tests/mail/local/ — the three outbound-mail lambdas in modules/agent."""

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "modules" / "agent" / "lambdas"


def load_lambda(name, **env):
    """Import a handler fresh. The lambdas read config at IMPORT time, so env goes first."""
    sys.path.insert(0, str(LAMBDAS_DIR / name))
    sys.path.insert(0, str(REPO_ROOT / "modules" / "aws"))
    for mod_name in list(sys.modules):
        if mod_name in ("_senders", "_smtp") or mod_name.startswith("lambda_mail_"):
            del sys.modules[mod_name]

    defaults = {
        "GERP_ID": "testfirm",
        "SETTINGS_TABLE": "gerp-settings-testfirm",
        "SECRET_PARAM_PREFIX": "/gradienterp/customers/testfirm/secrets",
        "SEND_LOG_GROUP": "/aws/lambda/gerp-mail-testfirm-send_email",
    }
    os.environ.update({**defaults, **env})

    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_mail_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def invoke(mod, payload):
    import json

    resp = mod.handler(payload, None)
    return resp["statusCode"], json.loads(resp["body"])


class FakeTable:
    """The settings table, holding SENDER# rows."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def query(self, **kw):
        return {"Items": list(self.rows)}

    def put_item(self, Item):
        self.rows = [r for r in self.rows if r["sk"] != Item["sk"]] + [Item]

    def delete_item(self, Key):
        self.rows = [r for r in self.rows if r["sk"] != Key["sk"]]

    def update_item(self, Key, UpdateExpression, **kw):
        for r in self.rows:
            if r["sk"] != Key["sk"]:
                continue
            if UpdateExpression.startswith("REMOVE"):
                r.pop("default", None)
            else:
                r["default"] = True
            r["updated_at"] = kw.get("ExpressionAttributeValues", {}).get(":n", 0)


class FakeSMTP:
    """A mail server that records what it was asked to send, and can be told to refuse."""

    def __init__(self, refuse=None, drop_after=None, fail_login=False):
        self.sent = []
        self.refuse = refuse or {}       # address -> (code, text)
        self.drop_after = drop_after     # close the connection after N sends
        self.fail_login = fail_login
        self.connections = 0
        self.quit_called = 0

    def connect(self, *a, **kw):
        self.connections += 1
        return self

    # the smtplib surface the sender uses
    def starttls(self):
        pass

    def login(self, user, password):
        import smtplib

        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    def send_message(self, msg):
        import smtplib

        to = msg["To"]
        if to in self.refuse:
            code, text = self.refuse[to]
            raise smtplib.SMTPRecipientsRefused({to: (code, text.encode())})
        if self.drop_after is not None and len(self.sent) >= self.drop_after:
            self.drop_after = None       # one drop, then it behaves
            raise smtplib.SMTPServerDisconnected("closed by server")
        self.sent.append({"to": to, "subject": msg["Subject"], "body": msg.get_content().strip()})

    def quit(self):
        self.quit_called += 1
