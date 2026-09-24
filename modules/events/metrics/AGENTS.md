# metrics events

## metrics.record.v1 — LIVE

A product event as a firm records it: `metrics.record` stamps `customer_id` and `zone` and puts it
once on the firm's own bus; `events.publish` puts it on the hub with the publication envelope when the firm
is openly operated, and the hub forwards it to the operator, where the counter counts
a published firm's event under its partition. The detail-type is the event's own name, an open
vocabulary, so no channel projects it by kind; `subject_id` is classed `subject`, so no channel
ever would.
