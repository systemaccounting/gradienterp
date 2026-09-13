"""GET /gerps — the directory: which gerps publish, and where each one's reads live.

Off the operator's own rows, never a copy of a business: `gerp-customers` for the active gerps and
their `gateway_url` (returned to nobody — the consumer talks to this api, this api talks to the
gerp), `gerp-profiles` (`kind=business`) for the published name and place. `published` on the row
is the mirror of the gerp's own flag; a row from before it was stamped is asked at the source —
the gerp's `/oob` catalog is itself gated, so 200 means published and 404 means not. The catalog
rows come back as `sources`, so a consumer knows what to ask for without a second call.

Cached a minute per container. Anonymous.
"""

import json
import os
import time
import urllib.error
import urllib.request

from aws import client as _aws

CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "gerp-customers")
PROFILES_TABLE = os.environ.get("PROFILES_TABLE", "gerp-profiles")
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))
_cache = {"at": 0.0, "gerps": []}


def _s(item, key, default=""):
    v = item.get(key)
    if not v:
        return default
    return v.get("S", v.get("BOOL", default))


def _active_rows():
    ddb = _aws("dynamodb")
    kw = {"TableName": CUSTOMERS_TABLE}
    while True:
        page = ddb.scan(**kw)
        for it in page.get("Items", []):
            if _s(it, "status") == "active" and _s(it, "gateway_url"):
                yield it
        if not page.get("LastEvaluatedKey"):
            return
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _catalog(gateway_url):
    """The gerp's own `/oob` catalog, or None when the gerp does not publish (404) or does not answer."""
    try:
        with urllib.request.urlopen(f"{gateway_url}/oob", timeout=4) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return None if e.code == 404 else []
    except Exception:  # noqa: BLE001 — a gerp that does not answer is listed with no sources, not dropped
        return []
    out = []
    for s in body.get("sources", []):
        path = s.get("path") or ""
        out.append({"key": path.rsplit("/", 1)[-1] or s.get("label", ""), "kind": s.get("kind", ""), "label": s.get("label", "")})
    return out


def _profile(gerp_id):
    it = _aws("dynamodb").get_item(TableName=PROFILES_TABLE, Key={"gerp_profile_id": {"S": gerp_id}}).get("Item") or {}
    return {"label": _s(it, "label"), "city": _s(it, "city"), "state": _s(it, "state"),
            "naics_intended": [x.get("S", "") for x in it.get("naics_intended", {}).get("L", [])]}


def _entry(row):
    gerp_id = _s(row, "gerp_id")
    published = row.get("published", {}).get("BOOL")
    if published is False:
        return None
    sources = _catalog(_s(row, "gateway_url"))
    if sources is None:            # the source says unpublished
        return None
    if published is None and sources == [] :
        # a row from before the stamp that did not answer: not listed rather than guessed
        return None
    prof = _profile(gerp_id)
    return {"gerp_id": gerp_id, "gerp_profile_id": gerp_id,
            "label": prof["label"] or _s(row, "label") or gerp_id,
            "city": prof["city"], "state": prof["state"], "naics_intended": prof["naics_intended"],
            "sources": sources, "api": f"/v1/gerps/{gerp_id}"}


def _gerps():
    now = time.time()
    if now - _cache["at"] < CACHE_SECONDS and _cache["gerps"]:
        return _cache["gerps"]
    gerps = [e for e in (_entry(r) for r in _active_rows()) if e]
    gerps.sort(key=lambda g: g["label"].lower())
    _cache.update(at=now, gerps=gerps)
    return gerps


def _matches(g, q):
    if q.get("q") and not g["label"].lower().startswith(q["q"].lower()):
        return False
    if q.get("sector") and q["sector"] not in g["naics_intended"]:
        return False
    if q.get("city") and g["city"].lower() != q["city"].lower():
        return False
    if q.get("state") and g["state"].lower() != q["state"].lower():
        return False
    return True


def handler(event, context):
    q = event.get("queryStringParameters") or {}
    gerps = [g for g in _gerps() if _matches(g, q)]
    return {"statusCode": 200,
            "headers": {"content-type": "application/json", "cache-control": f"public, max-age={CACHE_SECONDS}",
                        "access-control-allow-origin": "*"},
            "body": json.dumps({"gerps": gerps})}
