# iot — implementation plan

Spec in [`AGENTS.md`](AGENTS.md). Forward-declared placeholder; an external contract layer will be added once the module is built out (see root `AGENTS.md` for the project's stance on smithy).

## deliverables

- [ ] event schema — telemetry_recorded, device_state_changed, threshold_crossed, device_heartbeat, device_anomaly. Pinned in `lambdas/<event_kind>/main.py` + `schema.json` to start; an external contract layer (smithy) follows once the shape stabilizes.
- [ ] `infra/main.tf` — AWS IoT Core things + policies + Rules forwarding matched topics to ingestion lambdas + a DynamoDB table for raw telemetry (PAY_PER_REQUEST + per-device-class TTL) + EventBridge fan-out for normalized spec events + DDB device registry (device_id → customer_id, device_type, claimed_at). Not Timestream — Timestream for LiveAnalytics is closed to new accounts platform-wide; time-series lands in DDB, the posture accounting took after that closure (see `modules/accounting/AGENTS.md`).
- [ ] `lambdas/<event_kind>/main.py` — thin ingestion handlers: validate the payload, write raw reading to DynamoDB, emit normalized `iot.*` spec event on EventBridge. One per event kind.
- [ ] public-stream publishing — consumers on EventBridge: accounting subscribes to `iot.telemetry_recorded` with measurement=kWh to post utility-expense entries on a schedule; inventory subscribes to level sensors; labor subscribes to badge taps; etc. Respects tenant `openly_operated` flag per `prod/tower/AGENTS.md` §publisher.
- [ ] device onboarding flow — claim an unprovisioned device to a customer, issue x.509 cert or AWS IoT credentials, register in DDB device registry.
- [ ] tests — `tests/iot/local/test_*.py` with scratch_env; validate schema conformance on synthetic events; validate event → consumer fan-out via EventBridge local equivalent.
- [ ] external contract layer — smithy spec for the public event surface and firmware codegen targets (arduino/rust/micropython/c). Added once handlers and schema stabilize; not blocking for ingestion infra.
