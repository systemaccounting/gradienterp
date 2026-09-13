# assets — open work

`AGENTS.md` covers what's built. open, in pickup order:

## the accounting fast-follow

- [ ] `depreciation` rule — generic rule in `asset_rules.py`, offered via
      `rules.offered_rules([...])` (add the module to the OFFERED list — add_rule says
      "unknown rule" otherwise). params: `applies_to`, method, life, salvage. fired by a
      monthly calendar entry walking capitalized assets → Dr DEPRECIATION_EXPENSE /
      Cr ACCUMULATED_DEPRECIATION via `post_journal_entry` (deterministic timestamp for the
      dedup). needs ACCUMULATED_DEPRECIATION added to the canonical chart (contra-asset).
- [ ] asset classes as canonical rule instances — class → three GL accounts + default life +
      method (vehicles-5yr, computers-5yr, furniture-7yr, leasehold, buildings-39yr). a MACRS
      period is a param, never code.
- [ ] `asset_id` as a journal DIMENSION on the acquisition entry — today it carries only
      `{"location": location}`, so per-asset book value is not a query: the id survives solely
      inside `entryId` (`asset-acq-<asset_id>`) and has to be parsed back out. Add it beside
      `location` and cost, depreciation and disposal all slice per asset through the mechanism
      that already exists. Do it before `depreciation` lands, or every depreciation entry
      inherits the same hole.
- [ ] disposal — retire posts the gain/loss entry (remove cost + accumulated depreciation vs
      proceeds); today `retire` is operational-only and says so in its response.
- [ ] `asset.acquired` / `asset.disposed` event schemas + emit calls (triggers per the creation
      rule); disposal is the on-ramp to the `asset.listed` / `sale_leaseback.proposed`
      matching rows.
- [ ] capitalization-threshold nudge — canonical param (de minimis $2,500); the agent should
      suggest capitalizing an add whose cost clears it and expensing one that doesn't.

## later

- [ ] **sellable capacity is excluded from the register, and that leaves a gap** — `manage_assets/main.py:11` and `kb.md` both say a hotel room or rental car is NOT an asset but an inventory capacity item. So a motel's 54 rooms have nowhere to be listed individually, and "room 214 is down for refurbishment" has no home: assets refuses it as sellable capacity, and a capacity item carries no per-unit status. A room is two things on two axes — a physical unit (status / serial / maintenance) and sellable capacity (bookable time, a rate) — and the rule forces a choice that drops the first. The accounting doesn't object: the BUILDING is the capitalized asset, and a room is an uncapitalized register row, the same path already used for installed-base units the firm manages but doesn't own (`owner = contact_id`).
- [ ] warranty promise-clock — `warranty_expiry` is stored; the clock/alert rides the future
      SLA-clock machinery (rules + calendar).
- [ ] parked: ASC 842 / IFRS 16 leases (ROU asset + lease liability, `acquisition_type: LEASE`
      with its own amortization schedule) — settled in principle, don't build unprompted.
- [ ] parked: componentization — path-in-the-key (`1#building-1/roof`, the storage
      convention) carries it when someone needs separate component lives.
