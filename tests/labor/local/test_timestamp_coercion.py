"""manage_labor's put op coerces the two input shapes a caller naturally sends but DDB/the code rejected.

`started_at` is `timestamp_ms` in the registry, but ISO 8601 is what every other surface here speaks
and what a caller naturally reaches for. Sending it used to crash on `int("2026-07-29T14:00:00Z")`
while composing the entry_id. Now it normalizes, and the STORED value stays ms — close_handler and
pay_run read it as a number.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "clock"))  # the shared civil-clock lib
from _helpers import load_lambda, scratch_env

ISO = "2026-07-29T14:00:00Z"
ISO_MS = 1785333600000          # the same instant, ms-epoch


def _inv(lam, body):
    resp = lam.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _shift(**over):
    return {"op": "put", "entity": "time_entry", "worker_id": "ava-reyes", "role": "barista", **over}


def test_iso_started_at_is_normalized_to_ms():
    with scratch_env():
        put = load_lambda("manage_labor")
        code, body = _inv(put, _shift(started_at=ISO))
        assert code == 200, body
        row = body.get("item") or body
        assert row["started_at"] == ISO_MS, row          # stored as ms, not the string
        assert row["entry_id"].startswith(f"{ISO_MS}#")  # and the id composed off it


def test_ms_epoch_still_works():
    with scratch_env():
        put = load_lambda("manage_labor")
        code, body = _inv(put, _shift(started_at=ISO_MS))
        assert code == 200, body
        row = body.get("item") or body
        assert row["started_at"] == ISO_MS


def test_garbage_is_a_clean_error_not_a_crash():
    with scratch_env():
        put = load_lambda("manage_labor")
        code, body = _inv(put, _shift(started_at="next tuesday"))
        assert code == 400, body
        assert "started_at" in body.get("error", "")


def test_float_rate_is_coerced_for_ddb():
    """A registry-declared `decimal` (a worker's rate) arrives as a JSON number → python float, and
    boto3's document interface REJECTS floats outright. Every other module has to_ddb; labor didn't,
    so `rate: 18.5` raised TypeError and 500'd."""
    with scratch_env():
        put = load_lambda("manage_labor")
        code, body = _inv(put, {"op": "put", "entity": "worker", "contact_id": "mira-kwon",
                                "role": "barista", "rate": 18.5, "location": "1"})
        assert code == 200, body
        row = body.get("item") or body
        assert float(row["rate"]) == 18.5


def test_a_naive_time_lands_in_the_business_zone_not_utc():
    """The trapdoor this closed. A shift stated as "7am" with no offset used to be resolved against
    the PROCESS timezone — UTC in Lambda — so a Pacific 7am was stored as 7am UTC and every wage
    accrual downstream was seven hours off, with no error anywhere."""
    import datetime as dt
    import importlib
    from zoneinfo import ZoneInfo
    os.environ["GERP_TIMEZONE"] = "America/Los_Angeles"
    sys.modules.pop("clock", None)
    importlib.import_module("clock")
    try:
        with scratch_env():
            put = load_lambda("manage_labor")
            code, body = _inv(put, _shift(started_at="2026-07-27T07:00:00"))
            assert code == 200, body
            want = int(dt.datetime(2026, 7, 27, 7, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp() * 1000)
            got = int((body.get("item") or body)["started_at"])
            assert got == want, f"stored {got}, want {want}"
            assert dt.datetime.fromtimestamp(got / 1000, dt.timezone.utc).hour == 14, "7am PDT is 14:00Z"
    finally:
        os.environ.pop("GERP_TIMEZONE", None)
        sys.modules.pop("clock", None)


def test_an_unconfigured_gerp_still_behaves_as_utc():
    """No timezone set must mean exactly the prior behaviour — this change is opt-in per gerp."""
    import datetime as dt
    import importlib
    os.environ.pop("GERP_TIMEZONE", None)
    sys.modules.pop("clock", None)
    importlib.import_module("clock")
    with scratch_env():
        put = load_lambda("manage_labor")
        code, body = _inv(put, _shift(started_at="2026-07-27T07:00:00"))
        assert code == 200, body
        got = int((body.get("item") or body)["started_at"])
        assert dt.datetime.fromtimestamp(got / 1000, dt.timezone.utc).hour == 7
    sys.modules.pop("clock", None)


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all labor timestamp tests passed")
