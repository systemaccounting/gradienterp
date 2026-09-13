# migrating an existing business in — the cutover walk

When onboarding reveals the business already has books (QuickBooks, Xero, spreadsheets, a
shoebox), run this walk. It turns cutover into a procedure with an acceptance test: books that
don't tie out to the source system are not migrated, they're re-entered.

## the cutover rule (everything follows from it)

Pick a **cutover date** with the owner — usually the last day of the last closed month. Before
it: history stays in the old system, which remains the system of record for everything
pre-cutover. After it: everything happens here. The platform imports the **opening state as of
the cutover date** — never historical transactions.

**The double-count rule:** anything imported as an open document (an unpaid invoice, an unpaid
bill, a counted item) posts its own ledger effect — so the opening journal entry must EXCLUDE
those lines. A balance arrives exactly once: by document, or in the opening entry, never both.

## what the owner brings

Ask for whatever exports they have — a trial balance (or balance sheet) as of the cutover date,
an AR aging / open-invoice list, unpaid bills, an item list with costs and counts, an asset
list, employee YTD wage totals (any mid-year payroll report shows these). Small-business lists
paste into chat fine; PDFs go through `inspect_document`. Missing pieces can be reconstructed
in conversation ("who still owes you money, and how much?").

## the walk, in order

1. **contacts** — customers, vendors, employees via `manage_contacts` (op: put). Employees additionally get
   worker rows through the labor onboarding (rates, W-4s — PII through the form, never chat).
2. **items + counts** — `create_item` per item (sku, `unit_cost`, price, components if the
   owner describes recipes). Then, BEFORE the first count, attach the migration valuation:
   `add_rule` matches `STOCK_ADJUSTED#*`, rule `value_adjustment`, name `opening_counts`,
   param `{account: OWNER_EQUITY, account_type: EQUITY}`. Each on-hand count is one
   `update_stock` ADJUSTED movement (`entry_id` = `opening-count-<item_id>`) — it values the
   count into INVENTORY against OWNER_EQUITY at unit_cost. Count each item ONCE (movements are
   append-only; fix a wrong count with a correcting ADJUSTED, not a re-run). When the last
   count lands, `delete_rule` the `opening_counts` row so count-variance valuation returns to COGS.
3. **open AR** — enter each unpaid customer invoice INDIVIDUALLY (so collections and aging
   work): `manage_invoice (op: create)` with the real customer and due date, original date in the memo, and
   the line pointing at `{account: OWNER_EQUITY, accountType: EQUITY}` — NOT a revenue account;
   that revenue was earned in the old system's books. `issue_invoice` posts
   DR ACCOUNTS_RECEIVABLE / CR OWNER_EQUITY.
4. **open AP** — same, mirrored: one `create_po` per unpaid bill with the line at
   `{account: OWNER_EQUITY, accountType: EQUITY}`, then `manage_po (op: receive)` posts
   DR OWNER_EQUITY / CR ACCOUNTS_PAYABLE. Payment later is the normal `manage_po (op: pay)`.
5. **fixed assets** — `manage_assets` add per unit with `cost` + `paid_via: opening` (no entry
   posts — the value arrives in the opening entry's FIXED_ASSETS line). Sub-threshold gear gets
   a row with no cost. The capitalized costs must sum to the source TB's fixed-asset line.
6. **the opening entry** — ONE `post_journal_entry` carrying every REMAINING trial-balance
   line: cash per bank account, FIXED_ASSETS, loans (`NOTES_PAYABLE`), credit cards, sales tax
   payable… balanced to `OWNER_EQUITY`. Use `entryId` = `opening-<cutover-date>` and
   `timestamp` = the cutover date (both deterministic, so a re-run no-ops instead of
   double-posting). **The double-count rule, again:** AR, AP, and INVENTORY are NOT in this
   entry — steps 2–4 already posted them, each crediting or debiting OWNER_EQUITY, so equity
   arrives at the source figure on its own.
7. **payroll YTD** (mid-year migrations — this one bites) — for each W-2 worker, set
   `ytd_wages_at_cutover` (gross wages the old system paid Jan 1 → cutover) and `cutover_date`
   on ONE of their worker rows via `manage_labor` (op: update). The pay run adds it to the YTD that the
   SS/FUTA/SUTA wage-base caps consume; skip it and the platform over-withholds employer taxes
   all year.
8. **config** (offer, don't push) — budgets, automations, locations, reporting cadence: the
   normal setup playbooks, linked from onboarding.

## the acceptance gate

Immediately after the walk — before any new activity — run `get_statement` (statement: trial_balance) and reconcile it
line by line against the source system's trial balance. Show the owner the table: each account,
source figure, platform figure, difference. Every line equal, or the difference named and
explained (rounding, an excluded personal account). Two more checks:

- the income statement over the migration window is EMPTY — a migration that produced revenue
  or expense has re-entered history instead of importing state;
- open-invoice and open-bill lists match the source's aging report one for one.

Report the reconciliation table before declaring the migration done. If a line is off, find the
document or opening line that caused it and correct it — don't plug the difference.
