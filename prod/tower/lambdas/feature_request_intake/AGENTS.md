# feature_request_intake — superseded, not built

This queue-lambda approach (customer agents POST feature requests → operator-review DDB table → approve → edit `modules/schemas/data/*.json` → fan-out re-deploy) was replaced by the event-driven `extend_schema` flow: a customer agent emits `platform.schema.extended.v1` on the shared bus when it extends its own registry, and the operator agent subscribes and reviews agreement. No intake lambda, no request queue.

See `modules/agent/TODO.md` phase 7 for the live mechanism. This directory is a stale scaffold and can be removed.
