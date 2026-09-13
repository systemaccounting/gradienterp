"""Tests for the optimizer registry query.

Seed a few gerp-profiles + reindex them, then assert `find_profiles` selects the right ones by
capability (NAICS/SOC), intersects capability with location, radius-filters, and that reindex drops
stale index rows when a match-key value changes. The match-key field set is read from the real
`profile_fields.json`, so this also proves the schema drives the index.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "prod" / "optimizer" / "lambdas"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
os.environ["PROFILE_FIELDS"] = str(REPO_ROOT / "modules" / "schemas" / "data" / "profile_fields.json")
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))

import _helpers as H  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(f"opt_{name}", LAMBDAS / name / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FIND = _load("find_profiles")
REINDEX = _load("reindex")

from decimal import Decimal  # noqa: E402

from boto3.dynamodb.types import TypeSerializer  # noqa: E402

_ser = TypeSerializer()


def _image(profile):
    # DDB stores numbers as Decimal; mirror that so the serialized image matches a real stream record.
    return {k: _ser.serialize(Decimal(str(v)) if isinstance(v, float) else v) for k, v in profile.items()}


def _stream(*records):
    """A DDB stream event: each record = (eventName, image) where image is the New (INSERT/MODIFY)
    or Old (REMOVE) profile — serialized to the DDB-typed shape the real stream delivers."""
    out = []
    for name, profile in records:
        key = "OldImage" if name == "REMOVE" else "NewImage"
        out.append({"eventName": name, "dynamodb": {key: _image(profile)}})
    return {"Records": out}


def _fresh():
    os.environ["PROFILES_TABLE"] = make_table("profiles")
    os.environ["INDEX_TABLE"] = make_table("profile-index")
    H._match_fields_cache = None


def _grocer():
    return {"gerp_profile_id": "grocer-gerp", "kind": "business", "edges": "grocer-gerp",
            "display_name": "Green Grocer", "city": "Chicago", "state": "IL", "lat": 41.88, "lng": -87.63,
            "naics": ["445110"]}


def _fixit():
    return {"gerp_profile_id": "fixit-gerp", "kind": "business", "edges": "fixit-gerp",
            "display_name": "Fixit Appliance Repair", "city": "Chicago", "state": "IL", "lat": 41.90, "lng": -87.65,
            "naics": ["811412"]}


def _alice():
    return {"gerp_profile_id": "alice-sub", "kind": "person", "edges": "alice-sub",
            "display_name": "Alice R", "city": "Chicago", "state": "IL", "lat": 41.89, "lng": -87.64,
            "soc": ["49-9031"]}


def _seed(*profiles):
    _fresh()
    for profile in profiles:
        H.put_profile(profile)
        H.reindex(profile)


def _find(**payload):
    return json.loads(FIND.handler(payload, None)["body"])


def test_match_key_fields_from_schema():
    _fresh()
    fields = set(H.match_key_fields())
    assert {"naics", "soc", "city", "state"} <= fields   # the schema's match-keys
    assert "email" not in fields and "lat" not in fields  # a plain field / a constraint isn't a match-key


def test_find_by_capability():
    _seed(_grocer(), _fixit(), _alice())
    result = _find(match=["naics#811412"])
    assert {p["gerp_profile_id"] for p in result["profiles"]} == {"fixit-gerp"}   # only the repair NAICS


def test_find_intersects_capability_and_location():
    _seed(_fixit(), _grocer())
    assert {p["gerp_profile_id"] for p in _find(match=["naics#811412", "city#chicago"])["profiles"]} == {"fixit-gerp"}
    assert _find(match=["naics#811412", "city#newyork"])["count"] == 0   # right capability, wrong city


def test_find_person_by_occupation():
    _seed(_alice(), _fixit())
    assert {p["gerp_profile_id"] for p in _find(match=["soc#49-9031"])["profiles"]} == {"alice-sub"}


def test_a_measured_occupation_indexes_by_its_code():
    """`soc` is a measured distribution — objects carrying `code`, `share`, `hours` — and the
    index keys on the code, so a hub query by occupation finds the person the same way."""
    _seed({**_alice(), "soc": [{"code": "35-3023.01", "share": 0.62, "hours": 707},
                                {"code": "11-9051.00", "share": 0.38, "hours": 433}],
           "soc_window": "2025-09-01..2026-09-01"})
    assert H.index_keys({"soc": [{"code": "35-3023.01"}], "city": "Chicago"}) == {"soc#35-3023.01", "city#chicago"}
    assert {p["gerp_profile_id"] for p in _find(match=["soc#35-3023.01"])["profiles"]} == {"alice-sub"}
    assert {p["gerp_profile_id"] for p in _find(match=["soc#11-9051.00"])["profiles"]} == {"alice-sub"}
    assert _find(match=["soc#49-9031"])["count"] == 0, "the measured list replaced the fixture's"


def test_radius_filter():
    _seed(_fixit())   # fixit ~Chicago
    assert _find(match=["naics#811412"], near={"lat": 41.88, "lng": -87.63, "radius_km": 10})["count"] == 1
    assert _find(match=["naics#811412"], near={"lat": 40.71, "lng": -74.00, "radius_km": 10})["count"] == 0  # NYC


def test_reindex_removes_stale():
    _seed(_fixit())
    assert {p["gerp_profile_id"] for p in _find(match=["city#chicago"])["profiles"]} == {"fixit-gerp"}
    moved = _fixit()
    moved["city"] = "Denver"
    H.put_profile(moved)
    H.reindex(moved)
    assert _find(match=["city#chicago"])["count"] == 0   # stale chicago row dropped
    assert {p["gerp_profile_id"] for p in _find(match=["city#denver"])["profiles"]} == {"fixit-gerp"}


def test_missing_match_is_400():
    _fresh()
    assert FIND.handler({}, None)["statusCode"] == 400


# ─── the writer: gerp-profiles stream → reindex lambda ───

def test_stream_insert_indexes():
    _fresh()
    REINDEX.handler(_stream(("INSERT", _fixit())), None)
    assert H.query_index("naics#811412") == ["fixit-gerp"]   # capability indexed off the stream image
    assert H.query_index("city#chicago") == ["fixit-gerp"]


def test_stream_modify_drops_stale():
    _fresh()
    REINDEX.handler(_stream(("INSERT", _fixit())), None)
    moved = _fixit()
    moved["city"] = "Denver"
    REINDEX.handler(_stream(("MODIFY", moved)), None)
    assert H.query_index("city#chicago") == []               # stale row dropped on MODIFY
    assert H.query_index("city#denver") == ["fixit-gerp"]


def test_stream_remove_deindexes():
    _fresh()
    REINDEX.handler(_stream(("INSERT", _fixit())), None)
    REINDEX.handler(_stream(("REMOVE", _fixit())), None)
    assert H.query_index("naics#811412") == []               # all rows cleared on REMOVE
    assert H.query_index("city#chicago") == []


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all optimizer tests passed")
