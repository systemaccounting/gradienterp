# analyst-room fixtures

The five CSV exports demo 01's second prompt reads ("go through the files in our storage — where
are we leaking money?"). Everything here is a BUILD ARTIFACT: `../analysis_fixtures.py` generates
the files deterministically (fixed `random.seed(20260724)`), uploads them into the tenant's
cabinet through `manage_storage op=file` (the caption IS the index), and `--check` gates a take.
The CSVs are gitignored and safe to delete — regenerate any time.

## use

```
.venv/bin/python docs/demos/accounting/analysis_fixtures.py            # reset, generate, file, check
.venv/bin/python docs/demos/accounting/analysis_fixtures.py --reset    # unfile them
.venv/bin/python docs/demos/accounting/analysis_fixtures.py --check    # recordable right now?
```

The generator's docstring is the source of truth for the planted findings and their current ground
truth. Four findings are planted on purpose (labor vs demand, the delivery-app rake, AR
concentration, the loss-leader item), plus `served_by` — demo 03's revenue-per-hour input; the two
demos share the POS file deliberately and demo 03's `--check` fails loudly when it isn't filed.

## how to extend

- **Never hand-edit a CSV** — the next regeneration clobbers it. Change the writer in
  `analysis_fixtures.py`.
- **Any new column, row source, or file shifts the WHOLE RNG stream** — every draw after your
  change produces different values, so every downstream figure moves. After extending: regenerate,
  RECOMPUTE the ground-truth block in the generator's docstring from the new files (never copy
  forward), re-upload, and re-run both demos' `--check`.
- **A new planted finding is a distribution, not a row** — plant it in the generator's weights so
  it survives regeneration, document it in the docstring with its computed magnitude, and add what
  the take should surface to `../README.md`.
- A recorded take pins the figures IT read; regeneration moves them. That only matters at
  re-record time — recompute before trusting any documented number against a fresh take.
