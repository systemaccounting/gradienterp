# iot events

The metered ground truth — the floor of the stack ("measure physical i/o, model no fictions").
Emitter module: `modules/iot` (unbuilt; the schema leads the module).

## device.telemetry.v1 — SPEC
- a physical reading: kWh, temperature, fill level, badge tap. **the placeful-source contract**
  (multi-location doctrine): the device-registry row carries the device's `location`; the
  ingesting lambda stamps it — a meter attributes to where it physically sits.
- downstream (per `iot/AGENTS.md` plans): kWh → utility expense entries; badge taps → labor
  clock-ins; level sensors → `inventory.low`. energy-per-unit is the public cost feed's most
  thesis-central series.
