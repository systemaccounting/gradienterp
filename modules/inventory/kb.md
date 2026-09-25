# stock automations: recipes, made-to-order, and end-of-day counts

Inventory can do more than count: an item can carry a **recipe**, sell as **made-to-order**
(materials burn at the moment of sale), and end-of-day counts value their variance automatically. Each of
these is set up once, conversationally, and runs from then on.

An item lives at one location: `create_item` with `location` (the ordinal from `manage_locations`)
makes `<n>#<sku>`, default `1#`, and every movement of it posts to that location. A second branch
gets its own items, and a recipe never crosses locations.

## an item with a recipe (a composite)

When something the business sells is *made from* other things it stocks — a candle from wax + wick +
jar, a doppio from espresso beans — create the finished item with a `components` map on `manage_stock` (op: create_item):
each key is an existing component item's id, each value the quantity consumed **per unit made**
(fractional is fine — half a pound of wax per candle).

1. Make sure every component exists as its own stock item first (raw materials usually have
   `unit_price: 0` — bought, not sold as-is).
2. Create the finished item with `components`, a `unit_price` (what it sells for), and a `unit_cost`
   (what one unit is worth — normally the sum of its components' costs; more if the owner wants
   labor/overhead absorbed into it). The `unit_cost` is what its cost-of-goods posts at when it sells.
3. Batches are built with `manage_stock` (op: move) `movement_type=PRODUCED`: making 50 candles adds 50 finished
   and consumes 50× each component, all-or-nothing — if a material is short, nothing moves and the
   error names what's missing. Production posts no journal entry (value stays in inventory).

## made-to-order: materials burn at the sale

A made-to-order composite (the doppio) never has finished stock — selling it IS making it. That is
one rule row:

    manage_rules(op="add", matches="STOCK_SOLD#<item_id>", rule="produce_on_sale", name="backflush", param={})

From then on every SOLD of that item first produces it from its recipe, so the materials decrement on
each sale and a short recipe fails the sale cleanly. Offer this whenever the owner describes a
make-at-the-counter product. Do NOT attach it to batch-made items — their materials were already
consumed when the batch was PRODUCED.

## end-of-day count (reconciliation)

When the owner reports a physical count ("4.4 bags of beans left"), compare it to the book quantity
(`manage_stock` (op: get) — the book already reflects the day's sales and recipe burns) and record the difference
as **one signed `ADJUSTED`** movement: negative when the shelf is short, positive for a count-up.
The variance is **valued automatically** — it posts to `COST_OF_GOODS_SOLD` against `INVENTORY` at
the item's `unit_cost` — so never post a journal entry for a count yourself.

That default reads the units as **consumed**, which is the common case (a cafe's unlogged recipe
burn). A count can't tell you WHY it moved, so it is never on its own evidence *shrink* — the retail
word for stolen or lost goods. An owner whose counts really are loss, and who wants it on its own P&L
line, creates the account (`write_schema` (op: extend) on `chart_of_accounts`, e.g. `INVENTORY_SHRINKAGE`) and
re-points the valuation — a row, because the reading is the firm's to make:

    manage_rules(op="add", matches="STOCK_ADJUSTED#*", rule="value_adjustment", name="shrinkage",
             param={"account": "INVENTORY_SHRINKAGE"})

That row replaces the built-in COGS default — nothing double-posts.

## offering these

The full menu of automations and their params is `rule_params` (op: get) with `catalog=true`; what's
already attached (including the built-in defaults) is `manage_rules` (op: list) — check it before offering, and
never re-offer what's there. When the owner's description of the business matches one —
made-at-the-counter products, batch production, nightly counts — offer it in plain terms ("want me
to deduct beans automatically every time a doppio sells?") and write the row only on their yes.
`manage_rules` (op: delete) turns one off when they say stop.

## reordering without asking

When a par is set (`required_count` + `order_required` on `REORDER#<item>`), every count and
sale reports the gap. An `auto_order` row on the same key turns the gap into a purchase order
with no turn: `manage_rules op=add matches=REORDER#<item> rule=auto_order params={vendor:
"<their gerp_id>", unit_price: 32, sku: "<their item id>", max_qty: 10}`. The PO goes to the
vendor at that price for the gap (at most `max_qty`), `on_order` is stamped so the next sale does
not order it twice, and a receipt releases it. `unit_price` is what the owner will pay — state it
back to them before adding the row; a vendor's published price that moved is exactly what a
policy must not chase.
