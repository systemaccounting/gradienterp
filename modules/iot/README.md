# iot

The physical layer of the ledger. Toasters, fridges, HVAC, POS terminals, badge readers, water meters — the machines a business actually runs on — emit telemetry into the customer's event stream.

Ground truth is the metered flow — kWh, units, hours, degrees — and the dollar journal is a projection of it. That is what supplies the R&D gradient: capital reads the published margins, engineers read the published physical inefficiency — energy per unit, idle cycles, scrap rates — and that visibility is what recruits them to fix it.

For customers in openly-operated mode, tower's publisher forwards that telemetry to the public stream on `api.openlyoperated.biz` the same way it publishes `accounting.*` events. So an investor or engineer browsing the feed sees both the financial ledger and the physical operational data, in real time, on the same surface — and can locate inefficiencies directly: "twelve cafes' toasters pull 1.4 kWh per batch; my heating element does 1.19." Private customers' telemetry never leaves their sub-account.

Once the external contract layer lands, an outside engineer can write a client in the language of their choice and start reading the stream within an afternoon — the point of a language-neutral, versioned schema over a polyglot-firmware fleet.

## erp features iot adds

- barcode/RFID scanning and automated physical counts (perpetual inventory, verified by sensors instead of humans)
- shop floor & machine monitoring — run state, cycle counts, throughput per work center
- condition-based / predictive maintenance — vibration, temperature, runtime hours opening incidents on the asset register before the failure
- energy management — kWh per unit, per location, per job; the cost-structure product at its most physical
- cold-chain & environmental monitoring — fridge and holding temps as compliance evidence (and the trigger class parametric cover fires on)
- route & vehicle telemetry — fuel, miles, hours per job feeding job costing with actuals
- metered billing — invoice from measured usage
- demand-response participation — curtailable load, aggregated across firms to utility thresholds

## erp features iot strengthens

- manufacturing/production — MRP, scheduling, and capacity planning become buildable on real machine signal instead of estimates
- costing — actual energy and machine-hours per unit flow into `unit_cost` and the job dimension; standard cost becomes measured cost
- quality — process telemetry alongside inspection records
- utilization — booked vs TRUE capacity use (the hotel's occupancy, the shop's spindle hours)
- labor — presence and badge events beside clock in/out
- the open books themselves — the published operational series that makes a firm optimizable from the outside, which is the thesis

See `AGENTS.md` for the intended module shape and `TODO.md` for the build plan.
