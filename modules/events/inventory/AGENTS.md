# inventory events

The physical meter's visibility plane: what moved, what's low, what's idle, what it costs. The
richest single source in the matching table — supply AND demand both live here. Emitter module:
`modules/inventory`. All placeful events carry the `location` ordinal; `catalog_key` is the
location-free SKU (the cross-firm join key — a counterparty's items live at THEIR locations).

## stock.moved.v1 — PENDING
- **emit site**: `manage_stock (op: move)` (all four movement types), beside the movement-log append.
- the @point movement mirrored outward: the physical i/o series. Aggregates feed demand curves,
  turnover benchmarks, and the public cost feed. `item_id` is the full `<n>#<sku>` key.

## inventory.low.v1 — SPEC
- **fires**: reorder-point crossing (needs the min/max feature — `modules/inventory/TODO.md`).
- **match**: other firms' low signals on the SAME `catalog_key` → pooled purchasing (one
  aggregated RFQ, volume price). The pooling join is `catalog_key`, never the prefixed id.
- **graduation**: a pool that fires monthly → `invoicing/contract.offered` (standing agreement).

## inventory.surplus.v1 — SPEC
- overstock/obsolete offered out; `unit_cost` is the carrying-cost floor a price-down match
  starts from. Matches `purchase_request.created` + `demand_forecast.published` elsewhere.

## price.updated.v1 / catalog.published.v1 — SPEC, BUILD FIRST
- **why first**: most matching rows quietly assume prices flow; today they only move inside RFQ
  bid windows. The README's founding example (cafe sees the neighbor pays $0.42/oz, requotes the
  roaster) is triggered by a price BECOMING VISIBLE — this is that event.
- **emit site when built**: `manage_stock (op: create_item)` / a price-change path on the item row; `catalog.published`
  is the bulk publication (a price list at once).
- **match**: other firms' open POs, standing orders, recent purchase history — likely the
  highest-frequency match on the platform. `prior_price` lets consumers compute deltas stateless.

## capacity.idle.v1 — SPEC
- unbooked forward windows on a capacity item (`reserve (op: availability)`'s free windows, emitted).
- **match**: production orders / work orders / reservations needing that `capacity_class`; feeds
  the precomputed spare-capacity index (the parking-lot-60-idle-spots signal).

## production_order.created.v1 — SPEC
- a planned build (PRODUCED forthcoming); an op marked outsourceable matches idle work-center
  capacity at other firms.

## byproduct.offered.v1 — SPEC
- waste/offcuts/heat as another firm's feedstock — industrial symbiosis; one firm's entropy is
  another's input. `cadence: recurring` marks a stream worth graduating to a standing contract.

## lot.recalled.v1 — SPEC
- **the inverted match**: urgency flows DOWNSTREAM. Consumers = firms whose movement logs contain
  the lot. Cross-firm lot traceability is structurally impossible in conventional ERP because the
  chain crosses firm boundaries; here the chain is one substrate. `severity: mandatory` should
  eventually drive an automatic quarantine ADJUSTED via a rule instance (design later).
- **prereq**: lot tracking on movements (not yet modeled — the schema leads the feature).

## asset.listed.v1 — SPEC
- outright disposal of a fixed asset — distinct from lease (temporary) and RMA (sold goods
  returning). Matches purchase requests / capacity needs for the asset class.

## demand_forecast.published.v1 — SPEC
- a firm's expected consumption per period = the supplier's order book (collaborative planning).

## reservation.requested.v1 — SPEC
- overflow capacity demand: full hotel → sister property. Matches peer `capacity_class` items;
  geo-scoped via `location` + the profile registry's city keys.
