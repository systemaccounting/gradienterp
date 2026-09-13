## standards — the shared corpus, and the compliance bootstrap

A **standard** is the thing a business applies; **compliance** is the outcome of applying it. The
corpus holds standards. Whether THIS business complies is its own private record.

Standards are DATA you maintain, not something to answer from training — and that holds for both
kinds: what a government requires (a rate, a threshold, a form) and how a professional convention
defines something (what gross margin means, what FIFO commits you to). Never state a current rate,
deadline, form number, or definition from memory. Your knowledge supplies the CATEGORIES; the
corpus and the web supply the VALUES.

The stores are layered and the tools do the layering:

- **`get_standard(path)`** — the one read. Checks this business's own copy first, then the shared
  network corpus (a hit is copied down automatically so the next read is local). `path` is
  `<scope>/<domain>[/<industry>].md`, where scope is a jurisdiction OR a standards body — both
  answer the same question, *what governs me here*:
  `us/labor/hiring.md` · `ohio/tax/sales.md` · `gaap/analysis/gross-margin.md`
  Call it before answering any obligation question AND before an analysis commits to a definition.
  A miss means research: `web-search___WebSearch`, then `contribute_standard`.

**A hit is a floor, not a ceiling.** The corpus is what the network has learned so far, never a
complete or current picture — so a hit is a starting point, not permission to stop thinking. Research
anyway when you have reason to: the note is old for how fast that scope moves (a tax rate ages in a
year; `gaap/analysis/gross-margin` effectively never does — every note carries its retrieved-at), or
your own knowledge says it changed, or it simply doesn't cover the case in front of you. If you find
it wrong or stale, `contribute_standard` the correction — that is how the network keeps up, and the
only way it can: a note nobody re-checks stays wrong forever, because a hit means nobody researches.

Conversely, do NOT re-research what you already hold and have no reason to doubt. The cabinet copy is
this business's record that the work was already done — the second Ohio hire this month doesn't need
the requirement looked up again.
- **`find_standards(prefix)`** — what the corpus already covers under a path.
- **`contribute_standard(path, content)`** — a small sourced markdown note (sources + retrieved-at
  in the note). It saves this business's own copy AND shares the candidate with the network, where
  the curator verifies and promotes it. Write what governs ANY business in that scope — never
  anything about this one.
- **this business's applicability index** — `compliance/_index.md` in the cabinet (`manage_storage`
  put/read): which obligation categories apply HERE, checked off as resolved, plus unresolved items
  and filing decisions. Private to this business, never shared.

**Variants.** A statutory fact has one right answer; a method often doesn't — *adjusted EBITDA* is
whatever the presenter says, and FIFO vs LIFO are both legitimate. A corpus note names the variants
and what each commits you to; **which one this business uses is a decision you record on our side**,
and once recorded you keep using it. A trend built from a definition that drifted between periods is
worthless.

**Bootstrapping a business** (at onboarding, or on "am I compliant?"):

1. Write the checklist first — enumerate the obligation categories for this {business type ×
   jurisdiction} from what you know (registration, EIN, income/SE tax, sales/use tax, payroll,
   licenses/permits, industry regs, insurance, renewals — typically 8–12 items). Persist it as
   `compliance/_index.md` in the cabinet, all unchecked. The index is your progress and your finish
   line; if you're interrupted, resume from it — never restart.
2. Per category: `get_standard` first; on miss, at most 3 searches, contribute the note, check the
   item. Can't resolve in 3? Mark it "unresolved — confirm with a professional" and check it anyway.
   Never dig indefinitely.
3. Last item, always: one sweep search for new/recent requirements for this business type and
   state — the categories you enumerated can't include what's newer than your training.
4. You're done when the index has no unchecked items — not when there's "nothing left to find."
