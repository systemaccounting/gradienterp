"""metric rules — a callsite's moment recorded as a product event.

`record_metric` is the in-firm producer of modules/metrics. A firm attaches it to a callsite key it
already has (`INVOICE_STATUS#paid`, `STOCK_SOLD#<item>`, `CLOSE_SHIFT#<worker>`) and that moment
becomes a row in the firm's product record, named in the firm's own words. The instance row is the
whole configuration; the params say which ctx field is the subject and which ride along.

It returns nothing: the effect is the event on the firm's own bus, so it attaches safely where a
callsite folds returns into postings. It never raises: the callsite already did its write.
"""

import json
from typing import Annotated

import metrics
from rules import rule


@rule
def record_metric(
    ctx,
    event:      Annotated[str,  "string", "The event in the firm's own words, <resource>.<action_past>: member.joined, loaf.sold, shift.worked."],
    subject:    Annotated[str,  "string", "Which field of the moment names the subject: contact_id, invoice_id, worker_id, item_id."],
    properties: Annotated[dict, "object", "Fields of the moment that ride along, {name: field}: {\"qty\": \"quantity\", \"plan\": \"customer\"}."] = None,
):
    subject_id = ctx.get(subject)
    if subject_id is None or str(subject_id) == "":
        print(json.dumps({"event": "metric_not_recorded", "incident": "fail",
                          "subject": f"metric:{event}", "category": "metrics",
                          "label": f"Metric `{event}` is attached where `{subject}` is not on the moment",
                          "fields": sorted(k for k in ctx if k != "rule_exec_id")}))
        return []
    props = {}
    for name, field in (properties or {}).items():
        v = ctx.get(field)
        if v is not None:
            props[name] = v
    execs = ctx.get("rule_exec_id") or []
    try:
        metrics.record({"event": event, "subject_id": str(subject_id), "properties": props},
                       via="rule", rule_exec_id=execs[-1] if execs else None)
    except Exception as e:  # noqa: BLE001 — the callsite's write already happened
        print(json.dumps({"event": "metric_not_recorded", "incident": "fail",
                          "subject": f"metric:{event}", "category": "metrics",
                          "label": f"Metric `{event}` is not reaching the bus", "error": str(e)}))
    return []
