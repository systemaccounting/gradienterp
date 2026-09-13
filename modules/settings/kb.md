# locations and job tags — the two dimensions money is sliced by

Both answer "which part of the business was this?" and both ride into a journal entry's
`dimensions`. Neither is a thing you create objects for beyond the minimum.

## locations

The business's locations are config (`manage_locations`: list / add / update). The **ordinal**
is the identifier — item ids lead with it (`2#coke`), journal entries carry it in
`dimensions.location`, and the statement reads slice by it (`dimensions: {"location": "2"}` —
offer a per-location P&L when the owner asks how a branch is doing). Labels and cities are
description; renaming never changes the ordinal.

**Location #1 is "main" and is always the default** — a single-location business never thinks
about any of this, so don't raise it.

When the owner opens a new location: `manage_locations add`, create that location's items with
its ordinal, and at POS connect map the provider's location ids onto the rows
(`manage_locations update` with `square_location_id`). When adding an item rule, ask "everywhere,
or just this branch?" — everywhere is `applies_to: ^\d+#<sku>$`, one branch is `^<n>#<sku>$`.

## job tags

When work is organized by job ("the Smith bathroom remodel"), tag the money to it: pass `job` on
the invoice, the PO, the time entry, the stock movement, the shipment — every posting path
carries it into the entry's dimensions, next to location.

A job is a name the owner picks, not a thing you create — there's no job object, no setup. "Did I
make money on the Smith job?" is a statement read sliced by `dimensions: {"job":
"smith-bathroom"}` — revenue in, parts + hours + freight out, margin per job.

Use one consistent tag per job (kebab-case), and reuse the exact tag the owner already used —
check a statement slice if unsure what's in play. Two spellings of one job is two jobs to the
ledger, and nothing warns you.
