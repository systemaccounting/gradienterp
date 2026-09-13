"""reindex — gerp-profiles DDB stream → keep gerp-profile-index in sync.

Every profile write (BFF person form, provisioning business rows) flows through the stream, so the
index self-heals without any writer implementing indexing. INSERT/MODIFY → `reindex(new_image)`
recomputes the profile's match-key rows (and drops stale ones); REMOVE → `deindex(id)` clears them.
The DDB-typed stream images are deserialized to plain dicts so `_helpers` sees the same shape it
sees locally.
"""

from boto3.dynamodb.types import TypeDeserializer

from _helpers import deindex, reindex

_deser = TypeDeserializer()


def _plain(image: dict) -> dict:
    return {k: _deser.deserialize(v) for k, v in image.items()}


def handler(event, _context):
    for record in event.get("Records", []):
        name = record.get("eventName")
        ddb = record.get("dynamodb", {})
        if name in ("INSERT", "MODIFY"):
            reindex(_plain(ddb.get("NewImage", {})))
        elif name == "REMOVE":
            pid = _plain(ddb.get("OldImage", {})).get("gerp_profile_id")
            if pid:
                deindex(pid)
    return {"ok": True}
