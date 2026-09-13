"""The catalog is the PR surface: every vendor row has what the door needs and nothing that
collides. The live check (each row's metadata url answers with the endpoints the row names) is
deliberately outside the suite."""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CATALOG = REPO / "modules" / "mcp" / "data" / "providers.json"
REQUIRED = ("endpoint", "authorization_server", "token_endpoint_auth_method", "scopes", "prefix", "use")


def _rows():
    return json.loads(CATALOG.read_text())


def test_every_row_has_the_required_fields():
    for name, row in _rows().items():
        for k in REQUIRED:
            assert k in row, f"{name} lacks {k}"
        assert row["endpoint"].startswith("https://"), name
        assert row["authorization_server"].startswith("https://") and ".well-known/" in row["authorization_server"], name
        assert isinstance(row["scopes"], list), name
        assert row["token_endpoint_auth_method"] in ("none", "client_secret_post", "client_secret_basic"), name
        assert row["use"].strip(), name


def test_exactly_one_of_registration_endpoint_or_an_owner_made_client():
    for name, row in _rows().items():
        dcr = bool(row.get("registration_endpoint"))
        owner = row.get("client_by") == "owner"
        assert dcr != owner, f"{name}: needs exactly one of registration_endpoint / client_by: owner"
        assert "platform_app" not in row, name
        if owner:
            # the guide is data: where the owner makes the app and what the vendor calls the redirect field
            assert row.get("new_app_url", "").startswith("https://"), name
            assert row.get("callback_field", "").strip(), name


def test_the_owner_made_clients_are_the_three_with_no_registration_endpoint():
    assert {n for n, r in _rows().items() if r.get("client_by") == "owner"} == {"xero", "github", "hubspot"}


def test_prefixes_are_unique_and_plain():
    seen = {}
    for name, row in _rows().items():
        assert re.fullmatch(r"[a-z0-9]+", row["prefix"]), f"{name}: prefix {row['prefix']!r}"
        assert row["prefix"] not in seen, f"{name} and {seen[row['prefix']]} share a prefix"
        seen[row["prefix"]] = name


def test_optional_fields_have_their_shape():
    for name, row in _rows().items():
        if "write_tools" in row:
            assert row["write_tools"] and all(isinstance(t, str) and t for t in row["write_tools"]), name
        if "key_header" in row:
            assert set(row["key_header"]) >= {"name"}, name
        if "registration_endpoint" in row:
            assert row["registration_endpoint"].startswith("https://"), name


def test_the_fifteen_probed_vendors_are_in():
    assert set(_rows()) >= {"stripe", "square", "paypal", "xero", "notion", "linear", "atlassian", "github",
                            "hubspot", "gong", "zapier", "canva", "figma", "dropbox", "sentry"}


def test_a_public_client_is_marked_none():
    # Stripe only issues public clients; the door maps `none` to client_secret_post with a placeholder
    assert _rows()["stripe"]["token_endpoint_auth_method"] == "none"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all catalog tests passed")
