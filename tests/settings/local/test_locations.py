"""Local tests for the LOCATION# rows lib (modules/settings/lambdas/_locations.py) +
manage_locations tool: list/add/update, ordinal allocation, rename-keeps-ordinal."""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "out" / "locations_test"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "tests"))
from helpers.localaws import make_table   # noqa: E402

os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
# The container half (entrypoint.py) uses raw boto3, not modules/aws/aws.py — it is the agent IMAGE, not a
# lambda, so it does not bundle modules/aws. botocore honours the per-service endpoint variable, so
# pointing that at the same moto makes both writers share one real table.
os.environ["AWS_ENDPOINT_URL_DYNAMODB"] = os.environ.get("LOCAL_AWS_ENDPOINT", "http://localhost:5000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _fresh_table():
    """A clean settings table for one case — a table now, where this used to unlink a JSON file."""
    os.environ["SETTINGS_TABLE"] = make_table("settings")
    return os.environ["SETTINGS_TABLE"]

sys.path.insert(0, str(REPO / "modules" / "settings" / "lambdas"))


def _load():
    _fresh_table()
    for m in ("_locations", "ml_main"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(
        "ml_main", REPO / "modules/settings/lambdas/manage_locations/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _inv(mod, body):
    r = mod.handler(body, None)
    return r["statusCode"], json.loads(r["body"])


def test_add_allocates_ordinals_and_lists_sorted():
    ml = _load()
    code, body = _inv(ml, {"op": "add", "city": "losangeles", "name": "downtown", "label": "Downtown"})
    assert code == 200 and body["location"]["ordinal"] == "1"
    code, body = _inv(ml, {"op": "add", "city": "losangeles", "name": "lax"})
    assert code == 200 and body["location"]["ordinal"] == "2"
    code, body = _inv(ml, {"op": "list"})
    assert [l["ordinal"] for l in body["locations"]] == ["1", "2"]
    assert body["locations"][1]["label"] == "Lax"  # defaulted from the name


def test_update_keeps_ordinal_through_rename_and_sets_provider_id():
    ml = _load()
    _inv(ml, {"op": "add", "name": "main"})
    code, body = _inv(ml, {"op": "update", "ordinal": "1", "city": "losangeles",
                           "name": "downtown", "label": "DTLA", "square_location_id": "LSQ1"})
    assert code == 200, body
    code, body = _inv(ml, {"op": "list"})
    locs = body["locations"]
    assert len(locs) == 1 and locs[0]["ordinal"] == "1"
    assert locs[0]["name"] == "downtown" and locs[0]["square_location_id"] == "LSQ1"


def test_update_requires_ordinal():
    ml = _load()
    code, body = _inv(ml, {"op": "update", "label": "X"})
    assert code == 400 and "ordinal" in body["error"]


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print("ok", fn)
    print("all locations tests passed")
