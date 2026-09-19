#!/usr/bin/env python3
"""Lint the two schema surfaces: gateway tool schemas, and the canonical field registries.

1. GATEWAY TOOL SCHEMAS (`modules/*/lambdas/*/schema.json`) — `description` must be <= 200 BYTES.
   The gateway_target `description` is sourced from it and caps at 200; exceeding it fails
   `terraform apply` with "Invalid Attribute Value Length" deep in an apply rather than up front.

   BYTES, not characters. An em dash is one character and three UTF-8 bytes, so a 199-character
   description carrying one is 201 to the API — which is how `charge_saved_method` passed this lint
   and failed the apply.
   Skips lambdas that are NOT gateway-registered (internal cron / terraform-invoked handlers).

2. CANONICAL FIELD REGISTRIES (`modules/schemas/data/*_fields.json`) — entry shape and type
   vocabulary. These are hand-edited and then PUBLISHED to the operator's canonical bucket, where
   every tenant seeds or merges from them, so a malformed entry propagates fleet-wide before anyone
   notices. Nothing validated them until this lint: a field added by hand with a typo'd `type`, a
   missing `description`, or an `enum` with no `values` used to sail through.

   Deliberately NOT checked: whether a stored record matches its field's declared type.
   `validate_fields` is a coherence gate on the VOCABULARY, not a storage constraint — heterogeneous
   per-tenant records are the point (`modules/schemas/AGENTS.md`). This lints the registry itself.

Run standalone or via scripts/test.sh.
"""
import glob
import re
import json
import sys

CAP = 200
# a whole tool schema, minified, as the gateway advertises it. The tool block is over half of every
# cached prefix write; a 28-property union carries 5KB unless its descriptions stop restating the
# property name. Measured in UTF-8 bytes like CAP.
SCHEMA_CAP = 2560
# one allowance, with its reason: manage_contacts carries 36 top-level properties (person, vendor,
# customer, employee fields flat), and that structure alone is 2,673 bytes minified before a word
# of description. Its trimmed size is pinned here; getting under the cap is a restructure
# (modules/contacts/TODO.md), not a trim.
SCHEMA_ALLOWANCE = {"modules/contacts/lambdas/manage_contacts/schema.json": 3800}
NOT_GATEWAY = {"seed_schema", "canonical_pull_invoke"}  # have a schema.json but aren't gateway tools

# The vocabulary in use across the canonical registries. A new type is a deliberate addition here,
# not a typo that silently ships — which is the whole point of pinning it.
TYPES = {
    "string", "number", "decimal", "boolean", "date", "timestamp_ms",
    "enum", "map", "object", "list", "list<string>", "list<object>",
}
# Keys an entry may carry beyond `type`. `fields` nests a sub-schema (list<object> / object);
# `source`/`role` annotate provenance on the profile + registry-driven buckets.
# `class` + `shape` are the oob annotation (modules/schemas/AGENTS.md § the oob annotation): what
# the field IS for a public read
# (economic | operational | subject | secret) and whether it is stored as a snapshot of an event
# or a reference to an entity (copied | referenced).
OPTIONAL_KEYS = {"description", "required", "values", "default", "fields", "source", "role",
                 "class", "shape"}
OOB_CLASSES = {"economic", "operational", "subject", "secret"}
OOB_SHAPES = {"copied", "referenced"}

bad = []

# ── 0. gateway tool schema size ──
for f in sorted(glob.glob("modules/*/lambdas/*/schema.json")):
    if f.split("/")[-2] in NOT_GATEWAY:
        continue
    try:
        doc = json.load(open(f))
    except Exception:  # noqa: BLE001 — reported by the description check below
        continue
    size = len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    cap = SCHEMA_ALLOWANCE.get(f, SCHEMA_CAP)
    if size > cap:
        bad.append((f, f"schema is {size} bytes minified > {cap}"))

# ── 1. gateway tool descriptions ──
for f in sorted(glob.glob("modules/*/lambdas/*/schema.json")):
    if f.split("/")[-2] in NOT_GATEWAY:
        continue
    try:
        desc = json.load(open(f)).get("description", "")
    except Exception as e:  # noqa: BLE001
        bad.append((f, f"unparseable ({e})"))
        continue
    # BYTES: the API counts UTF-8, so an em dash costs 3 against this cap, not 1.
    n_bytes = len(desc.encode("utf-8"))
    if n_bytes > CAP:
        bad.append((f, f"description {n_bytes} bytes > {CAP}"))

n_tools = len(glob.glob("modules/*/lambdas/*/schema.json")) - len(NOT_GATEWAY)


