# web client — open work

the owner web app's screens are live (`index.html`); these are the field/flow gaps left. design is
dark, lockup-branded.

## billing collection — not built

payment method + address. collect at gerp launch (`creategerp` view), surface read-only in
`infobilling`. open: collect at launch vs a later "billing" step; which processor's hosted element
(vault the card via the processor's drop-in — never touch raw PAN here).

## verify me — not built

the public profile (`publicuser`) has a greyed **verify me** button + an Unverified/Verified badge
(`verified` bool, default false). the flow that flips it is unbuilt (ID.me-style). owner saves never
touch `verified`.

## gerp profile — not built

a gerp's PUBLIC-facing business profile — distinct from the account/person "public profile". the
gerp/business half of the polymorphic `gerp_profile_id`; today only the person half exists
(`gerp-profiles`). the only gerp-side profile data now is the `label` (business name) on the
`gerp-customers` row.
- shows on openlyoperated.biz — directory tile + business page per openly-operated gerp; gated by
  the `openly_operated` flag (private gerps don't appear).
- owner edits it here — a gerp-level view (under the gerp hub), or captured at gerp launch. fields
  TBD (name, description, category, location, logo/handle).
- store TBD — fields on `gerp-customers` vs a dedicated operator `gerp-profiles` table.
- served by api.openlyoperated.biz (read surface — `prod/api_openlyoperated/`, also unbuilt).

## forward-compat — not go-live scope, don't foreclose

NOT building for go-live; raised so near-term choices don't block it.
north star:
> employer→agent: "ima need a hand catering tomorrow night"
> agent: "i got a guy"

the agent matches an available public-user to an ad-hoc cross-firm labor need. the substrate already
exists one level down: addressed events on the bus + the optimizer (`*.requested → *.matched`). keep
the door open by holding these in near-term work:
- public-user identity lives operator-side + durable (`gerp-profiles`, keyed by
  `gerp_profile_id`) — not siloed in one gerp's contacts.
- the empirical record = the union of published events referencing `gerp_profile_id` (completed
  modules/tasks across gerps) — so labor/tasks emit it in `detail`.
- availability + need are intents on the bus, not a private gerp's table.

later (no build now): ID.me-style verification (the verify-me flow above); `modules/labor` scheduling
people across firms by availability.
