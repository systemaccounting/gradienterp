# printing — implementation plan

Spec in [`AGENTS.md`](AGENTS.md). Forward-declared placeholder; nothing built. Ordered so the
feedback loop closes as early as possible — evidence that isn't attributable to a recipe is wasted.

## deliverables

- [ ] **the provenance pointer** (do this first, and it isn't in this module) — a component list on
      `modules/assets`, each entry `{part, corpus_path, variant, print_run_id, installed_at}`. Without
      it a failure is one firm's broken wand and evidence can't pool across the network. Everything
      else here is worth less until this exists.
- [ ] **print run record** — the module's own object and its only table: `{run_id, corpus_path,
      variant, printer_asset_id, material_item_id + batch, started, outcome (succeeded|failed|
      scrapped), notes}`. A failed run is a first-class row — cheap evidence that a recipe is fragile
      on some machine. Consumes filament as an inventory movement, so cost per part falls out of the
      meter rather than being estimated.
- [ ] **recipe notes in the standards corpus** — the note shape from `AGENTS.md` (geometry artifact ·
      fits · material · process · settings · evidence · cites), at `<equipment>/parts/<part>.md`.
      Needs an artifact-path convention so a note can point at a binary mesh; the corpus tools are
      markdown-only today.
- [ ] **ingest from public git / model sites** — fetch an upstream model, record URL + revision +
      **license**, and refuse to proceed on a non-commercial or share-alike term the firm's use would
      violate. A recipe that can't be legally used is unusable regardless of geometry, and that check
      belongs before anyone spends filament.
- [ ] **incident → recipe attribution** — when a task/incident is filed against an asset component,
      carry the component's `corpus_path` + `variant` onto it, so the curator's sweep can pool
      failures by recipe rather than by firm.
- [ ] **contribute a revision** — the agent revises geometry or settings after a failure and
      contributes back under `_contrib/<account>/`, citing the incidents that motivated it. The
      existing contribute-then-curate path carries it; the curator already preserves variants.
- [ ] **curator evidence aggregation** — extend the weekly sweep to pool part evidence across
      contributors ("PETG variant: 3 firms, 2 base fractures under lateral load") and promote a
      revision naming the load case it addresses. Distinct from the documentary verification the
      curator does for compliance notes: here the verification is **empirical**, and disagreement
      between firms is signal, not noise.
- [ ] **tests** — `tests/printing/local/test_*.py` with scratch_env: a run consumes material and
      records outcome; provenance survives install; an incident attributes to the right recipe +
      variant; a license-blocked upstream refuses before printing.

## deliberately out of scope

- **slicing, and any attempt to own print settings generically.** The recipe records the parameters
  that matter for a part; it is not a slicer profile store and this module is not a slicer.
- **generative design / topology optimization.** The agent can draw or revise geometry; the module
  does not implement a design tool.
- **a parts marketplace.** Selling printed parts is inventory + the cross-firm PO rail, both of which
  already exist. This module is about making them and learning from them.

## open

- **`fits` needs a vocabulary.** `rancilio-silvia` is a fine scope for a part note, but equipment
  naming across a network drifts (`Rancilio Silvia` / `silvia-v6` / a model number). Either the
  corpus tolerates aliases and the curator merges them, or the equipment scope is keyed off something
  already canonical. Count the collisions before building a registry.
