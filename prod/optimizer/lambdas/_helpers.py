"""Shared helpers for the optimizer registry query/index primitives.

The inverted index (gerp-profile-index) is DERIVED from gerp-profiles' match-key fields — which
fields are match-keys is read from `profile_fields.json` (role == "match-key"), so a new match-key
is a schema edit, not code.
"""

import json
import os
from decimal import Decimal
from pathlib import Path

from boto3.dynamodb.conditions import Key as _Key

from aws import table as _ddb_table

# the profile schema — bundled beside the lambda in prod; the repo copy locally
PROFILE_FIELDS = os.environ.get("PROFILE_FIELDS", str(Path(__file__).with_name("profile_fields.json")))


# Resolved at call time so a harness can point at a scratch table between cases.
def profiles_table():
    return _ddb_table(os.environ["PROFILES_TABLE"])


def index_table():
    return _ddb_table(os.environ["INDEX_TABLE"])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def to_ddb(v):
    """Recursively coerce floats to Decimal. A profile carries `lat`/`lng`, and `put_item` refuses
    a float outright."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


# ─── the match-key fields (schema-driven) ───

_match_fields_cache = None


def match_key_fields() -> list[str]:
    """Field names whose `profile_fields.json` role is 'match-key' (across common/person/business) —
    the index's dimensions. Read from the schema so adding a match-key is a JSON edit, not a rewrite."""
    global _match_fields_cache
    if _match_fields_cache is None:
        schema = json.loads(Path(PROFILE_FIELDS).read_text())
        fields = []
        for bucket in schema.values():
            if isinstance(bucket, dict):
                for name, spec in bucket.items():
                    if isinstance(spec, dict) and spec.get("role") == "match-key":
                        fields.append(name)
        _match_fields_cache = fields
    return _match_fields_cache


def index_keys(profile: dict) -> set[str]:
    """The `<field>#<value>` keys a profile is matchable under — its match-key values, lowercased.
    List-valued match-keys (naics/soc) contribute one key per element; a measured element is an
    object carrying `code` (with its share and weight), and the code is the key."""
    keys = set()
    for field in match_key_fields():
        val = profile.get(field)
        if val in (None, ""):
            continue
        for element in (val if isinstance(val, list) else [val]):
            if isinstance(element, dict):
                element = element.get("code", "")
            element = str(element).strip().lower()
            if element:
                keys.add(f"{field}#{element}")
    return keys


# ─── profiles ───

def put_profile(profile: dict):
    profiles_table().put_item(Item=to_ddb(profile))


def get_profiles(ids: list[str]) -> list[dict]:
    """Fetch profiles by id, order-preserving. Bounded to the handful a match returns, so per-id
    GetItem (a BatchGetItem is the swap if result sets ever grow)."""
    by_id = {}
    for pid in dict.fromkeys(ids):
        item = profiles_table().get_item(Key={"gerp_profile_id": pid}).get("Item")
        if item:
            by_id[pid] = item
    return [by_id[pid] for pid in dict.fromkeys(ids) if pid in by_id]


# ─── the inverted index ───

def reindex(profile: dict):
    """Sync a profile's match-key index rows: write its current keys, drop any stale ones from a
    prior index. Idempotent — the publish path calls this whenever a profile's match-keys change."""
    pid = profile["gerp_profile_id"]
    want = index_keys(profile)
    have = {row["match"] for row in _profile_index_rows(pid)}
    for match in have - want:
        index_table().delete_item(Key={"match": match, "gerp_profile_id": pid})
    for match in want - have:
        index_table().put_item(Item={"match": match, "gerp_profile_id": pid})


def deindex(pid: str):
    """Remove all of a profile's index rows (on profile delete)."""
    for row in _profile_index_rows(pid):
        index_table().delete_item(Key={"match": row["match"], "gerp_profile_id": pid})


def _profile_index_rows(pid: str) -> list[dict]:
    return index_table().query(
        IndexName="by-profile", KeyConditionExpression=_Key("gerp_profile_id").eq(pid)).get("Items", [])


def query_index(match: str) -> list[str]:
    """The gerp_profile_ids matchable under one `<field>#<value>` key."""
    match = match.strip().lower()
    return [row["gerp_profile_id"] for row in index_table().query(
        KeyConditionExpression=_Key("match").eq(match)).get("Items", [])]
