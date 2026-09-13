"""find_profiles — the hub's selection query over the profile registry.

Given match filters (`<field>#<value>` keys, AND-combined) + an optional geo radius, return the
matching profiles. An **indexed point-query per key** (never a scan): query the inverted index for
each key, intersect the id sets, batch-get the profiles, radius-filter if `near` is given. This is
the yellow-pages lookup the hub calls to pick spokes ("appliance repair in Chicago" →
match=["naics#811412", "city#chicago"]). The caller supplies the keys; the hub (which knows the
NAICS/SOC taxonomies) maps a request to them.
"""

import json
from math import asin, cos, radians, sin, sqrt

from _helpers import get_profiles, query_index
from aws import json_default as _json_default


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    lat1, lng1, lat2, lng2 = map(radians, (float(lat1), float(lng1), float(lat2), float(lng2)))
    inner = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(inner))


def handler(event, context):
    payload = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    match = payload.get("match") or []
    if isinstance(match, str):
        match = [match]
    if not match:
        return {"statusCode": 400,
                "body": json.dumps({"error": "match (a list of <field>#<value> keys) is required"})}
    near = payload.get("near")  # {"lat":.., "lng":.., "radius_km":..} — optional geo refine

    # AND over the keys: intersect the id set from each indexed key.
    id_sets = [set(query_index(key)) for key in match]
    ids = set.intersection(*id_sets) if id_sets else set()
    profiles = get_profiles(list(ids))

    if near and near.get("lat") is not None and near.get("lng") is not None:
        radius_km = float(near.get("radius_km", 50))
        profiles = [
            p for p in profiles
            if p.get("lat") is not None and p.get("lng") is not None
            and _haversine_km(near["lat"], near["lng"], p["lat"], p["lng"]) <= radius_km
        ]

    return {"statusCode": 200, "body": json.dumps({"count": len(profiles), "profiles": profiles}, default=_json_default)}
