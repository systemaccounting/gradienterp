"""The public metric key: one layout, written once (modules/metrics, issue #46).

A public metric is counted on the platform's counters table under the firm's partition, and the
range key is what the definition is and nothing the owner typed, so two firms publishing the same
definition share a key: `<event>#<kind>[#<property>=<value>]#<grain>#<period>`. The stamp at record
time, the counter at write time and the api at read time all build and read it here, and nowhere
else splits it. The vocabulary's bucket is the metric's class, read off `metric_events` by the
event name, never stored in the key. No import beyond `re`, so a zip carries this file alone.
"""
import re

EVENT_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
EVENT_RULE = ("event: <resource>.<action_past> — lowercase letters, digits and _ with dots between, "
              "e.g. member.checked_in")


class Invalid(ValueError):
    """A shape the caller can fix: the message names the field."""


KINDS = ("count", "active", "count_by")
GRAINS = ("day", "week", "month")
PLATFORM = "platform"   # the partition the platform's own signals live under


def _enc(value: str) -> str:
    """`#`, `=` and `%` inside a value, percent-encoded, so the key splits on `#` and `=` alone."""
    return str(value).replace("%", "%25").replace("#", "%23").replace("=", "%3D")


def _dec(value: str) -> str:
    return value.replace("%3D", "=").replace("%23", "#").replace("%25", "%")


def public_key(definition: dict, period: str) -> str:
    """The range key for one point of a public row: `definition` is `{event, kind, grain}` and, for a
    `count_by`, `property` and `value`; `period` is the point's start in the row's zone (`YYYY-MM-DD`,
    `YYYY-Www`, `YYYY-MM`)."""
    event, kind, grain = definition["event"], definition["kind"], definition["grain"]
    if not EVENT_RE.fullmatch(event or ""):
        raise Invalid(EVENT_RULE)
    if kind not in KINDS:
        raise Invalid(f"kind: one of {', '.join(KINDS)}")
    if grain not in GRAINS:
        raise Invalid(f"grain: one of {', '.join(GRAINS)}")
    if not period or "#" in period:
        raise Invalid("period: the point's start, no #")
    parts = [event, kind]
    if kind == "count_by":
        prop, value = definition.get("property"), definition.get("value")
        if not prop or "#" in prop or "=" in prop or value is None:
            raise Invalid("count_by: a property name (no # or =) and a value")
        parts.append(f"{prop}={_enc(value)}")
    parts += [grain, period]
    return "#".join(parts)


def parse_public_key(key: str) -> dict:
    """The exact inverse: `{event, kind, property, value, grain, period}`, `property` and `value` None
    outside a `count_by`."""
    parts = key.split("#")
    if len(parts) == 4:
        event, kind, grain, period = parts
        prop = value = None
        if kind == "count_by":
            raise Invalid("count_by without a property=value")
    elif len(parts) == 5 and parts[1] == "count_by" and "=" in parts[2]:
        event, kind, pv, grain, period = parts
        prop, value = pv.split("=", 1)
        value = _dec(value)
    else:
        raise Invalid("not a public metric key")
    if kind not in KINDS or grain not in GRAINS or not EVENT_RE.fullmatch(event):
        raise Invalid("not a public metric key")
    return {"event": event, "kind": kind, "property": prop, "value": value, "grain": grain, "period": period}


def public_prefix(definition: dict) -> str:
    """Everything before the period, with the trailing `#`: what a Query with `begins_with` reads a
    key's points by."""
    return public_key(definition, "p")[:-1]


def slug(definition: dict) -> str:
    """A metric's id on the api and the site, url-safe: `<event>.<kind>`, the two parts a card is
    about; the grain is the request's."""
    return f"{definition['event']}.{definition['kind']}"
