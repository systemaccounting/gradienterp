"""The committed image snapshots carry no owner address and no portal slug. `snapshot.py` replaces
each `REDACT` value with its placeholder before writing, in every env var that contains it."""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import snapshot  # noqa: E402

EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
PUBLISHED_DOMAINS = ("gradienterp.cloud", "example.com")


def test_redact_replaces_each_value_wherever_it_appears():
    fns = {
        "report": {"env": {"OWNER_EMAIL": "someone@home.test", "PENDING_TABLE": "t"}},
        "mail": {"env": {"ALLOWLIST": "someone@home.test,other@home.test"}},
        "ui": {"env": {"PORTAL_SLUG": "f00dfeed"}},
        "links": {"env": {"PORTAL_URL": "https://fn.lambda-url.test/f00dfeed", "NOTE": "cc someone@home.test"}},
        "no_config": None,
    }
    snapshot._redact(fns)
    assert fns["report"]["env"] == {"OWNER_EMAIL": snapshot.REDACT["OWNER_EMAIL"], "PENDING_TABLE": "t"}
    assert fns["mail"]["env"]["ALLOWLIST"] == snapshot.REDACT["ALLOWLIST"]
    assert fns["ui"]["env"]["PORTAL_SLUG"] == snapshot.REDACT["PORTAL_SLUG"]
    assert fns["links"]["env"]["PORTAL_URL"] == f"https://fn.lambda-url.test/{snapshot.REDACT['PORTAL_SLUG']}"
    assert fns["links"]["env"]["NOTE"] == f"cc {snapshot.REDACT['OWNER_EMAIL']}"


def test_a_credential_shaped_value_is_refused_by_name_and_names_pass():
    fns = {"ingest": {"env": {"SIGNING_SECRET": "whsec_" + "a1" * 12, "SECRET_NAME": "/gradienterp/x/secrets/stripe"}},
           "chat": {"env": {"TOKEN": "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12}},
           "fine": {"env": {"TABLE": "gerp-settings-x", "PK": "pk_live_" + "z" * 20}},
           "no_config": None}
    assert sorted(snapshot._refuse_credentials(fns)) == ["chat: TOKEN", "ingest: SIGNING_SECRET"]


def test_committed_images_hold_no_credential_shaped_value():
    for path in sorted(HERE.glob("*/image.json")):
        found = snapshot._refuse_credentials(json.loads(path.read_text())["functions"])
        assert not found, f"{path.parent.name}: {found}"


def test_committed_images_hold_only_placeholders_and_published_addresses():
    images = sorted(HERE.glob("*/image.json"))
    assert images, "no image.json found; the glob is broken"
    bad = []
    for path in images:
        for name, cfg in json.loads(path.read_text())["functions"].items():
            for key, value in (cfg.get("env") or {}).items():
                if key in snapshot.REDACT and value != snapshot.REDACT[key]:
                    bad.append(f"{path.parent.name}/{name}: {key} is not its placeholder")
                for domain in EMAIL.findall(value):
                    if not any(domain == d or domain.endswith("." + d) for d in PUBLISHED_DOMAINS):
                        bad.append(f"{path.parent.name}/{name}: {key} holds an address on {domain}")
    assert not bad, "re-take with tests/server/snapshot.py, or add the var to REDACT:\n" + "\n".join(bad)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all snapshot redaction tests passed")
