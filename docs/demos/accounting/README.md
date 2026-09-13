# demo 01 — CFO: the books keep themselves, then the analyst room

**DONE.** Cut and paced. Everything for this demo is in this directory — run `bash cut.sh` to
rebuild both gifs from the take — the beat boundaries and why each number is what it is ride as its comments.

| artifact | length | what it is |
|---|---|---|
| `demo-accounting.gif` | 14.7s · 1.2M | the login-page card — prompt 1 only |
| `demo-accounting-full.gif` | 35.9s · 7.3M | opens from the card — both prompts |
| `demo-analysis.webm` | 134.8s | the source take. **Keep it** — every re-cut comes from here, no re-record. |

## why two prompts in one demo

Demo 1 and the analysis demo were both CFO, which broke the one-slot-per-C[A-Z]O-function ladder the
landing page is built on. Folding them into a single take beats picking one, because the second
prompt **consumes the first's output** — it reads as a chain rather than two unrelated questions.
`record.mjs` already supported multi-turn (`step.turns`, built for the reorder demo), so this cost
no recorder work.

**The arc:** prompt 1 is what your accountant does and QuickBooks already does. Prompt 2 is the room
of analysts under a CFO, and the honest comparison is *weeks of someone building a spreadsheet*.
Escalation in one screen — and artifacts feeding artifacts is the through-line.

## prompt 1 — "prepare my monthly statements"

Beats: ⚙ pulling your balances → ⚙ reading your orders. Answer in sms voice, no tables: "done, july
statements are in s3" then ONE line per statement — headline figure + the S3 object **key** as the
link — then "want me to email these".

Live figures (balanced): net income $6,850 · total assets $50,993.80 · total equity $38,293.80 ·
trial balance $136,293.80.

The money shot is statements presented AS their S3 objects — we wrap AWS in the open. Mechanically:
`get_statement {statement: balances, start: jan 1, end: today}` then a read with `write_csvs: true` (periodEnd=end)`; the chat
renderer linkifies `[key](url)`.

## prompt 2 — "now go through the files in our storage — where are we leaking money? top three, one line each"

**"one line each" is load-bearing.** Without it the agent writes a genuinely excellent memo — three
paragraphs, benchmarks, recommended actions — which is right in live use and unreadable in a gif.
The constraint holds the answer to ~2 lines per finding, and both exchanges then fit one 880×720
frame. No trailing period on the prompt (it types on screen).

Five exports sit in the cabinet: a **38,385-row POS log** (with per-item costs), an AR aging, a GL
extract, labor shift hours, and the delivery app's settlement statements. The question is **open** —
no hint about what to look for — and the answer is a ranked set of DECISIONS with dollar impacts,
not a table.

Four findings are **planted in the fixtures on purpose**, each one a thing a human takes days to
see. The live agent found all four unprompted:

| plant | what it costs | agent found |
|---|---|---|
| labor scheduled flat against demand | worst hours cost more in wages than they earn (6am = 103.7% labor/rev) | ✓ "on your worst days labor cost literally exceeded revenue" · ~$18k/yr |
| delivery app's 30% + 5% rake | half the channel's gross profit | ✓ "they kept **$5,275**" (truth $5,274.95) · 71% → 36% margin |
| AR concentration | $14.7k in the 90+ bucket, one client | ✓ Harbor Law $12,481 across 6 invoices, oldest 171 days |
| a loss-leader hiding in the mix | oat matcha latte at 22% margin vs 79-85% | ✓ found as a BONUS beyond the three asked for, with a price recommendation |

Every checkable figure matched ground truth computed independently before asking. Aggregating tens of
thousands of rows to those numbers is not something a model guesses.

Fixtures: `analysis_fixtures.py` beside this file (deterministic seed; ground truth in its
docstring) → `fixtures/`, uploaded to the cabinet's `uploads/` prefix. Needs `analyze` (v73+).

⚠️ **The "agent found" column quotes the take, and the take used an earlier generation.** `served_by`
was added to the POS writer after recording; it consumes an RNG draw per row, which shifted every
downstream figure (revenue $183,609 → $191,788.80; the worst AR account moved from Harbor Law Group
to Riverside Catering). All four plants survive — only their magnitudes changed. The cut gif is
unaffected. A RE-RECORD would produce different numbers, so recompute from the docstring's block
rather than reusing this table's figures.

## one bug this demo flushed out, fixed

The `analyze` docstring wrote bucket names in `$SHELL` notation, so the agent tried shell
interpolation on what is actually a Python variable — and with no discovery hint it fell back to
`manage_storage` (which indexes *captioned* documents), found nothing, and stopped. The docstring
now names the variables plainly and says to list `uploads/` before assuming. With that, "i added
some stuff to /uploads, what's there?" lists all five files with sizes and infers what each one is;
discovery was never missing, the tool description was.

Worth knowing rather than fixing: `manage_storage` and the sandbox see different things on purpose —
the first indexes curated, captioned documents, the second reads raw bytes. Two surfaces for two
jobs, not a gap.

## leftovers on the tenant

- the five analysis fixtures are still in the cabinet's `uploads/` prefix, and the `analysis/`
  artifacts the sandbox wrote are still beside them. Harmless; delete if you want it pristine.
