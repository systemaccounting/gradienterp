"""The shared oob filter: a reader is a filter over the registry, not a hand-written projection.

The property that matters is the DEFAULT. A field nobody classified must be withheld, so shipping
a new column can never publish it by accident — the failure mode is a missing field someone
reports, never a leak nobody notices.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "schemas"))
import oob  # noqa: E402


def _fresh():
    oob._CACHE.clear()
    os.environ["LOCAL_CANONICAL_DIR"] = str(REPO / "modules" / "schemas" / "data")


def test_publishes_economic_and_operational_only():
    _fresh()
    row = {"worker_id": "ava", "role": "barista", "started_at": 1, "rate": 22.0}
    out = oob.project(row, "labor_fields")
    assert out["fields"] == {"role": "barista", "started_at": 1, "rate": 22.0}
    assert out["subjects"] == {"worker_id": "ava"}, "a person is handed back for consent, not inlined"


def test_secret_never_appears_anywhere():
    _fresh()
    # worker-legal.value carries the W-4 / SSN blob
    out = oob.project({"worker_id": "ava", "value": {"ssn": "123-45-6789"}}, "labor_fields")
    assert "value" not in out["fields"] and "value" not in out["subjects"]
    assert "123-45-6789" not in str(out)


def test_an_unclassified_field_is_withheld():
    """The load-bearing default. Add a column, forget to classify it, and it stays closed."""
    _fresh()
    out = oob.project({"role": "barista", "brand_new_column": "anything"}, "labor_fields")
    assert out["fields"] == {"role": "barista"}
    assert "brand_new_column" not in out["fields"] and "brand_new_column" not in out["subjects"]


def test_publishable_is_the_no_identity_shortcut():
    _fresh()
    assert oob.publishable({"worker_id": "ava", "role": "barista"}, "labor_fields") == {"role": "barista"}


# --- the canonical baseline itself -------------------------------------------------------------

import json  # noqa: E402

DATA = REPO / "modules" / "schemas" / "data"
CLASSES = {"economic", "operational", "subject", "secret"}
SHAPES = {"copied", "referenced"}


def _field_registries():
    """Every canonical FIELD registry. chart_of_accounts is a list and rule_params is rate data —
    neither describes fields, so neither carries classes."""
    for path in sorted(DATA.glob("*_fields.json")):
        yield path.stem, json.loads(path.read_text())


def test_every_canonical_field_carries_a_class_and_a_shape():
    """Absent means secret, which is the safe default but a USELESS one: an unclassified registry
    publishes nothing at all. This is the completeness guard — a field added to the baseline without
    an answer to "what is this" fails here, at the one place the answer belongs."""
    naked = [f"{reg}.{bucket}.{name}"
             for reg, doc in _field_registries()
             for bucket, fields in doc.items()
             for name, spec in fields.items()
             if isinstance(spec, dict) and not (spec.get("class") and spec.get("shape"))]
    assert not naked, f"unclassified canonical fields: {naked}"


def test_classes_and_shapes_are_from_the_vocabulary():
    bad = [f"{reg}.{bucket}.{name} -> {spec.get('class')}/{spec.get('shape')}"
           for reg, doc in _field_registries()
           for bucket, fields in doc.items()
           for name, spec in fields.items()
           if isinstance(spec, dict)
           and (spec.get("class") not in CLASSES or spec.get("shape") not in SHAPES)]
    assert not bad, bad


def test_a_subject_is_always_referenced():
    """A person is a REFERENCE, never a copy. Copying who someone is onto a transaction row stamps
    a decision they had not made yet — publish a profile next year and the history must resolve
    backward, which a stamped row cannot express."""
    stamped = [f"{reg}.{bucket}.{name}"
               for reg, doc in _field_registries()
               for bucket, fields in doc.items()
               for name, spec in fields.items()
               if isinstance(spec, dict) and spec.get("class") == "subject"
               and spec.get("shape") != "referenced"]
    assert not stamped, f"a person copied onto a row: {stamped}"


def test_a_contact_row_publishes_no_name_channel_or_document():
    """The CRM row is the private half by construction: what publishes is the relationship and the
    money, and the only route to a name is gerp_profile_id — which a private party does not have."""
    _fresh()
    row = {
        "contact_id": "c-dana", "gerp_profile_id": "sub-123", "entity_type": "person",
        "first_name": "Dana", "last_name": "Reyes", "email": "dana@example.com",
        "phone": "+15555550123", "addresses": [{"line1": "12 Elm St"}], "tax_id": "123-45-6789",
        "is_employee": True, "hourly_rate": 24.5, "role": "barista",
    }
    out = oob.project(row, "contact_fields")
    assert out["fields"] == {"entity_type": "person", "is_employee": True,
                             "hourly_rate": 24.5, "role": "barista"}
    assert out["subjects"] == {"contact_id": "c-dana", "gerp_profile_id": "sub-123"}
    for leaked in ("Dana", "Reyes", "dana@example.com", "5555550123", "Elm St", "123-45-6789"):
        assert leaked not in str(out["fields"]), leaked


def test_a_public_profile_withholds_nothing():
    """A row exists in the profile store only because its owner published one. Withholding a field
    of it would be the platform second-guessing a decision that was theirs."""
    _fresh()
    row = {"gerp_profile_id": "sub-123", "display_name": "Dana Reyes", "first": "Dana",
           "last": "Reyes", "email": "dana@example.com", "city": "Oakland", "soc": ["35-3023"]}
    assert oob.publishable(row, "profile_fields") == row


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all oob filter tests passed")
