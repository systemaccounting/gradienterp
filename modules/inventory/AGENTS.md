# inventory module

The catalog and its meter. One table of items, one **append-only movement log**, and the reads are folds over that log — `quantity(item, time)` is the single model. Two kinds of item share the one catalog:

- **stock** — a counted good (espresso beans, minibar cokes). Its meter is a running count: `on-hand(t) = Σ Δ ≤ t`.
- **capacity** — a reusable quantity-1 unit (a room, a seat, a chair, a bay). Its meter is availability over time: `net = default − scheduled`. An item is capacity iff it carries an `availability_rule`.

Both are the same movement row (`Δ · item · source · when`); they differ only in the `when` — a `@point` instant for stock, a `@period` interval for capacity. Field set validated against the per-customer `item_fields` registry at lambda cold start; canonical baseline at `modules/schemas/data/item_fields.json`.

Depends on accounting — stock movements invoke `post_journal_entry` — and on settings (the oob read gates on `GERP#openly_operated`). Accounting does not depend on inventory: account classifications are owned by accounting itself (see `modules/accounting/AGENTS.md` "classifications").

## current features

- `manage_stock` (op: create_item) — agent tool / lambda; `PutItem` with `attribute_not_exists(item_id)` (409 on dup), registry-validates, auto-generates `item_id`, sets `created_at`/`updated_at`. An `availability_rule` + `availability_duration` makes it a **capacity** item; a `components` map (component item_id → qty per unit, same-location enforced) makes it a **composite** with a recipe; otherwise it's plain **stock** with `quantity=0`. `item_id` leads with the location ordinal (`<n>#<sku>`, default `1#`; explicit prefix wins) and the row carries a `location` field the posting paths copy into journal dims. Runs the `catalog` trigger to stamp `revenue_account` on the row (see below)
- `manage_stock` (op: move) — agent tool / lambda; appends a `@point` movement to the log, posts a journal entry on SOLD/RECEIVED, and refreshes the item's `quantity` (the materialized cache); oversell/underflow guarded by a DDB conditional. The reorder read follows every move (`REORDER#<item>`: the par, the gap, `on_order` — the caller's figure or the item's own stamp); an `auto_order` row there turns the gap into a PO through the shared agreements request service with no turn, stamping `on_order`, and a RECEIVED releases what it brought. `PRODUCED` assembles a composite from its recipe; a SOLD runs the STOCK_SOLD#-keyed rule instances (a `produce_on_sale` row backflushes a made-to-order composite — § composites); an ADJUSTED runs the `STOCK_ADJUSTED#*` ones, canonically valuing the count variance to COGS (§ stock movements → journal entries)
- `manage_stock` (op: get) — agent tool / lambda; one item by `item_id`, or all items (`Scan`). Reads the cached `quantity`
- `reserve` (op: availability) — agent tool / lambda; a **capacity** item's free windows over a range: `default − scheduled`, folded from the log — plus `utilization` (booked ÷ offered: a room's occupancy, a bay's utilization). Rejects a stock item (its meter is a count)
- `reserve` — agent tool / lambda; book (`−1`) or cancel (`+1`) a capacity item over one interval OR a **recurrence** (`rule` + `duration`, `start` = DTSTART, `end` = UNTIL) — a fortnight of shifts is one call and one row rather than fourteen of each, and the guard runs per occurrence so a clash refuses the whole pattern and names the day. Guards a clash with an existing booking and a range the item isn't offered for; idempotent on `movement_id`. `op=cancel` with a `source` and no interval releases everything still standing under that source in one call
- `oob_inventory` — HTTP `GET /oob/inventory`; in-account `Scan` of this gerp's own items table sorted by inventory value; gated on settings `GERP#openly_operated` (404 if not published)
- DDB table `gerp-inventory-<gerp_id>-items` — hash `item_id`
- DDB table `gerp-inventory-<gerp_id>-movements` — hash `item_id`, range `mv_sk` (the movement log)
- settings row `GERP#oob_catalog#inventory` — static descriptor server's `GET /oob` discovery scans
- outputs `items_table`, `movements_table`, `lambda_functions`, `lambda_arns`

## the movement log

`movements.py` — every item's state is a fold over an append-only log; the totals queries hit are **computed**, never a stored scalar. One row shape, two `when` kinds:

```
 Δ    item          source                when
+6    minibar_coke  restock               @2026-07-15T09:00    (point)
−1    room_101      reservation           @[07-15, 07-18)      (period)
+1    room_101      reservation cancel    @[07-15, 07-16)      (period)
```

