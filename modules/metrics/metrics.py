"""metrics — the one write of modules/metrics.

A product event is a step a subject took with the firm's product: a lead captured, a member
checked in, a loaf sold. Three producers call `record` — the door (`lambdas/record`, an outside
app with a bearer), the rule (`metric_rules.record_metric`, a callsite a firm attached it to) and
the agent (`manage_metrics op=record`) — and it does the same thing for each: check the shape,
put the event on the firm's own bus as source `metrics`. The bus rule in `infra/` carries it from
there into the store.

The event name is the firm's own vocabulary, `<resource>.<action_past>` like every detail-type on
the bus. Nothing here knows what a product is: the name's charset and the subject's presence are
the whole check.
"""

import datetime as dt
import re
from decimal import Decimal

import events

SOURCE = "metrics"
EVENT_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
EVENT_RULE = ("event: <resource>.<action_past> — lowercase letters, digits and _ with dots between, "
              "e.g. member.checked_in")
_UTC = dt.timezone.utc


class Invalid(ValueError):
    """The event is not one the store takes; the message names the field."""


def _scalar(v) -> str:
    """A property value as the string the store holds (`map<string,string>`)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal, str)):
        return str(v)
    raise Invalid("properties: values are strings, numbers or booleans")


def normalize_at(value) -> str:
    """`at` as the UTC instant the store keys on: ISO 8601 with milliseconds and a Z, so that
    string order is time order. None is now; a number is epoch milliseconds; a string is ISO 8601,
    naive taken as UTC."""
    if value is None or value == "":
        t = dt.datetime.now(_UTC)
    elif isinstance(value, bool):
        raise Invalid("at: an ISO 8601 timestamp or epoch milliseconds")
    elif isinstance(value, (int, float, Decimal)):
        t = dt.datetime.fromtimestamp(float(value) / 1000, _UTC)
    elif isinstance(value, str):
        try:
            t = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise Invalid("at: an ISO 8601 timestamp or epoch milliseconds") from e
        t = t.replace(tzinfo=_UTC) if t.tzinfo is None else t.astimezone(_UTC)
    else:
        raise Invalid("at: an ISO 8601 timestamp or epoch milliseconds")
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def normalize(raw) -> dict:
    """`{event, subject_id, at?, properties?}` → `{event, subject_id, ts, properties}`, or Invalid."""
    if not isinstance(raw, dict):
        raise Invalid("an event is an object: {event, subject_id, at?, properties?}")
    event = raw.get("event")
    if not isinstance(event, str) or not EVENT_RE.fullmatch(event):
        raise Invalid(EVENT_RULE)
    subject = raw.get("subject_id")
    if not isinstance(subject, str) or not subject.strip():
        raise Invalid("subject_id: who or what took the step, a non-empty string")
    props = raw.get("properties")
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise Invalid("properties: an object of scalars")
    out = {}
    for k, v in props.items():
        if not isinstance(k, str) or not k:
            raise Invalid("properties: keys are strings")
        if v is None:
            continue
        out[k] = _scalar(v)
    return {"event": event, "subject_id": subject.strip(), "ts": normalize_at(raw.get("at")),
            "properties": out}


def record(raw: dict, via: str, **extra) -> dict:
    """Validate and put one event on the firm's bus. Raises Invalid on shape; the put itself never
    raises (events.emit is fire-and-forget). `extra` rides in the detail: the door adds `caller`,
    the rule adds `rule_exec_id`."""
    d = normalize(raw)
    detail = {"subject_id": d["subject_id"], "ts": d["ts"], "properties": d["properties"], "via": via, **extra}
    events.emit(SOURCE, d["event"], detail)
    return {"event": d["event"], "subject_id": d["subject_id"], "ts": d["ts"]}


# ─── the public metric key ───
#
# A public metric is counted on the platform's counters table under the firm's partition, and the
# range key is what the definition is and nothing the owner typed, so two firms publishing the same
# definition share a key: `<event>#<kind>[#<property>=<value>]#<grain>#<period>`. The stamp at record
# time, the counter at write time and the api at read time all build and read it here, and nowhere
# else splits it. The vocabulary's bucket is the metric's class, read off `metric_events` by the
# event name, never stored in the key.

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

