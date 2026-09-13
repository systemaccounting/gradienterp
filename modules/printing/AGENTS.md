# printing module

Onsite manufacturing: a firm prints a part, installs it, and the network learns from what happens
next. Product framing in `README.md`.

The module owns exactly one thing — **the print run**, the record of making a part instance. Every
other piece already belongs to a module that owns it, and printing links them:

| what | who owns it | status |
|---|---|---|
| the design (geometry, material, settings, evidence) | the **standards corpus** (`modules/agent`'s `get_standard` / `contribute_standard`) | live |
| the machine the part goes into | `modules/assets` | live (no component model — see gaps) |
| machine telemetry + anomalies | `modules/iot` | forward-declared placeholder |
| the failure, with its cause | `tasks` (the exception pattern: `subject_key` + severity/category) | live |
| filament / resin stock | `modules/inventory` | live |
| the printer itself | `modules/assets` (an asset) + `modules/iot` (a device) | live / placeholder |

## current features

Nothing built yet. Forward-declared placeholder — no code, no infra. The sections below describe the
intended shape for the next builder.

## a part is not an STL

The single most important thing to get right, and the thing every model-sharing site gets wrong.

A printed part is not determined by its shape. It's determined by geometry **and** material,
orientation, layer height, nozzle and bed temperature, cooling, wall count, infill, supports, and
post-processing. Change one and the same STL warps, delaminates, or cracks in service. So sharing a
mesh shares the least reproducible half of the recipe and calls it the part.

What gets standardized is the **recipe + the evidence**:

```
geometry        an artifact (STL/3MF/STEP) the note points at — not the note itself
fits            the asset class this is a part FOR (an espresso machine model, a printer, a fixture)
material        PETG / PLA / PA12-CF / 316L — with the food-contact or thermal standard it must meet
process         FDM / SLA / SLS / LPBF, and the orientation the recipe assumes
settings        the parameters that actually matter for THIS part, not a full slicer profile dump
evidence        printed N times across M firms; installed hours; failures with their causes
cites           the material / contact standards this recipe depends on, by corpus path
```

**Different material or process = a VARIANT, not a competing truth.** PETG on an FDM bed and PA12 on
SLS are both legitimate answers with different commitments (cost, temperature, food contact,
cleanability). The standards curator is already instructed to name variants and what each commits you
to rather than collapse them into one right answer — that rule was written for accounting methods and
applies here unchanged.

## where the designs come from

Three sources, and the module treats them the same once ingested:

- **the standards corpus** — the network's own curated recipes, at an equipment-scoped path
  (`<equipment>/parts/<part>.md`, e.g. `rancilio-silvia/parts/steam-wand-tip.md`). Scope generalizes
  the same way it does for jurisdictions and standards bodies: it names the population the standard
  governs, which for a part is *the thing it fits*.
- **public git / model sites** — Printables, Thingiverse, GitHub. The agent fetches, and the recipe
  note records the upstream URL, revision, and **license**. This is not a formality: many published
  models carry non-commercial or share-alike terms, and a cafe printing a part to keep a revenue
  machine running is commercial use. A recipe whose license forbids that is unusable no matter how
  good the geometry, and the note must say so before anyone prints it.
- **the firm's own agent** — drawn, or revised from one of the above. A revision is a contribution
  back, which is how the corpus improves (below).

## certified by live feedback

Certification here is not a claim anyone makes. It is an **accumulated service record**, assembled
from ordinary ERP entries that already exist for their own reasons:

1. **print run** (this module) — a part instance is made: which recipe + variant, which printer,
   which material batch (an inventory movement), the outcome (succeeded / failed / scrapped). A
   failed print is evidence too, and cheap to collect — it says the recipe is fragile on that machine.
2. **installation** — the instance goes onto an asset as a component, **carrying its provenance**:
   the corpus path + variant + print-run id it came from.
3. **service** — `modules/iot` telemetry accrues against the asset (cycles, temperatures, anomalies).
4. **failure** — the wand snaps; an incident is filed on the asset with a **cause**, not just a date.
5. **aggregation** — the curator pools evidence across firms and promotes a revision: *"PETG variant,
   3 firms, 2 base fractures under lateral load"* → stronger material, or a fillet at the base.

**Provenance is the load-bearing link.** Without the pointer from the installed component back to the
corpus recipe + variant, a failure is just this firm's broken wand and evidence cannot pool. With it,
the incident is attributable to a specific recipe and the loop closes. If only one thing from this
spec gets built first, build that pointer.

**The cause is worth more than the count.** "Broke after 6 weeks" is weak. "Broke at the base because
the barista levers it against the bottom of the pitcher" names a **load case** nobody designed for —
off-label, recurring, and absent from every spec sheet. Once named, the fix is an engineering choice
(tougher material, or a fillet, or a sacrificial collar), and the network can evaluate which revision
actually stopped the failures.

## "should I print this?" — three answers, all counts

The question the owner actually asks. None of the three answers is a disclaimer, and none is
"consult a professional" — the point of the evidence chain is that the agent can just say what the
fleet knows:

- **go** — *"there's a million events on this part delivering steamed milk. dont sweat it."* The
  recipe has service behind it, at scale, in the same duty cycle. That's a stronger answer than any
  spec sheet, because a spec sheet describes a coupon in a lab and this describes the actual job.
- **don't, print the other variant** — *"4 firms ran this in PLA, 3 cracked at the base inside a
  month. the PETG variant is clean across 200k cycles. print PETG."* This is what makes the first
  answer worth anything. An oracle that only ever says yes is a marketing page.
- **you'd be first** — *"nobody's printed this one. it cites a food-safe material and the geometry
  looks sane, but there's no service behind it — you'd be the evidence."* Not a hedge and not false
  confidence: the honest state of a new recipe, said plainly. This is also the answer that
  bootstraps the corpus, so it has to be comfortable to give.

The model supplies the CATEGORIES — what a part is for, how it plausibly fails — and the corpus
supplies the VALUES: whether this one actually held. Same split the standards doctrine already draws
for rates and definitions, applied to a physical thing.

## the physical veto

A recipe that doesn't print is wrong regardless of how well it's argued, and unlike a tax rate or an
accounting definition, being wrong costs filament, machine time, or a milk line. This cuts both ways
and is the module's real advantage: **the feedback is empirical rather than documentary**. A
compliance note is verified by reading a source; a part is verified by ten firms printing it. That is
a stronger gate, and it is one no single firm could operate alone.

## risk

**Whoever checks the print-at-your-own-risk box owns the outcome.** The corpus publishes what is
known — the recipe, its variants, the service evidence, the standards it cites — and the firm that
prints decides. No food-contact carve-out, no part class gated differently, no liability apparatus
bolted on. That is the same self-selection the rest of the platform runs on: state the stance
cleanly, publish honestly, and let people who want a different arrangement use something else.

The one detail that makes the box mean anything: **acceptance is against a fingerprint, not a name.**
Recipes revise — that is the whole point of live feedback — so "I accepted `steam-wand-tip`" is
worthless if the geometry changed underneath it. The print run records the recipe path, the variant,
AND the revision accepted, which `modules/agreements`' `terms_fingerprint` already expresses. It also
means the acceptance and the evidence are the same record: the run that later shows up in a failure
pool is the run that carried the agreement.

## gaps this module depends on

- **`modules/assets` has no component model** — an asset is a unit; a tip is a part *on* a unit, not
  its own asset. The provenance pointer needs somewhere to live. Smallest version: a component list on
  the asset row, each entry carrying `{part, corpus_path, variant, print_run_id, installed_at}`.
- **`modules/iot` is unbuilt** — step 3 above is a placeholder until it lands. The loop works without
  it (print → install → incident), just with coarser evidence.
- **corpus tools are markdown-only** — `get_standard` / `contribute_standard` read and write small
  text notes. A geometry file is binary and larger, so the note references an artifact object rather
  than embedding it. That split is correct anyway (the note is the standard; the mesh is an
  attachment), but the tools need an artifact path convention and the bucket policy already scopes
  writes to `_contrib/<account>/`.
