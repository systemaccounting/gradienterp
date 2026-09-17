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
