# iot module

Physical devices — toasters, fridges, HVAC, POS terminals, badge readers, water meters — emit telemetry into each customer's event stream. For openly-operated customers, tower's publisher forwards the telemetry to the public stream; private customers' telemetry stays inside their own sub-account. Product framing in `README.md`.

No upstream module deps. Downstream consumers: accounting (telemetry that maps to cost — kWh × utility rate → utility expense), inventory (temperature/level sensors → stock signals), labor (badge taps → clock in/out), purchasing (level-sensor triggers → quote requests). None of these depend on iot — they subscribe to its events.

## current features

Nothing built yet. This module is a forward-declared placeholder — no code, no infra, no spec. The event schema, infra, ingestion handlers, and (later) the external contract layer are the next deliverables; see `TODO.md`. The sections below describe the intended shape for the next builder.

## external contract layer (planned)

Unlike internal modules, iot's clients are polyglot firmware (arduino/rust/micropython/c) that can't read python or markdown, and are constrained (kb of ram, no http client of choice) — codegen'd bindings matter. The public `api.openlyoperated.biz` stream also needs a language-neutral schema for third-party consumption. A smithy contract layer gets added here once the module is built out. See root `AGENTS.md` for the project's stance on smithy (external contracts only).

## scope

Event types the module will define:

- **telemetry** — periodic readings: `iot.telemetry_recorded` (device_id, measurement, value, unit, timestamp)
- **state change** — discrete transitions: `iot.device_state_changed` (on/off, door open/closed, shift started/ended)
- **threshold alert** — level-crossing events: `iot.threshold_crossed` (device_id, measurement, threshold, direction)
- **health signal** — operational status: `iot.device_heartbeat`, `iot.device_anomaly`

Event schemas: language-neutral types (scalars + structs), versioned. Devices emit; AWS IoT Core ingests; Rules fan out to per-consumer targets.

## planned shape

- `modules/iot/infra/main.tf` — AWS IoT Core things + policies + Rules + a DynamoDB table for raw telemetry + EventBridge fan-out for normalized spec events
- `modules/iot/lambdas/` — thin ingestion handlers per event kind: validate, normalize, emit spec event, write raw reading to DynamoDB
- external contract layer (smithy) — added once handlers and schema stabilize; drives codegen for firmware bindings and api clients

## storage

- **DynamoDB (raw telemetry)** — high-volume sensor readings (sub-minute cadence) land in a PAY_PER_REQUEST table with a per-device-class TTL for retention. Timestream for LiveAnalytics is closed to new accounts platform-wide, so time-series stays in DDB — the same posture the accounting ledger took after that closure (see `modules/accounting/AGENTS.md`).
- **DynamoDB (device registry)** — device_id → customer_id, device_type, claimed_at, fleet metadata. CRUD-over-schema (see root `AGENTS.md` "module test"); device onboarding/retirement lives here, not as bespoke lambdas.
- **EventBridge** — normalized spec events from the ingestion lambdas. Consumers subscribe by pattern (e.g. accounting subscribes to `iot.telemetry_recorded` with measurement=kWh to post utility-expense entries on a schedule).

## flow

1. device connects to AWS IoT Core via MQTT (x.509 cert or AWS IoT credentials provider)
2. device publishes to a topic matching its type (e.g. `telemetry/kwh/<device_id>`)
3. IoT Rule forwards the message to the matching ingestion lambda
4. lambda validates the payload, writes the raw reading to DynamoDB, emits a normalized `iot.*` spec event on EventBridge
5. consumers (accounting, inventory, labor, …) react per their own subscriptions; iot doesn't know or care who listens

## dependencies

- AWS IoT Core (mqtt ingress)
- DynamoDB (raw telemetry + device registry)
- EventBridge (fan-out)