# ── 2. canonical field registries ──
def check_entry(path, name, entry):
    """A registry entry is `{type, ...}`; everything else is optional but must be known."""
    t = entry.get("type")
    if t is None:
        bad.append((path, f"`{name}` has no `type`"))
        return
    if not isinstance(t, str):
        bad.append((path, f"`{name}` type must be a string, got {type(t).__name__}"))
        return
    if t not in TYPES:
        bad.append((path, f"`{name}` unknown type {t!r} — add it to TYPES if deliberate"))
    if t == "enum" and not entry.get("values"):
        bad.append((path, f"`{name}` is an enum with no `values`"))
    if "required" in entry and not isinstance(entry["required"], bool):
        bad.append((path, f"`{name}` required must be a bool"))
    d = entry.get("description")
    if d is not None and (not isinstance(d, str) or not d.strip()):
        bad.append((path, f"`{name}` has an empty description — drop it or write one"))
    cls = entry.get("class")
    if cls is not None and cls not in OOB_CLASSES:
        bad.append((path, f"`{name}` class must be one of {sorted(OOB_CLASSES)}, got {cls!r}"))
    shape = entry.get("shape")
    if shape is not None and shape not in OOB_SHAPES:
        bad.append((path, f"`{name}` shape must be one of {sorted(OOB_SHAPES)}, got {shape!r}"))
    unknown = set(entry) - {"type"} - OPTIONAL_KEYS
    if unknown:
        bad.append((path, f"`{name}` unknown keys {sorted(unknown)}"))
    # a nested sub-schema is entries all the way down
    for sub, subentry in (entry.get("fields") or {}).items():
        if isinstance(subentry, dict):
            check_entry(path, f"{name}.{sub}", subentry)
        elif not isinstance(subentry, str):        # shorthand: {"street_name": "string"}
            bad.append((path, f"`{name}.{sub}` must be an entry object or a type string"))
        elif subentry not in TYPES:
            bad.append((path, f"`{name}.{sub}` unknown type {subentry!r}"))


n_fields = 0
registries = sorted(glob.glob("modules/schemas/data/*_fields.json"))
for path in registries:
    try:
        doc = json.load(open(path))
    except Exception as e:  # noqa: BLE001
        bad.append((path, f"unparseable ({e})"))
        continue
    if not isinstance(doc, dict):
        bad.append((path, "registry must be an object of buckets"))
        continue
    for bucket, fields in doc.items():
        if not isinstance(fields, dict):
            bad.append((path, f"bucket `{bucket}` must be an object of fields"))
            continue
        for name, entry in fields.items():
            if isinstance(entry, dict):
                n_fields += 1
                check_entry(path, f"{bucket}.{name}", entry)
            elif isinstance(entry, str):           # shorthand type
                n_fields += 1
                if entry not in TYPES:
                    bad.append((path, f"`{bucket}.{name}` unknown type {entry!r}"))
            else:
                bad.append((path, f"`{bucket}.{name}` must be an entry object or a type string"))

# the two metric registries: a vocabulary of event names, and queries with typed positional params
PARAM_TYPES = {"string", "number", "timestamp"}
n_metric = 0
for path in ("modules/schemas/data/metric_events.json", "modules/schemas/data/metric_queries.json"):
    try:
        doc = json.load(open(path))
    except Exception as e:  # noqa: BLE001
        bad.append((path, f"unparseable ({e})"))
        continue
    for bucket, entries in doc.items():
        if not isinstance(entries, dict):
            bad.append((path, f"bucket `{bucket}` must be an object of entries"))
            continue
        for name, entry in entries.items():
            n_metric += 1
            if not isinstance(entry, dict) or not str(entry.get("description", "")).strip():
                bad.append((path, f"`{bucket}.{name}` needs a description"))
                continue
            if path.endswith("metric_events.json"):
                if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", name):
                    bad.append((path, f"`{bucket}.{name}` is not <resource>.<action_past>"))
                if not isinstance(entry.get("properties", []), list):
                    bad.append((path, f"`{bucket}.{name}` properties must be a list"))
            else:
                params, sql = entry.get("params"), entry.get("sql")
                if not isinstance(params, list) or any(not isinstance(q, dict) or not q.get("name") or q.get("type") not in PARAM_TYPES for q in params):
                    bad.append((path, f"`{bucket}.{name}` params must be a list of {{name, type}} with type in {sorted(PARAM_TYPES)}"))
                if not isinstance(sql, str) or not sql.strip():
                    bad.append((path, f"`{bucket}.{name}` needs sql"))
                elif isinstance(params, list) and sql.count("?") != len(params):
                    bad.append((path, f"`{bucket}.{name}` has {sql.count('?')} `?` markers and {len(params)} params"))

if bad:
    print(f"schema lint: {len(bad)} problem(s)")
    for f, why in bad:
        print(f"  {f}: {why}")
    sys.exit(1)
print(f"schema lint: {n_tools} gateway descriptions <= {CAP} bytes ✓ · "
      f"{n_fields} registry fields across {len(registries)} registries ✓ · {n_metric} metric entries ✓")
