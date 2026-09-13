"""The openly_operated flip announces itself on the shared bus — gerp.published / gerp.unpublished
with the gerp id — after the row is written; with no bus wired, the write still stands."""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules"))
from helpers.localaws import make_table, make_bus, drain  # noqa: E402

os.environ["AWS_ENDPOINT_URL_DYNAMODB"] = os.environ.get("LOCAL_AWS_ENDPOINT", "http://localhost:5000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(REPO / "modules" / "settings" / "lambdas"))


def _load(bus=None):
    os.environ["SETTINGS_TABLE"] = make_table("settings")
    os.environ["CUSTOMER_ID"] = "cafe"
    os.environ.pop("OP_EVENT_BUS_ARN", None)
    if bus:
        os.environ["OP_EVENT_BUS_ARN"] = bus
    for m in ("ts_main", "events"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("ts_main", REPO / "modules/settings/lambdas/tenant_settings/main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _put(mod, body):
    return mod.handler({"requestContext": {"http": {"method": "PUT"}}, "body": json.dumps(body)}, None)


def test_the_flip_is_announced_after_the_write():
    bus, q = make_bus("publish-flip")
    mod = _load(bus)
    assert _put(mod, {"openly_operated": True})["statusCode"] == 200
    [ev] = drain(q, expected=1)
    assert ev["detail_type"] == "gerp.published" and ev["detail"]["gerp_id"] == "cafe" and ev["detail"]["at"]
    assert ev["detail"]["openly_operated"] is True, "the envelope reads the row just written"
    assert _put(mod, {"openly_operated": False})["statusCode"] == 200
    [ev] = drain(q, expected=1)
    assert ev["detail_type"] == "gerp.unpublished" and ev["detail"]["openly_operated"] is False


def test_no_bus_wired_leaves_the_write_standing():
    mod = _load()
    assert _put(mod, {"openly_operated": True})["statusCode"] == 200
    assert mod._openly_operated() is True


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all publish flip tests passed")