- **`@point`** (stock) — `+`receive / `−`sell / `−`count-down, one row per move. `on_hand(t) = Σ Δ ≤ t`; the optional `at` gives the level timeline (turnover, aging, reorder).
- **`@period`** (capacity) — a `±1` over `[start, end)`. A booking is a `−1`, a cancel a `+1` over the same interval; they net out in the fold (`consumed = booked − cancelled`), so there is no state to mutate — only the log to append. A row may also carry a **`rule` + `duration`**, in which case `start` is its DTSTART, `end` its UNTIL, and `movements.spans()` expands it — one row then folds to exactly the spans N rows would. A booking is the same kind of expression as the availability it consumes, which is what keeps a fortnight one call instead of fourteen; anchoring on the row's own start (not the query window) is what keeps its occurrences fixed at the moment it was made.

`mv_sk = "{effective-time}#{movement_id}"` orders an item's movements and makes a re-put idempotent (`attribute_not_exists`). The effective time is half the key, so a caller that may be invoked twice for one event states when the move HAPPENED (`manage_stock` (op: move)'s `timestamp`, e.g. a PO's `received_at`) rather than letting the clock supply a fresh one — a matching `movement_id` under two different clocks is two rows. Bounds persist as ISO-8601 **aware-UTC** strings, and every datetime normalizes to aware UTC at ingest (`movements.parse_utc`): `portion` is tz-agnostic, so a mixed naive/aware comparison raises and an all-naive-different-tz one is silently wrong.

The item's scalar `quantity` is a **materialized cache** of the stock fold, kept O(1) for `manage_stock` (op: get) and the oob public feed. The log is the source of truth, and is what truncates to S3 + Athena for the historical physical-i/o series. `manage_stock` (op: move) writes both in ONE `transact_write_items` — the stock gate and the `mv_sk` gate ride the same transaction, so an oversell leaves no phantom movement and a redelivery moves no count. A caller can tell a redelivery apart by `duplicate: true` on the response.

## capacity: net = default − scheduled

