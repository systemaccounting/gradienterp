# metrics events

## metrics.record.v1 — LIVE

A product event as a firm records it: `metrics.record` stamps `customer_id` and `zone` and puts it
once on the firm's own bus; the second rule there (`to_operator`) sends every `source = metrics` event to
the operator's bus as is (a bus target is taken once per event, so not through the hub), where the counter counts
a published firm's event under its partition. The detail-type is the event's own name, an open
vocabulary, so no channel projects it by kind; `subject_id` is classed `subject`, so no channel
ever would.