`availability.py` — the interval engine, over vendored [`portion`](https://github.com/AlexandreDecan/portion). The item's `availability_rule` (RFC 5545 RRULE) + `availability_duration` (seconds per occurrence) create the DEFAULT windows across the requested range; the `@period` movements are folded and subtracted.

**A recurrence expands on the BUSINESS's calendar, not UTC.** `from_rrule(..., zone=clock.zone())` —
both capacity tools pass it. `BYHOUR=7` is a civil claim: 7am where the business is, every week of
the year, so the instant behind it moves across a daylight-saving change while the wall clock does
not. Expanding against a UTC anchor put a Pacific cafe's 7am availability at local midnight and then
drifted it an hour twice a year — the spans still looked well-formed, which is why it survived a
demo. `modules/clock/AGENTS.md` has the general rule; the capacity item ids also come back
**canonical** from `load_capacity_item` (an unprefixed `cap-x` resolves to `1#cap-x`) so the
movement log keys the same way the item does — writing movements under the raw id splits the meter
in half and the reservations silently stop counting.

A capacity item that is a PERSON'S time carries them in `subject_contact` (a reference), never in
`item_id` or `name`. The id and name are copied into every movement row and published by
`oob_inventory`, and a key cannot be reclassified afterwards — an earlier `shift-<worker>`
convention published the staff roster of any gerp using it. `subject_contact` follows the contact's
profile link, so whether that worker is named publicly can change later without rewriting history.

Intervals are **half-open `[start, end)`** throughout — a stay ending at 11:00 leaves the item free from 11:00, so back-to-back bookings don't collide. Because consumption is interval subtraction rather than a slot match, an **off-grid** range behaves exactly like an on-grid one: a 2pm→11am stay carves precisely that hole out of a daily rule. (A single-timestamp EXDATE can only negate one whole occurrence, so the same 3-night stay had to be discretized into 3 exdates.)

**CAPACITY-1.** `−` is boolean coverage: right for a quantity-1 unit, wrong for a pool (N of a type), where it *deletes* an interval instead of decrementing a count. A pool of rooms is N rows, one per unit. A large fungible pool (parking, GA seats) needs coverage-counting — `remaining(t) = capacity − overlaps(t)` over an `IntervalDict` — a separate mechanism that must not reuse `−`. See `TODO.md`.

`reserve` records the reservation only. A booking consumes capacity; the money posts when it's invoiced, which is where the revenue account resolves.

**The `source` is the consumer.** A booking tagged `source=job:<invoice_id>` binds the time leg to the invoice carrying the money leg — a hotel reservation, a job scheduler, a shift roster are all this composition over `reserve` + `source`, not modules. Cancel-by-source (`op=cancel`, a `source`, no interval) releases the source's **net standing interval** — `booked − cancelled` restricted to that source, the same fold the availability read uses — so a partial cancel or a rebook under the source is handled by the same algebra, and a retry finds nothing standing (404, never a double-release).

A caller-supplied `source` must be `<namespace>:<id>` — `job` | `po` | `produce` | `subject` (`movements.check_source`, enforced at both `reserve` and `manage_stock` (op: move)); the lambdas' own defaults (`ADJUSTED`, `PRODUCED`, `inventory.update_stock`, the op) are machine constants and bypass it. Two reasons, and the second is why it is a refusal rather than a convention: cancel-by-source matches the EXACT string, so a source is a key and free text makes the undo depend on retyping a phrase identically; and the log is APPEND-ONLY and publishes, so a name typed here is published with no write that can take it back. Whose shift it is goes in as `subject:<contact_id>` — a reference, which resolves to a name only if that contact references a public profile.

## catalog rules: where an item's revenue lands

`catalog_rules.py` — `unit_price` says *how much* an item sells for; `revenue_account` says *where it lands*. That's a **decision**, so it lives in a rule rather than in `manage_stock` (op: create_item): a default buried in a lambda is invisible — the agent can't find it, can't change it, and can't tell it was ever decided. As a rule it's a named surface with a param spec, re-pointable via `set_rule_param` (a `GENERAL` employer-wide row) with no deploy.

The rule defaults off the item's **own shape** — an `availability_rule` means it sells time (`SERVICE_REVENUE`); anything else sells goods (`SALES_REVENUE`) — and fires at **create**, stamping its answer onto the item row. So the posting path never infers anything: the item *says* where its revenue goes, and the ledger never depends on a guess made elsewhere. Precedence: an explicit `revenue_account` on the request > the owner's param row > the rule's code default.

A business whose split differs re-points a param; one that needs its own revenue line creates it (`write_schema` (op: extend) on `chart_of_accounts` — e.g. `TIPS_REVENUE`) and points at it. `accountType` isn't stored — a revenue account is `REVENUE` by definition. `manage_stock` (op: create_item) bundles `rules.py` + `catalog_rules.py` and reads the params table (`RULES_PARAMS_TABLE`).

## stock movements → journal entries

`manage_stock` (op: move) invokes accounting's `post_journal_entry` (via `lambda.invoke` in prod; appends to `LOCAL_INVENTORY_JOURNAL` jsonl in local mode):

- SOLD → debit `COST_OF_GOODS_SOLD` (EXPENSE), credit `INVENTORY` (ASSET)
- RECEIVED → debit `INVENTORY` (ASSET), credit `ACCOUNTS_PAYABLE` (LIABILITY)
- ADJUSTED → the variance, valued: shrink posts DR `COST_OF_GOODS_SOLD` / CR `INVENTORY` at `unit_cost × |Δ|`, a count-up reverses it — so INVENTORY dollars track the physical meter. The posting is the `STOCK_ADJUSTED#*` canonical rule instance (`stock_rules.value_adjustment`); a firm row on that key REPLACES it (create `INVENTORY_SHRINKAGE`, point `account` at it; the migration walk temporarily points `account`/`account_type` at `OWNER_EQUITY`/`EQUITY` so opening counts value to equity, then deletes the row). A zero-cost item posts nothing
- PRODUCED → no journal entry (value stays inside INVENTORY — § composites)

EOD count reconciliation is this plus arithmetic: the owner tells the agent the count, the agent reads book on-hand (`manage_stock` (op: get) — already net of the day's sales and backflushes), and books the difference as one signed ADJUSTED — the movement row records the physical variance, the journal row values it.

`accountType` is set on every line item — without it `post_journal_entry` queues to pending and the entry never settles. `amount = unit_cost × abs(quantity)`.

Sign + guards: SOLD/RECEIVED take a positive quantity (SOLD reduces stock, RECEIVED increases). ADJUSTED takes a signed quantity (positive = recount up, negative = counted short). A conditional on the DDB update blocks SOLD or negative-ADJUSTED from driving quantity below zero.

## composites: PRODUCED assembles from the recipe

An item whose `components` map names other stock items (`{"wax_lb": 0.5, "wick": 1, "jar": 1}`) is a **composite**. `manage_stock` (op: move) `PRODUCED` assembles N of it: `+N` to the composite and `−N×per` from every component in one `TransactWriteItems` — a short component fails the whole build (the error names it and its shortfall) and no cache moves. Component movements are tagged `source=produce:<composite_id>`, so what a build consumed is a log query. `table.meta.client` carries the resource's document-interface transforms — the transaction's values are Python-native, not raw `{"N": ...}` AttributeValues.

PRODUCED posts **no journal entry**: raw → finished is a physical transformation and the value never leaves INVENTORY. The composite's own `unit_cost` (required at create — the owner's valuation: Σ component costs, or more if they absorb labor/overhead) prices its COGS at SOLD.

**Made-to-order is a rule instance, not a workflow.** A doppio pulled at the counter has no finished stock — selling it IS making it. That's config, so it's a row (`modules/rules` — the attachment is the dispatch): `add_rule(matches="STOCK_SOLD#doppio", rule="produce_on_sale")` and every SOLD of the doppio backflushes — `manage_stock` (op: move) runs the STOCK_SOLD#-keyed instances on a SOLD (`stock_rules.py`; a tax on the same ITEM is keyed `INVOICE_LINE#` and never reaches here), collects the `produce` effects, and assembles before the sale gate. A short recipe fails the sale, naming the component. No row, no backflush — a make-to-stock candle sells from finished stock and its materials burned at batch PRODUCED time.

`manage_stock` (op: create_item) refuses a recipe naming a missing component (404 naming it), a capacity item as a component, or `components` on a capacity item — a capacity item's meter is time; there is no stock to assemble. A component may itself be a composite (a gift set of candles); PRODUCED consumes the sub-composite's finished stock, it never recurses into its recipe.

## lambdas

- `manage_stock` (op: create_item) — `PutItem` with `attribute_not_exists(item_id)` idempotency; registry-validates the field set; auto-generates `item_id` if omitted
- `manage_stock` (op: move) — appends the `@point` movement, posts the journal entry on SOLD/RECEIVED, refreshes the cached quantity
- `manage_stock` (op: get) — one item by `item_id`, or all items (scan)
- `reserve` (op: availability) — a capacity item's free windows over a range
- `reserve` — book / cancel a capacity item over an interval

Shared libs, bundled at the zip root beside `main.py` (`extra_root_sources` in `infra/main.tf`):

- `lambdas/_helpers.py` — `IS_LAMBDA` branch, registry loader, `to_ddb` decimal coercion, `post_journal_entry` cross-module invoke, `DecimalEncoder`, local-mode jsonl. In both lambda zips
- `movements.py` — the log: constructors, serialize, folds, store. In both lambdas
- `availability.py` + `capacity.py` + `vendor/` (`portion`, `sortedcontainers`) — the interval engine. In **`reserve` only**: the `@point` path is pure dict + arithmetic, so `movements.py` imports `availability` lazily and `manage_stock` carries no vendor tree

The `oob_inventory` zip is standalone (`main.py` only). Tests in `tests/inventory/local/`: `test_inventory_flow.py` (the stock ops), `test_capacity_flow.py` (the capacity ops), `test_movements.py` (the log + folds), `test_availability.py` (the interval engine). They need `portion` — run under the repo `.venv`.

## oob read

`oob_inventory` (`infra/oob.tf`) serves the module's public read at `GET /oob/inventory`, attached to the per-customer server HTTP API. It scans this gerp's OWN items table in-account (no assume-role) and returns stock on hand sorted by inventory value; gated on settings `GERP#openly_operated` — not published returns 404. Registration is a static `GERP#oob_catalog#inventory` descriptor row in the settings table (terraform-owned), which server's `GET /oob` discovery endpoint scans. Adds a `settings` dependency: table name + ARN in, `dynamodb:GetItem` on the flag.

## infra

`infra/main.tf`: the `items` + `movements` DDB tables; shared lambda IAM role (DDB on the items table, `PutItem`/`Query` on movements, DDB Query on the schema table, DDB `GetItem` on the settings table for the oob flag, `lambda:InvokeFunction` on accounting's `post_journal_entry`, logs); the 5 lambdas + Gateway tool targets. `infra/oob.tf` adds the `oob_inventory` lambda (HTTP-routed, not a Gateway tool), its `GET /oob/inventory` route, and the descriptor row — 6 lambdas total.

A Gateway tool target's `description` is capped at **200 characters**; a longer one fails the apply, so keep each `schema.json` description inside it.

Vars: `gerp_id`, `schema_table_name`, `post_journal_entry_fn_{arn,name}`, `register_with_agent`, `settings_table_{name,arn}`, `server_api_{id,execution_arn}`.

See `TODO.md` for open work (the pool coverage-count layer, retiring calendar's availability half, the revenue account).
